import asyncio
import json
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from models import PlayRequest, ProgressUpdate, TranscriptStatus, Episode
from services.downloader import download_episode, episode_local_path
from services.transcriber import transcribe_episode
from database import get_db
from config import settings
from pathlib import Path

router = APIRouter(prefix="/player", tags=["player"])

import logging

log = logging.getLogger(__name__)

# In-memory set to avoid duplicate transcription jobs
_transcribing: set[str] = set()

# Downloads in flight, mapping episode id -> None while running, or the error
# string if the last attempt failed. Absent means "not being downloaded".
_downloading: dict[str, str | None] = {}


def _safe_file_path(raw_path: str) -> Path:
    """
    Bug 1: Resolve path and verify it's within media_dir to prevent path traversal.
    Raises HTTPException(403) if outside allowed directory.
    """
    file_path = Path(raw_path).resolve()
    media_path = Path(settings.media_dir).resolve()
    if not str(file_path).startswith(str(media_path)):
        raise HTTPException(403, "Access denied")
    return file_path


@router.post("/play")
async def play(req: PlayRequest):
    """
    Trigger download + transcription for an episode.
    Returns immediately; transcription runs in background.
    Audio is streamed from /player/audio/{episode_id} once downloaded.

    Also returns the podcast's playback preferences, because the player needs
    them at exactly this moment — the speed to use, how much intro to skip,
    whether to open the ad-free cut — and asking for them separately would
    either race the first frames of audio or cost a second round trip on every
    play.
    """
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            """SELECT e.downloaded, e.local_path, e.transcript_status, e.podcast_id,
                      s.playback_rate, s.skip_intro, s.skip_outro,
                      s.prefer_adfree, s.auto_transcribe
                 FROM episodes e
                 LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id
                WHERE e.id = ?""",
            (req.episode_id,),
        )
    finally:
        await db.close()

    if not row:
        raise HTTPException(404, "Episode not found. Fetch episodes first.")

    local_path = Path(row["local_path"]) if row["local_path"] else None
    ready = bool(row["downloaded"]) and local_path is not None and local_path.exists()

    # A show can opt out of transcription entirely. That is a load control, not
    # a preference: transcription is the one stage that can cost money or pin a
    # core for minutes, and some subscriptions are music or background noise
    # nobody will ever search.
    transcribe = row["auto_transcribe"] is None or bool(row["auto_transcribe"])

    if not ready:
        _start_download(
            req.episode_id, req.audio_url,
            row["transcript_status"] if transcribe else "done",
        )
    elif transcribe and row["transcript_status"] not in ("done", "processing"):
        _start_transcription(req.episode_id, local_path)

    return {
        "episode_id": req.episode_id,
        "audio_url": f"/player/audio/{req.episode_id}",
        "transcript_status": row["transcript_status"],
        # "downloading" means poll /player/download-status; "ready" means the
        # audio can be requested now.
        "status": "ready" if ready else "downloading",
        "settings": {
            "playback_rate": row["playback_rate"],
            "skip_intro": row["skip_intro"],
            "skip_outro": row["skip_outro"],
            "prefer_adfree": None if row["prefer_adfree"] is None else bool(row["prefer_adfree"]),
            "auto_transcribe": None if row["auto_transcribe"] is None else bool(row["auto_transcribe"]),
        },
    }


def _start_transcription(episode_id: str, local_path: Path) -> None:
    if episode_id in _transcribing:
        return
    _transcribing.add(episode_id)

    async def _run():
        try:
            await transcribe_episode(episode_id, local_path)
        finally:
            _transcribing.discard(episode_id)

    asyncio.create_task(_run())


def _start_download(episode_id: str, audio_url: str, transcript_status: str) -> None:
    """Fetch the audio in the background.

    Awaiting the download inside /play held the request open for as long as it
    took, which meant the play button sat spinning, leaving the screen lost the
    work, and two episodes could not be fetched at once. Now the request
    returns immediately and the client polls, so several can download in
    parallel and none of them depend on a screen staying open.
    """
    if episode_id in _downloading:
        return
    _downloading[episode_id] = None          # in flight, no error

    async def _run():
        try:
            path = await download_episode(episode_id, audio_url)
            db = await get_db()
            try:
                await db.execute(
                    "UPDATE episodes SET downloaded = 1, local_path = ? WHERE id = ?",
                    (str(path), episode_id),
                )
                await db.commit()
            finally:
                await db.close()
            _downloading.pop(episode_id, None)
            if transcript_status not in ("done", "processing"):
                _start_transcription(episode_id, path)
        except Exception as exc:
            log.warning("download failed for %s: %s", episode_id, exc)
            _downloading[episode_id] = str(exc)

    asyncio.create_task(_run())


@router.get("/download-status/{episode_id}")
async def download_status(episode_id: str):
    """Whether an episode's audio is on disk yet.

    Server-side, so it survives leaving the screen: come back and the download
    is either still going or already finished.
    """
    error = _downloading.get(episode_id, ...)
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT downloaded, local_path FROM episodes WHERE id = ?", (episode_id,)
        )
    finally:
        await db.close()
    if not row:
        raise HTTPException(404, "Episode not found")

    on_disk = bool(row["downloaded"]) and bool(row["local_path"]) and Path(row["local_path"]).exists()
    return {
        "episode_id": episode_id,
        "downloaded": on_disk,
        "downloading": error is None,        # present in the map with no error
        "error": error if isinstance(error, str) else None,
    }


@router.get("/audio/{episode_id}")
async def stream_audio(episode_id: str):
    """Serve the downloaded audio file to the browser."""
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT local_path FROM episodes WHERE id = ? AND downloaded = 1", (episode_id,)
        )
    finally:
        await db.close()

    if not row or not row["local_path"]:
        raise HTTPException(404, "Audio not downloaded yet")

    # Bug 1: Validate path is within media_dir
    file_path = _safe_file_path(row["local_path"])
    return FileResponse(str(file_path), media_type="audio/mpeg")


@router.get("/episode/{episode_id}")
async def get_episode(episode_id: str) -> Episode:
    """Fetch a single episode by ID from the DB."""
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        )
    finally:
        await db.close()
    if not row:
        raise HTTPException(404, "Episode not found")
    return Episode(**dict(row))


@router.get("/transcript-status/{episode_id}")
async def transcript_status(episode_id: str) -> TranscriptStatus:
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT transcript_status FROM episodes WHERE id = ?", (episode_id,)
        )
    finally:
        await db.close()
    if not row:
        raise HTTPException(404, "Episode not found")
    return TranscriptStatus(episode_id=episode_id, status=row["transcript_status"])


@router.get("/adfree-status/{episode_id}")
async def adfree_status(episode_id: str):
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            'SELECT adfree_path, ads_detected FROM episodes WHERE id = ?', (episode_id,)
        )
        if not row:
            return {'has_adfree': False, 'ads_count': 0}
        has = bool(row['adfree_path']) and Path(row['adfree_path']).exists()
        return {'has_adfree': has, 'ads_count': row['ads_detected'] or 0}
    finally:
        await db.close()


@router.get("/audio-adfree/{episode_id}")
async def stream_adfree(episode_id: str):
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            'SELECT adfree_path FROM episodes WHERE id = ?', (episode_id,)
        )
        if not row or not row['adfree_path'] or not Path(row['adfree_path']).exists():
            raise HTTPException(status_code=404, detail='Ad-free version not found')

        # Bug 1: Validate path is within media_dir
        file_path = _safe_file_path(row['adfree_path'])
        return FileResponse(str(file_path), media_type='audio/mpeg')
    finally:
        await db.close()


@router.get("/progress")
async def list_progress():
    """Every episode you have started or finished.

    Fetched once when the app loads, so the device you pick up knows where the
    device you put down had got to. Joins the episode and podcast so a device
    that has never seen an episode can still render it in "Continue listening"
    — a fresh phone has no local copy of the title or artwork.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT p.episode_id, p.position, p.duration, p.played, p.updated_at,
                      e.title, e.image_url,
                      s.title AS podcast_title, s.image_url AS podcast_image
               FROM playback p
               LEFT JOIN episodes e      ON e.id = p.episode_id
               LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id
               ORDER BY p.updated_at DESC"""
        )
        return [
            {
                "episode_id": r["episode_id"],
                "position": r["position"],
                "duration": r["duration"],
                "played": bool(r["played"]),
                "updated_at": r["updated_at"],
                "title": r["title"],
                "podcast_title": r["podcast_title"],
                "podcast_image": r["podcast_image"] or r["image_url"],
            }
            for r in rows
        ]
    finally:
        await db.close()


@router.put("/progress/{episode_id}")
async def save_progress(episode_id: str, update: ProgressUpdate):
    """Upsert a position and/or a finished flag.

    Only the fields present are written, so the every-few-seconds position
    save cannot clear the finished flag, and marking an episode finished
    cannot rewind it.
    """
    sets, params = [], []
    if update.position is not None:
        sets.append("position = ?")
        params.append(max(0.0, update.position))
    if update.duration is not None:
        sets.append("duration = ?")
        params.append(max(0.0, update.duration))
    if update.played is not None:
        sets.append("played = ?")
        params.append(1 if update.played else 0)
    if not sets:
        raise HTTPException(400, "Nothing to update")

    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            f"""INSERT INTO playback (episode_id, position, duration, played, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(episode_id) DO UPDATE SET
                  {", ".join(sets)}, updated_at = excluded.updated_at""",
            (episode_id,
             max(0.0, update.position or 0.0),
             update.duration,
             1 if update.played else 0,
             now,
             *params),
        )
        await db.commit()
    finally:
        await db.close()
    return {"episode_id": episode_id, "updated_at": now}


@router.delete("/progress/{episode_id}")
async def delete_progress(episode_id: str):
    """Forget an episode entirely — neither started nor finished."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM playback WHERE episode_id = ?", (episode_id,))
        await db.commit()
    finally:
        await db.close()
    return {"status": "cleared"}


@router.get("/transcript/{episode_id}")
async def get_transcript(episode_id: str):
    """The whole transcript, for reading along while the audio plays.

    Encoded as [start, end, text] triples rather than objects: an hour of
    speech is ~10k words, and repeating three JSON keys on every one of them
    roughly doubles the payload for no added meaning. Times are rounded to
    10ms, which is finer than a spoken word boundary can be heard anyway.

    Sent whole rather than windowed. The client needs to scroll the entire
    transcript, and a range endpoint would turn one cached fetch per episode
    into a request every few seconds of playback.
    """
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT words_json, language FROM transcripts WHERE episode_id = ?",
            (episode_id,),
        )
    finally:
        await db.close()

    if not row:
        raise HTTPException(404, "No transcript for this episode")

    try:
        words = json.loads(row["words_json"])
    except (TypeError, ValueError):
        raise HTTPException(500, "Stored transcript is not readable")

    return {
        "episode_id": episode_id,
        "language": row["language"],
        "words": [
            [round(float(w.get("start", 0)), 2),
             round(float(w.get("end", 0)), 2),
             w.get("word", "")]
            for w in words
        ],
    }


@router.post("/backfill")
async def start_backfill():
    """Fill in transcripts for episodes that have none, from captions only.

    Never runs speech-to-text: a backfill spans the whole back catalogue, and
    routing that through a paid backend could cost a great deal unannounced.
    Videos without captions are counted and skipped, and still transcribe on
    first play — where it is one episode the listener chose.
    """
    from services import backfill
    if backfill.status()["running"]:
        return {"status": "already_running", **backfill.status()}
    asyncio.create_task(backfill.run())
    return {"status": "started", "pending": await backfill.pending_count()}


@router.get("/backfill/status")
async def backfill_status():
    """Progress of a run, and how many episodes are still missing a transcript."""
    from services import backfill
    return {**backfill.status(), "pending": await backfill.pending_count()}


@router.post("/backfill/stop")
async def stop_backfill():
    """Finish after the video in flight."""
    from services import backfill
    backfill.request_stop()
    return {"status": "stopping"}


@router.get("/brief/{episode_id}")
async def episode_brief(episode_id: str):
    """What this episode is about, in a couple of lines.

    Generated the first time an episode is opened and stored on the row, so it
    is paid for once and only for episodes actually looked at. Written into the
    same `summary` column the nightly chapterizer uses, so the page renders it
    through the path that already exists.
    """
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT title, summary, transcript_status FROM episodes WHERE id = ?",
            (episode_id,),
        )
        if not row:
            raise HTTPException(404, "Episode not found")
        if row["summary"]:
            return {"episode_id": episode_id, "summary": row["summary"], "generated": False}
        if row["transcript_status"] != "done":
            return {"episode_id": episode_id, "summary": None, "generated": False}

        transcript = await db.execute_fetchone(
            "SELECT words_json FROM transcripts WHERE episode_id = ?", (episode_id,)
        )
        if not transcript:
            return {"episode_id": episode_id, "summary": None, "generated": False}

        from services.note_builder import brief as build_brief
        loop = asyncio.get_event_loop()
        summary = await loop.run_in_executor(
            None, build_brief, transcript["words_json"], row["title"]
        )
        if summary:
            await db.execute(
                "UPDATE episodes SET summary = ? WHERE id = ?", (summary, episode_id)
            )
            await db.commit()
        return {"episode_id": episode_id, "summary": summary, "generated": bool(summary)}
    finally:
        await db.close()


@router.get("/export/{episode_id}")
async def export_note(episode_id: str, enrich: bool = True):
    """The episode as one Markdown note, for pasting into a vault.

    `enrich=false` returns immediately from what is already stored — summary,
    chapters, distills, bookmarks. With enrichment it adds key points, what the episode
    mentioned, and a diagram of its argument, which costs one model call the
    first time and is cached after.
    """
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            """SELECT e.*, s.title AS podcast_title
               FROM episodes e
               LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id
               WHERE e.id = ?""",
            (episode_id,),
        )
        if not row:
            raise HTTPException(404, "Episode not found")
        episode = dict(row)

        chapters = [dict(r) for r in await db.execute_fetchall(
            "SELECT title, start_time FROM chapters WHERE episode_id = ? ORDER BY start_time",
            (episode_id,),
        )]
        gists = [dict(r) for r in await db.execute_fetchall(
            "SELECT start_seconds, text, summary, auto FROM gists WHERE episode_id = ? "
            "ORDER BY start_seconds",
            (episode_id,),
        )]
        # Bookmarks are the cheap half of the same gesture, and a note that
        # left them out would be missing whatever the listener actually
        # reached for while their hands were busy.
        bookmarks = [dict(r) for r in await db.execute_fetchall(
            "SELECT start_seconds, text, note FROM bookmarks WHERE episode_id = ? "
            "ORDER BY start_seconds",
            (episode_id,),
        )]

        extras = None
        if enrich:
            cached = await db.execute_fetchone(
                "SELECT extras_json FROM episode_notes WHERE episode_id = ?", (episode_id,)
            )
            if cached:
                try:
                    extras = json.loads(cached["extras_json"])
                except ValueError:
                    extras = None
            else:
                transcript = await db.execute_fetchone(
                    "SELECT words_json FROM transcripts WHERE episode_id = ?", (episode_id,)
                )
                if transcript:
                    from services.note_builder import enrich as build_extras
                    loop = asyncio.get_event_loop()
                    extras = await loop.run_in_executor(
                        None, build_extras, transcript["words_json"], episode["title"]
                    )
                    if extras:
                        await db.execute(
                            """INSERT OR REPLACE INTO episode_notes
                               (episode_id, extras_json, created_at) VALUES (?, ?, ?)""",
                            (episode_id, json.dumps(extras),
                             datetime.now(timezone.utc).isoformat()),
                        )
                        await db.commit()
    finally:
        await db.close()

    from services.note_builder import build_markdown
    markdown = build_markdown(episode, chapters, gists, extras, bookmarks)
    return {
        "episode_id": episode_id,
        "title": episode["title"],
        "enriched": bool(extras),
        "markdown": markdown,
    }


@router.get("/chapters/{episode_id}")
async def get_chapters(episode_id: str):
    """Return chapters and summary for an episode."""
    db = await get_db()
    try:
        ep = await db.execute_fetchone(
            "SELECT summary, chapters_status FROM episodes WHERE id = ?", (episode_id,)
        )
        if not ep:
            raise HTTPException(status_code=404, detail="Episode not found")

        chapters = await db.execute_fetchall(
            "SELECT title, start_time FROM chapters WHERE episode_id = ? ORDER BY start_time",
            (episode_id,)
        )
        return {
            "episode_id": episode_id,
            "chapters_status": ep["chapters_status"] or "none",
            "summary": ep["summary"],
            "chapters": [{"title": r["title"], "start_time": r["start_time"]} for r in chapters],
        }
    finally:
        await db.close()

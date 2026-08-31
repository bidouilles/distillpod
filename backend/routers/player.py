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

# In-memory set to avoid duplicate transcription jobs
_transcribing: set[str] = set()


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
    """
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT downloaded, local_path, transcript_status FROM episodes WHERE id = ?",
            (req.episode_id,),
        )
    finally:
        await db.close()

    if not row:
        raise HTTPException(404, "Episode not found. Fetch episodes first.")

    local_path = Path(row["local_path"]) if row["local_path"] else None

    # Download if needed
    if not row["downloaded"] or not (local_path and local_path.exists()):
        local_path = await download_episode(req.episode_id, req.audio_url)
        db = await get_db()
        try:
            await db.execute(
                "UPDATE episodes SET downloaded = 1, local_path = ? WHERE id = ?",
                (str(local_path), req.episode_id),
            )
            await db.commit()
        finally:
            await db.close()

    # Start transcription in background if not already done/running
    if row["transcript_status"] not in ("done", "processing") and req.episode_id not in _transcribing:
        _transcribing.add(req.episode_id)

        async def _bg_transcribe():
            try:
                await transcribe_episode(req.episode_id, local_path)
            finally:
                _transcribing.discard(req.episode_id)

        asyncio.create_task(_bg_transcribe())

    return {
        "episode_id": req.episode_id,
        "audio_url": f"/player/audio/{req.episode_id}",
        "transcript_status": row["transcript_status"],
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
    chapters, highlights. With enrichment it adds key points, what the episode
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
    markdown = build_markdown(episode, chapters, gists, extras)
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

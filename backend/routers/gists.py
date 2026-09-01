import asyncio
import logging

from fastapi import APIRouter, HTTPException
from database import get_db
from models import Gist, GistRequest
from services import timeline
from services.snip_engine import create_gist

log = logging.getLogger(__name__)

router = APIRouter(prefix="/gists", tags=["gists"])

# One auto-snip run per episode, however many times the button is pressed —
# each is a model call over a whole transcript.
_snipping: set[str] = set()


@router.post("/")
async def make_gist(req: GistRequest) -> Gist:
    """
    Create an AI distillation at the current playback position.
    Extracts the transcript window and passes it to Claude for a quote + insight.

    `source: "clean"` says the position came from the ad-free/trimmed file,
    which runs on its own clock. Without translating it, a distill taken while
    listening to that cut quoted a passage minutes away from what was heard.
    """
    db = await get_db()
    row = await db.execute_fetchone(
        """SELECT e.id, e.title, e.podcast_id, e.transcript_status, s.title as podcast_title
           FROM episodes e
           JOIN subscriptions s ON e.podcast_id = s.podcast_id
           WHERE e.id = ?""",
        (req.episode_id,),
    )
    at_seconds = await timeline.resolve(db, req.episode_id, req.current_seconds, req.source)
    await db.close()

    if not row:
        raise HTTPException(404, "Episode not found")
    if row["transcript_status"] != "done":
        raise HTTPException(409, f"Transcript not ready (status: {row['transcript_status']})")

    shot = await create_gist(
        episode_id=req.episode_id,
        podcast_id=row["podcast_id"],
        episode_title=row["title"],
        podcast_title=row["podcast_title"],
        current_seconds=at_seconds,
        with_summary=True,
    )

    # Persist shot
    db = await get_db()
    await db.execute(
        """INSERT INTO gists
           (id, episode_id, podcast_id, episode_title, podcast_title,
            start_seconds, end_seconds, text, summary, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (shot.id, shot.episode_id, shot.podcast_id, shot.episode_title,
         shot.podcast_title, shot.start_seconds, shot.end_seconds,
         shot.text, shot.summary, shot.created_at.isoformat()),
    )
    await db.commit()
    await db.close()
    return shot


@router.post("/auto/{episode_id}")
async def auto_snip_episode(episode_id: str):
    """Pick the moments worth keeping from an already-transcribed episode.

    The nightly job does this for new podcast episodes. This is the same thing
    on demand, which is the only route for anything the job does not reach:
    YouTube videos (their pseudo-subscriptions are skipped by the sync) and
    anything older than the job's recency window.

    Returns immediately. The work is a model call over a full transcript, so
    the client polls the gist list rather than holding a request open.
    """
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            """SELECT e.id, e.title, e.podcast_id, e.transcript_status,
                      s.title AS podcast_title
               FROM episodes e
               LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id
               WHERE e.id = ?""",
            (episode_id,),
        )
        if not row:
            raise HTTPException(404, "Episode not found")
        if row["transcript_status"] != "done":
            raise HTTPException(409, "Episode is not transcribed yet")
        transcript = await db.execute_fetchone(
            "SELECT words_json FROM transcripts WHERE episode_id = ?", (episode_id,)
        )
        if not transcript:
            raise HTTPException(409, "Episode is not transcribed yet")
        meta = (row["podcast_id"], row["title"], row["podcast_title"] or "")
        words_json = transcript["words_json"]
    finally:
        await db.close()

    if episode_id in _snipping:
        return {"episode_id": episode_id, "status": "already_running"}
    _snipping.add(episode_id)

    async def _run():
        try:
            from services import jobs
            from services.auto_snipper import build_rows, pick_snips
            loop = asyncio.get_event_loop()
            # Reaches the agent CLI through a thread, so the lane has to be
            # taken here — the async wrapper is not in this path.
            async with jobs.lane("llm", label=f"suggest highlights: {episode_id}"):
                snips = await loop.run_in_executor(None, pick_snips, words_json)
            if not snips:
                return
            podcast_id, episode_title, podcast_title = meta
            rows = build_rows(snips, episode_id, podcast_id, episode_title, podcast_title)
            inner = await get_db()
            try:
                await inner.executemany(
                    """INSERT INTO gists
                       (id, episode_id, podcast_id, episode_title, podcast_title,
                        start_seconds, end_seconds, text, summary, created_at, auto)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    rows,
                )
                await inner.commit()
            finally:
                await inner.close()
            log.info("%s: %d auto-snip(s)", episode_id, len(rows))
        except Exception:
            log.exception("auto-snip failed for %s", episode_id)
        finally:
            _snipping.discard(episode_id)

    asyncio.create_task(_run())
    return {"episode_id": episode_id, "status": "started"}


@router.get("/")
async def list_gists(episode_id: str = None) -> list[Gist]:
    """Distillations, in the order that makes sense for where they are shown.

    Within an episode that is playback order: they are moments in one
    conversation, and reading them in the order they were said is the only way
    they cohere. Ordering by when they were *made* interleaved the nightly
    auto-snips with anything tapped later, so an episode page listed 0:00,
    0:00, 24:34, 14:24 — which reads as a bug even though every entry is right.

    Across the library the useful order is the opposite: newest first, because
    there the unit is "what did I keep recently", not "what happened when".
    """
    db = await get_db()
    if episode_id:
        rows = await db.execute_fetchall(
            "SELECT * FROM gists WHERE episode_id = ? ORDER BY start_seconds ASC",
            (episode_id,),
        )
    else:
        rows = await db.execute_fetchall("SELECT * FROM gists ORDER BY created_at DESC")
    await db.close()
    return [Gist(**dict(r)) for r in rows]


@router.delete("/{shot_id}")
async def delete_gist(shot_id: str):
    db = await get_db()
    await db.execute("DELETE FROM gists WHERE id = ?", (shot_id,))
    await db.commit()
    await db.close()
    return {"status": "deleted"}

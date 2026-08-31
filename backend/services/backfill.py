"""
Fill in transcripts for the library you already have — from captions only.

Most of the library arrives without a transcript on purpose: a channel import
creates episode rows from a listing without fetching anything per video, and the
nightly caption pass is capped and only looks back 48 hours. That keeps a
subscription free, but it leaves the majority of episodes unsearchable, and
search, chat, distills, auto-snips and the Obsidian export are all gated behind
having a transcript.

**This never runs speech-to-text.** A backfill covers the whole back catalogue at
once, and doing that through a paid hosted backend could cost a great deal
without warning. An episode whose video has no captions is counted and left
alone; it still transcribes on first play, where the cost is one episode the
listener actually chose. That is a deliberate constraint, not an oversight.

Pacing is the other constraint. Asking yt-dlp for metadata in a tight loop is
what tripped YouTube's bot check during the channel work and had the address
refused for everything for a while afterwards, so requests are spaced and a run
gives up rather than hammering once refusals start arriving.
"""
import asyncio
import logging
from datetime import datetime, timezone

from database import get_db
from services import youtube
from services.transcriber import store_transcript

log = logging.getLogger(__name__)

# Spacing between videos. Slower than strictly necessary, because the cost of
# being throttled is measured in hours of refusal, not seconds of waiting.
REQUEST_SPACING_SECONDS = 4.0

# Consecutive failures that mean YouTube is refusing rather than one video being
# odd. Carrying on past this earns a longer ban and transcribes nothing.
CONSECUTIVE_FAILURE_LIMIT = 5

_state: dict = {
    "running": False,
    "total": 0,          # episodes this run set out to do
    "transcribed": 0,
    "no_captions": 0,    # left alone rather than sent to speech-to-text
    "failed": 0,
    "current": None,     # title being worked on
    "stopped_early": False,
    "finished_at": None,
}


def status() -> dict:
    return {**_state}


def request_stop() -> None:
    """Ask a run to finish after the video in flight."""
    _state["stop_requested"] = True


async def pending_count() -> int:
    """How many episodes a backfill would attempt right now."""
    db = await get_db()
    try:
        row = await db.execute_fetchone(_PENDING_SQL.format(select="COUNT(*) AS n"))
        return row["n"] if row else 0
    finally:
        await db.close()


# Only YouTube, only episodes that are actually reachable in the app. An episode
# whose subscription has been removed does not appear in the feed, so filling in
# its transcript would spend requests on something nobody can open.
_PENDING_SQL = """
    SELECT {select}
    FROM episodes e
    JOIN subscriptions s ON s.podcast_id = e.podcast_id
    WHERE e.transcript_status != 'done'
      AND e.audio_url LIKE '%youtu%'
    ORDER BY e.published_at DESC
"""


async def run() -> None:
    """Work through pending episodes. Never raises."""
    if _state["running"]:
        return
    _state.update({
        "running": True, "total": 0, "transcribed": 0, "no_captions": 0,
        "failed": 0, "current": None, "stopped_early": False,
        "finished_at": None, "stop_requested": False,
    })

    consecutive_failures = 0
    try:
        db = await get_db()
        try:
            episodes = [dict(r) for r in await db.execute_fetchall(
                _PENDING_SQL.format(select="e.id, e.title, e.audio_url")
            )]
        finally:
            await db.close()

        _state["total"] = len(episodes)

        for i, ep in enumerate(episodes):
            if _state.get("stop_requested"):
                _state["stopped_early"] = True
                break
            if i:
                await asyncio.sleep(REQUEST_SPACING_SECONDS)

            _state["current"] = ep["title"]
            try:
                meta = await youtube.fetch_metadata(ep["audio_url"])
                words = await youtube.fetch_caption_words(meta)
            except Exception as exc:
                consecutive_failures += 1
                _state["failed"] += 1
                log.info("backfill: %s failed (%s)", ep["id"], exc)
                if consecutive_failures >= CONSECUTIVE_FAILURE_LIMIT:
                    # Almost certainly rate limiting. Stop and let it cool off.
                    log.warning("backfill: stopping after %d consecutive failures",
                                consecutive_failures)
                    _state["stopped_early"] = True
                    break
                continue

            consecutive_failures = 0

            if not words:
                # No captions. Deliberately NOT sent to speech-to-text — see the
                # module docstring. It will transcribe on first play instead.
                _state["no_captions"] += 1
                continue

            db = await get_db()
            try:
                await store_transcript(db, ep["id"], words,
                                       language=youtube.caption_language(meta))
                await db.commit()
                _state["transcribed"] += 1
            finally:
                await db.close()
    except Exception:
        log.exception("backfill run failed")
    finally:
        _state.update({
            "running": False,
            "current": None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })

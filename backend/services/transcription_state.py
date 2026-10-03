"""Shared transcription state for HTTP handlers, cron and the STT worker thread."""
import logging
import sqlite3
import time

import database
from database import get_db

log = logging.getLogger(__name__)


async def update(episode_id: str, status: str, stage: str, error: str | None = None):
    db = await get_db()
    try:
        await db.execute("""UPDATE episodes SET transcript_status=?, transcript_stage=?,
            transcript_error=?, transcript_progress=? WHERE id=?
            AND (? != 'queued' OR transcript_status NOT IN ('processing','queued'))""",
            (status, stage, error[:600] if error else None, 100 if status == "done" else None, episode_id, status))
        await db.commit()
    finally:
        await db.close()


def reporter(episode_id: str):
    """Thread-safe bounded writes; a late update must never overwrite completion."""
    last_write = 0.0
    last_stage = None
    def report(percent: float | None, stage: str):
        nonlocal last_write, last_stage
        now = time.monotonic()
        if stage == last_stage and now - last_write < 3:
            return
        try:
            with sqlite3.connect(database.DB_PATH, timeout=5) as db:
                db.execute("""UPDATE episodes SET transcript_progress=?, transcript_stage=?
                    WHERE id=? AND transcript_status='processing'""",
                    (min(99, max(0, percent)) if percent is not None else None, stage, episode_id))
            last_write, last_stage = now, stage
        except sqlite3.Error as exc:
            log.warning("Could not update transcription progress: %s", exc)
    return report

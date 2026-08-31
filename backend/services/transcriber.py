"""
Episode transcription orchestration: run the STT backend, store word-level
timestamps, then kick off ad detection.

The transcription itself lives in services/stt.py — this module only cares that
it gets back [{word, start, end}, ...].
"""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import aiosqlite
from config import settings
from database import get_db, index_transcript
from services import stt


async def store_transcript(db, episode_id: str, words: list[dict],
                           language: str = "") -> str:
    """Persist a word list as the episode's transcript and mark it done.

    The one place a transcript is written, whichever backend produced it — the
    STT run below, or YouTube's own captions. Keeping it single means the FTS
    index can never be forgotten, which would silently make the episode
    unsearchable. Returns the serialised words for callers that need them.
    """
    words_json = json.dumps(words)
    await db.execute(
        """INSERT OR REPLACE INTO transcripts (episode_id, words_json, language, created_at)
           VALUES (?, ?, ?, ?)""",
        (episode_id, words_json, language or settings.stt_language or "auto",
         datetime.now(timezone.utc).isoformat())
    )
    await index_transcript(db, episode_id, words_json)
    await db.execute(
        "UPDATE episodes SET transcript_status = 'done' WHERE id = ?",
        (episode_id,)
    )
    await db.commit()
    return words_json


async def transcribe_episode(episode_id: str, audio_path: Path) -> None:
    """
    Transcribe episode in background thread, save words to DB.
    Updates transcript_status in episodes table throughout.
    """
    db = await get_db()
    try:
        # Mark as processing
        await db.execute(
            "UPDATE episodes SET transcript_status = 'processing' WHERE id = ?",
            (episode_id,)
        )
        await db.commit()

        # Off the event loop: CPU-bound for whisper, a long HTTP call for voxtral
        loop = asyncio.get_event_loop()
        words = await loop.run_in_executor(None, stt.transcribe, str(audio_path))

        words_json = await store_transcript(db, episode_id, words)

        # ── The clean cut ────────────────────────────────────────────────────
        # Runs after the transcript is committed; failure is non-fatal, because
        # a missing clean cut degrades the episode while a lost transcript
        # wastes the expensive half of the work.
        try:
            await build_clean_cut(db, episode_id, audio_path, words_json)
        except Exception:
            pass  # never block the transcript result

    except Exception as e:
        await db.execute(
            "UPDATE episodes SET transcript_status = 'error' WHERE id = ?",
            (episode_id,)
        )
        await db.commit()
        raise e
    finally:
        await db.close()


async def build_clean_cut(db, episode_id: str, audio_path, words_json: str) -> dict | None:
    """Produce the clean version of an episode, honouring the podcast's settings.

    One place for a step two callers need — first play and the nightly job —
    because they must agree: whether silence is trimmed is a per-podcast
    setting, and a step implemented twice would honour it in one path only.

    What comes out is a second audio file plus the map back to the original
    timeline, stored on the row. That map is the part that cannot be recovered
    later: without it every stored timestamp is wrong by however much was
    removed before it, which is what made distills and bookmarks taken from the
    ad-free version quote passages the listener had never heard.
    """
    import asyncio
    from services import audio_processor
    from services.ad_detector import detect_ads

    loop = asyncio.get_event_loop()

    row = await db.execute_fetchone(
        """SELECT s.trim_silence, s.normalize_volume
             FROM episodes e
             LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id
            WHERE e.id = ?""",
        (episode_id,),
    )
    trim_silence = bool(row["trim_silence"]) if row and row["trim_silence"] is not None else False
    normalize = bool(row["normalize_volume"]) if row and row["normalize_volume"] is not None else False

    ads = await loop.run_in_executor(None, detect_ads, words_json)

    output = Path(audio_path).parent / f"{episode_id}_adfree.mp3"
    result = None
    if ads or trim_silence or normalize:
        result = await loop.run_in_executor(
            None,
            lambda: audio_processor.process(
                str(audio_path), str(output), ads=ads,
                trim_silence=trim_silence, normalize=normalize,
            ),
        )

    await db.execute(
        """UPDATE episodes
              SET ads_detected = ?, adfree_path = ?,
                  processed_segments = ?, trimmed_seconds = ?
            WHERE id = ?""",
        (len(ads),
         str(output) if result else None,
         json.dumps(result["segments"]) if result else None,
         # What trimming pauses saved, separately from what removing ads did:
         # it is the number the setting promises, so it is the one to show.
         (result["removed_seconds"] - _ads_seconds(ads)) if result else None,
         episode_id),
    )
    await db.commit()
    return result


def _ads_seconds(ads: list[dict]) -> float:
    total = 0.0
    for ad in ads:
        try:
            total += max(0.0, float(ad["end"]) - float(ad["start"])) + 2.0   # incl. the air
        except (KeyError, TypeError, ValueError):
            continue
    return total


async def get_transcript_words(episode_id: str) -> list[dict]:
    db = await get_db()
    row = await db.execute_fetchone(
        "SELECT words_json FROM transcripts WHERE episode_id = ?", (episode_id,)
    )
    await db.close()
    if not row:
        return []
    return json.loads(row["words_json"])

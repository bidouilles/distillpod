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
from database import get_db
from services import stt


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

        words_json = json.dumps(words)

        # Save transcript
        await db.execute(
            """INSERT OR REPLACE INTO transcripts (episode_id, words_json, language, created_at)
               VALUES (?, ?, ?, ?)""",
            (episode_id, words_json, settings.stt_language or "auto",
             datetime.now(timezone.utc).isoformat())
        )
        await db.execute(
            "UPDATE episodes SET transcript_status = 'done' WHERE id = ?",
            (episode_id,)
        )
        await db.commit()

        # ── Ad detection + ad-free audio generation ──────────────────────────
        # Runs after transcript is committed; failure is non-fatal.
        try:
            from services.ad_detector import detect_ads, remove_ads_from_audio
            ads = await loop.run_in_executor(None, detect_ads, words_json)
            ads_count = len(ads)
            adfree_path: Optional[str] = None
            if ads:
                adfree_file = Path(audio_path).with_suffix("") \
                    .parent / f"{episode_id}_adfree.mp3"
                success = await loop.run_in_executor(
                    None, remove_ads_from_audio, str(audio_path), ads, str(adfree_file)
                )
                if success:
                    adfree_path = str(adfree_file)
            await db.execute(
                "UPDATE episodes SET ads_detected = ?, adfree_path = ? WHERE id = ?",
                (ads_count, adfree_path, episode_id)
            )
            await db.commit()
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


async def get_transcript_words(episode_id: str) -> list[dict]:
    db = await get_db()
    row = await db.execute_fetchone(
        "SELECT words_json FROM transcripts WHERE episode_id = ?", (episode_id,)
    )
    await db.close()
    if not row:
        return []
    return json.loads(row["words_json"])

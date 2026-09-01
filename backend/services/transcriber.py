"""
Episode transcription orchestration: get word-level timestamps by the cheapest
route that works, store them, then build the clean cut and the search index.

Two routes, in that order. A YouTube video's own captions carry a timestamp per
word, cost nothing and arrive in seconds; speech-to-text is the fallback, which
is the case it exists for. The transcription itself lives in services/stt.py —
this module only cares that it gets back [{word, start, end}, ...].
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import aiosqlite
from config import settings
from database import get_db, index_transcript
from services import jobs, stt, youtube

log = logging.getLogger(__name__)


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

        words, language = await _obtain_words(episode_id, audio_path)
        words_json = await store_transcript(db, episode_id, words, language=language)

        # ── Meaning-based search index ───────────────────────────────────────
        # Straight after the transcript, so an episode is searchable by meaning
        # as soon as it is searchable by word. Non-fatal and a no-op when no
        # embedding backend is configured.
        try:
            from services import semantic_index
            if await semantic_index.opted_in(db):
                await semantic_index.index_episode(db, episode_id, words_json)
        except Exception:
            pass

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


async def _obtain_words(episode_id: str, audio_path: Path) -> tuple[list[dict], str]:
    """The transcript for an episode, by the cheapest route that works.

    For a YouTube video, that is its own captions: they carry a timestamp per
    word, cost nothing, arrive in seconds, and are what the ingest path already
    prefers. Only the play path skipped them — so pressing play on a captioned
    video sent it to a paid speech-to-text backend to re-derive, at length, a
    transcript YouTube would have handed over for free. Every video whose
    captions the nightly pass had not reached took that route.

    Speech-to-text remains the fallback, which is the whole point of having it:
    a video with no captions still gets a transcript, and so does every podcast.
    """
    if episode_id.startswith("yt-"):
        try:
            async with jobs.lane("youtube", label=f"captions: {episode_id}"):
                meta = await youtube.fetch_metadata(_video_url(episode_id))
                words = await youtube.fetch_caption_words(meta)
            if words:
                log.info("%s: transcribed from captions (%d words)", episode_id, len(words))
                return words, youtube.caption_language(meta)
            log.info("%s: no captions, falling back to speech-to-text", episode_id)
        except Exception as exc:
            # A refused or missing caption fetch is not fatal: speech-to-text
            # can still do it, which is exactly the case it exists for.
            log.info("%s: caption fetch failed (%s), falling back to speech-to-text",
                     episode_id, exc)

    # Keyed: the same episode asked for twice is transcribed once. That matters
    # more here than anywhere else — a duplicate is a second bill.
    async with jobs.lane("stt", label=f"transcribe: {episode_id}",
                         key=f"stt:{episode_id}") as turn:
        if turn.duplicate:
            stored = await _stored_words(episode_id)
            if stored:
                return stored, ""
        loop = asyncio.get_event_loop()
        # Off the event loop: CPU-bound for whisper, a long HTTP call for voxtral
        return await loop.run_in_executor(None, stt.transcribe, str(audio_path)), ""


async def _stored_words(episode_id: str) -> list[dict]:
    """Whatever another turn just stored for this episode, if anything."""
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT words_json FROM transcripts WHERE episode_id = ?", (episode_id,)
        )
    finally:
        await db.close()
    if not row:
        return []
    try:
        return json.loads(row["words_json"])
    except (TypeError, ValueError):
        return []


def _video_url(episode_id: str) -> str:
    """The watch URL for a `yt-<id>` episode."""
    return f"https://www.youtube.com/watch?v={episode_id[len('yt-'):]}"


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

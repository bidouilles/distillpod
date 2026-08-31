"""
Add a YouTube video to the library as an ordinary episode.

Deliberately thin: the video becomes an `episodes` row (plus a pseudo-
subscription standing in for the channel, because the feed joins on one), and
from there every existing feature — player, transcript search, distills, chat,
research — treats it exactly like a podcast episode.

The request returns as soon as the metadata is in, because that is all the
episode row needs. Captions and audio are fetched by a background task, and the
UI already polls /player/transcript-status/{id}, so no new progress channel is
needed.
"""
import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database import get_db
from ids import safe_episode_id
from services import youtube, youtube_library
from services.downloader import download_episode
from services.transcriber import store_transcript, transcribe_episode

log = logging.getLogger(__name__)

router = APIRouter(prefix="/youtube", tags=["youtube"])

# Same guard as the player's: one ingest per video, however many times the
# button is pressed.
_ingesting: set[str] = set()


class AddVideoRequest(BaseModel):
    url: str


def _episode_id(video_id: str) -> str:
    return safe_episode_id(f"yt-{video_id}")


def _podcast_id(meta: dict) -> str:
    """A channel stands in for the podcast, so videos group by who made them."""
    channel_id = meta.get("channel_id") or meta.get("uploader_id") or "unknown"
    return safe_episode_id(f"yt-{channel_id}")


async def _ingest(episode_id: str, meta: dict) -> None:
    """Transcript first (it is what the user is waiting for), then the audio.

    Captions, when the video has them, are both instant and word-level, so the
    STT backend is only woken for videos that have none.
    """
    url = meta.get("webpage_url") or ""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE episodes SET transcript_status = 'processing' WHERE id = ?",
            (episode_id,),
        )
        await db.commit()

        try:
            words = await youtube.fetch_caption_words(meta)
        except Exception as exc:               # a caption failure is not fatal
            log.warning("caption fetch failed for %s: %s", episode_id, exc)
            words = []

        if words:
            await store_transcript(db, episode_id, words,
                                   language=youtube.caption_language(meta))
            log.info("%s: %d words from YouTube captions", episode_id, len(words))
    finally:
        await db.close()

    # Audio is fetched either way: the point of the feature is listening to it.
    try:
        path = await download_episode(episode_id, url)
    except Exception as exc:
        log.error("audio download failed for %s: %s", episode_id, exc)
        if not words:
            db = await get_db()
            try:
                await db.execute(
                    "UPDATE episodes SET transcript_status = 'error' WHERE id = ?",
                    (episode_id,),
                )
                await db.commit()
            finally:
                await db.close()
        return

    db = await get_db()
    try:
        await db.execute(
            "UPDATE episodes SET downloaded = 1, local_path = ? WHERE id = ?",
            (str(path), episode_id),
        )
        await db.commit()
    finally:
        await db.close()

    if not words:
        log.info("%s: no captions, falling back to STT", episode_id)
        await transcribe_episode(episode_id, path)


def _start_ingest(episode_id: str, meta: dict) -> None:
    if episode_id in _ingesting:
        return
    _ingesting.add(episode_id)

    async def _run():
        try:
            await _ingest(episode_id, meta)
        except Exception:
            log.exception("youtube ingest failed for %s", episode_id)
        finally:
            _ingesting.discard(episode_id)

    asyncio.create_task(_run())


# One channel import at a time, however many times subscribe is pressed.
_importing: set[str] = set()


def _start_channel_import(channel_id: str) -> None:
    if channel_id in _importing:
        return
    _importing.add(channel_id)

    async def _run():
        try:
            stats = await youtube_library.sync_channel(channel_id)
            log.info("channel %s: %s", channel_id, stats)
        except Exception:
            log.exception("channel import failed for %s", channel_id)
        finally:
            _importing.discard(channel_id)

    asyncio.create_task(_run())


@router.post("/add")
async def add_video(req: AddVideoRequest):
    """Ingest one video, or subscribe to a channel.

    One box takes both, because from the outside they are the same gesture:
    "follow this". A channel URL subscribes and starts importing its recent
    long-form uploads in the background; a video URL ingests that one video.
    """
    if youtube.is_channel_url(req.url):
        try:
            channel = await youtube_library.subscribe(req.url)
        except youtube.YouTubeError as exc:
            raise HTTPException(502, str(exc))
        _start_channel_import(channel["channel_id"])
        return {"kind": "channel", **channel, "already_added": False}

    video_id = youtube.video_id(req.url)
    if not video_id:
        raise HTTPException(400, "Not a YouTube video or channel URL")

    episode_id = _episode_id(video_id)

    db = await get_db()
    try:
        existing = await db.execute_fetchone(
            "SELECT id, podcast_id, title, transcript_status FROM episodes WHERE id = ?",
            (episode_id,),
        )
    finally:
        await db.close()

    if existing and existing["transcript_status"] in ("processing", "done"):
        return {
            "kind": "video",
            "episode_id": existing["id"],
            "podcast_id": existing["podcast_id"],
            "title": existing["title"],
            "already_added": True,
        }

    try:
        meta = await youtube.fetch_metadata(youtube.watch_url(video_id))
    except youtube.YouTubeError as exc:
        raise HTTPException(502, str(exc))

    podcast_id = _podcast_id(meta)
    channel = meta.get("channel") or meta.get("uploader") or "YouTube"
    title = meta.get("title") or "Untitled video"
    thumbnail = meta.get("thumbnail")
    published = youtube.published_at(meta)
    chapters = youtube.chapters(meta)
    now = datetime.now(timezone.utc).isoformat()

    db = await get_db()
    try:
        # The channel as a subscription: the feed joins on one, and it makes
        # videos filterable by who made them like any other show.
        await db.execute(
            # OR IGNORE, so a channel already subscribed to keeps its
            # standing: adding one of its videos must not demote it.
            """INSERT OR IGNORE INTO subscriptions
               (podcast_id, feed_url, title, image_url, subscribed_at, source)
               VALUES (?, ?, ?, ?, ?, 'youtube_video')""",
            (podcast_id,
             f"https://www.youtube.com/feeds/videos.xml?channel_id={meta.get('channel_id', '')}",
             channel, thumbnail, now),
        )
        # Upsert rather than REPLACE: re-adding a video whose first ingest
        # failed must not drop the ad-free render or summary it may already have.
        await db.execute(
            """INSERT INTO episodes
               (id, podcast_id, title, description, audio_url, duration_seconds,
                published_at, image_url, transcript_status, chapters_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)
               ON CONFLICT(id) DO UPDATE SET
                 podcast_id = excluded.podcast_id,
                 title = excluded.title,
                 description = excluded.description,
                 audio_url = excluded.audio_url,
                 duration_seconds = excluded.duration_seconds,
                 published_at = excluded.published_at,
                 image_url = excluded.image_url,
                 transcript_status = 'queued',
                 chapters_status = excluded.chapters_status""",
            (episode_id, podcast_id, title, meta.get("description") or "",
             meta.get("webpage_url") or youtube.watch_url(video_id),
             int(meta.get("duration") or 0) or None,
             published.isoformat() if published else None,
             thumbnail,
             "done" if chapters else "none"),
        )

        # The uploader's own chapter marks — free, and better than generated ones.
        if chapters:
            await db.execute("DELETE FROM chapters WHERE episode_id = ?", (episode_id,))
            for i, ch in enumerate(chapters):
                await db.execute(
                    "INSERT INTO chapters (id, episode_id, title, start_time) VALUES (?, ?, ?, ?)",
                    (f"{episode_id}-ch{i}", episode_id, ch["title"], ch["start_time"]),
                )
        await db.commit()
    finally:
        await db.close()

    _start_ingest(episode_id, meta)

    return {
        "kind": "video",
        "episode_id": episode_id,
        "podcast_id": podcast_id,
        "title": title,
        "channel": channel,
        "image_url": thumbnail,
        "duration_seconds": int(meta.get("duration") or 0) or None,
        "chapters": len(chapters),
        "has_captions": bool(youtube.caption_track(meta)),
        "already_added": False,
    }

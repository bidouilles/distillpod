"""
YouTube channels as subscriptions.

Adding one video already creates a pseudo-subscription for its channel, because
the feed joins on one. This turns that into a real subscription: the channel is
polled, and new uploads arrive as episodes the way a podcast's do.

Episodes are built from two requests for the whole channel and none per video:

  the /videos tab   decides *which* uploads count. It excludes Shorts and live
                    streams by construction, and yt-dlp reports a duration and
                    live status per entry so the filter can be belt-and-braces.
  the Atom feed     supplies the publish dates the tab returns as null for some
                    channels.

That shape is deliberate and was arrived at the hard way. Asking yt-dlp for each
video's metadata instead — the obvious way to get dates, descriptions and
chapters — meant ten calls in a row on subscribing, which tripped YouTube's
"confirm you're not a bot" check and had the address refused for everything,
including single calls, for a while afterwards. So the sync spends no per-video
calls to create episodes at all.

Transcripts still need one call per video, to reach the caption tracks, so that
pass is capped and spaced out rather than run over everything at once. Anything
it does not reach transcribes on first play, as it always has. A captioned video
costs no audio download, so a subscription costs no disk until you play
something.
"""
import asyncio
import logging
from datetime import datetime, timezone

from database import get_db
from ids import safe_episode_id
from services import youtube
from services.transcriber import store_transcript

log = logging.getLogger(__name__)

# Per run, so a first sync of a long-standing channel cannot import an unbounded
# number of episodes in one go. The rest arrive on later runs.
MAX_NEW_PER_SYNC = 15

# Transcripts are the only part that still costs a call per video. Capped and
# spaced so a sync cannot look like a scraper — see the module docstring.
MAX_TRANSCRIBE_PER_SYNC = 3
CALL_SPACING_SECONDS = 3.0


def episode_id_for(video_id: str) -> str:
    """The same id a manually added video gets, so the two can never duplicate."""
    return safe_episode_id(f"yt-{video_id}")


def podcast_id_for(channel_id: str) -> str:
    return safe_episode_id(f"yt-{channel_id}")


async def upsert_channel(db, channel: dict) -> str:
    """Create or refresh the channel's subscription row. Returns its podcast_id."""
    podcast_id = podcast_id_for(channel["channel_id"])
    await db.execute(
        """INSERT INTO subscriptions (podcast_id, feed_url, title, image_url, subscribed_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(podcast_id) DO UPDATE SET
             feed_url = excluded.feed_url,
             title = excluded.title,
             image_url = COALESCE(excluded.image_url, subscriptions.image_url)""",
        (podcast_id,
         youtube.channel_videos_url(channel["channel_id"]),
         channel["title"],
         channel.get("thumbnail"),
         datetime.now(timezone.utc).isoformat()),
    )
    return podcast_id


async def upsert_video(db, meta: dict, podcast_id: str) -> str:
    """Write one video's metadata as an episode row. Returns its episode id."""
    vid = meta.get("id") or ""
    episode_id = episode_id_for(vid)
    published = youtube.published_at(meta)
    chapters = youtube.chapters(meta)

    # Upsert rather than REPLACE: a re-sync must not drop the ad-free render,
    # summary or local audio path an episode may already have.
    await db.execute(
        """INSERT INTO episodes
           (id, podcast_id, title, description, audio_url, duration_seconds,
            published_at, image_url, transcript_status, chapters_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'none', ?)
           ON CONFLICT(id) DO UPDATE SET
             podcast_id = excluded.podcast_id,
             title = excluded.title,
             description = excluded.description,
             audio_url = excluded.audio_url,
             duration_seconds = excluded.duration_seconds,
             published_at = excluded.published_at,
             image_url = excluded.image_url,
             chapters_status = excluded.chapters_status""",
        (episode_id, podcast_id, meta.get("title") or "Untitled video",
         meta.get("description") or "",
         meta.get("webpage_url") or youtube.watch_url(vid),
         int(meta.get("duration") or 0) or None,
         published.isoformat() if published else None,
         meta.get("thumbnail"),
         "done" if chapters else "none"),
    )

    if chapters:
        await db.execute("DELETE FROM chapters WHERE episode_id = ?", (episode_id,))
        for i, ch in enumerate(chapters):
            await db.execute(
                "INSERT INTO chapters (id, episode_id, title, start_time) VALUES (?, ?, ?, ?)",
                (f"{episode_id}-ch{i}", episode_id, ch["title"], ch["start_time"]),
            )
    return episode_id


async def upsert_listing_video(db, video: dict, podcast_id: str) -> str:
    """Create an episode row from the channel listing alone.

    Fills gaps on an existing row but never overwrites: a video added by hand
    carries a description and the uploader's chapters, and the listing carries
    neither, so replacing it would be a downgrade. COALESCE lets a re-sync
    repair a row that was imported without a publish date.
    """
    episode_id = episode_id_for(video["video_id"])
    published = video.get("published_at")
    await db.execute(
        """INSERT INTO episodes
           (id, podcast_id, title, description, audio_url, duration_seconds,
            published_at, image_url, transcript_status, chapters_status)
           VALUES (?, ?, ?, '', ?, ?, ?, ?, 'none', 'none')
           ON CONFLICT(id) DO UPDATE SET
             published_at = COALESCE(episodes.published_at, excluded.published_at),
             duration_seconds = COALESCE(episodes.duration_seconds, excluded.duration_seconds),
             image_url = COALESCE(episodes.image_url, excluded.image_url)""",
        (episode_id, podcast_id, video["title"], video["url"],
         video.get("duration_seconds"),
         published.isoformat() if published else None,
         video.get("thumbnail")),
    )
    return episode_id


async def sync_channel(
    channel_id: str,
    *,
    limit: int = 15,
    max_new: int = MAX_NEW_PER_SYNC,
    transcribe: bool = True,
) -> dict:
    """Pull the channel's recent long-form uploads in as episodes."""
    podcast_id = podcast_id_for(channel_id)
    stats = {"listed": 0, "new": 0, "transcribed": 0}

    videos = await youtube.fetch_channel_videos(channel_id, limit=limit)
    stats["listed"] = len(videos)
    if not videos:
        return stats

    db = await get_db()
    try:
        known = {
            r["id"] for r in await db.execute_fetchall(
                "SELECT id FROM episodes WHERE podcast_id = ?", (podcast_id,)
            )
        }
        fresh = [v for v in videos if episode_id_for(v["video_id"]) not in known][:max_new]
        for video in fresh:
            await upsert_listing_video(db, video, podcast_id)
        await db.commit()
        stats["new"] = len(fresh)
    finally:
        await db.close()

    if not transcribe or not fresh:
        return stats

    # Newest first, capped and spaced. The rest transcribe on first play.
    for i, video in enumerate(fresh[:MAX_TRANSCRIBE_PER_SYNC]):
        if i:
            await asyncio.sleep(CALL_SPACING_SECONDS)
        episode_id = episode_id_for(video["video_id"])
        try:
            meta = await youtube.fetch_metadata(video["url"])
            words = await youtube.fetch_caption_words(meta)
        except Exception as exc:
            # Rate limiting is expected here and is not a failure: the episode
            # exists, it simply has no transcript yet.
            log.info("channel sync: no transcript yet for %s (%s)", episode_id, exc)
            continue
        if not words:
            continue
        db = await get_db()
        try:
            # Now that the metadata is in hand anyway, take the description and
            # the uploader's chapters with it.
            await upsert_video(db, meta, podcast_id)
            await store_transcript(db, episode_id, words,
                                   language=youtube.caption_language(meta))
            await db.commit()
            stats["transcribed"] += 1
        finally:
            await db.close()

    return stats


async def subscribe(url: str) -> dict:
    """Resolve a channel URL and subscribe to it. Does not import anything."""
    channel = await youtube.resolve_channel(url)
    db = await get_db()
    try:
        podcast_id = await upsert_channel(db, channel)
        await db.commit()
    finally:
        await db.close()
    return {
        "podcast_id": podcast_id,
        "channel_id": channel["channel_id"],
        "title": channel["title"],
        "image_url": channel.get("thumbnail"),
    }

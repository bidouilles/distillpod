import asyncio
import hashlib
from fastapi import APIRouter, HTTPException, Query, Response
from datetime import datetime, timezone
from pydantic import BaseModel
from database import get_db
from models import Podcast, PodcastSettings, Subscription, Episode, Tag
from services import episode_query, opml, podcast_index, rss

router = APIRouter(prefix="/podcasts", tags=["podcasts"])

# When the inbox was last cleared. Stored server-side so "new" means new to
# you, not new to this browser.
INBOX_SEEN_KEY = "inbox_seen_at"


@router.get("/search")
async def search_podcasts(q: str, limit: int = 20) -> list[Podcast]:
    """Search podcasts via Podcast Index API."""
    results = await podcast_index.search_podcasts(q, limit)
    return [
        Podcast(
            id=str(r["id"]),
            title=r.get("title", ""),
            author=r.get("author", ""),
            description=r.get("description", ""),
            image_url=r.get("image"),
            feed_url=r.get("url", ""),
            website_url=r.get("link"),
            episode_count=r.get("episodeCount"),
        )
        for r in results
    ]


@router.get("/subscriptions")
async def list_subscriptions() -> list[Subscription]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM subscriptions ORDER BY subscribed_at DESC")
        # One query for every podcast's tags, rather than one per subscription.
        tag_rows = await db.execute_fetchall(
            """SELECT pt.podcast_id, t.id, t.name
               FROM podcast_tags pt JOIN tags t ON t.id = pt.tag_id
               ORDER BY t.name COLLATE NOCASE"""
        )
        by_podcast: dict[str, list[Tag]] = {}
        for r in tag_rows:
            by_podcast.setdefault(r["podcast_id"], []).append(Tag(id=r["id"], name=r["name"]))
        return [
            Subscription(
                **{k: v for k, v in dict(r).items() if k not in _SETTINGS_COLUMNS},
                tags=by_podcast.get(r["podcast_id"], []),
                settings=_settings_of(r),
            )
            for r in rows
        ]
    finally:
        await db.close()


@router.post("/subscriptions/{podcast_id}")
async def subscribe(podcast_id: str, feed_url: str, title: str, image_url: str = None):
    db = await get_db()
    try:
        await db.execute(
            """INSERT OR IGNORE INTO subscriptions
               (podcast_id, feed_url, title, image_url, subscribed_at, source)
               VALUES (?, ?, ?, ?, ?, 'podcast')""",
            (podcast_id, feed_url, title, image_url, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    finally:
        await db.close()
    return {"status": "subscribed"}


@router.delete("/subscriptions/{podcast_id}")
async def unsubscribe(podcast_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM subscriptions WHERE podcast_id = ?", (podcast_id,))
        await db.commit()
    finally:
        await db.close()
    return {"status": "unsubscribed"}


_SETTINGS_COLUMNS = (
    "playback_rate", "skip_intro", "skip_outro", "prefer_adfree", "auto_transcribe",
    "trim_silence", "normalize_volume",
)


def _settings_of(row) -> PodcastSettings:
    """Read a subscription row's playback preferences.

    NULL is carried through as `None` rather than defaulted, because "no
    opinion" and "1.0x" are different: the first leaves the player on whatever
    the listener last chose, the second overrides it on every episode.
    """
    data = dict(row)
    return PodcastSettings(
        playback_rate=data.get("playback_rate"),
        skip_intro=data.get("skip_intro"),
        skip_outro=data.get("skip_outro"),
        prefer_adfree=None if data.get("prefer_adfree") is None else bool(data["prefer_adfree"]),
        auto_transcribe=None if data.get("auto_transcribe") is None else bool(data["auto_transcribe"]),
        trim_silence=None if data.get("trim_silence") is None else bool(data["trim_silence"]),
        normalize_volume=None if data.get("normalize_volume") is None else bool(data["normalize_volume"]),
    )


@router.put("/{podcast_id}/settings")
async def set_podcast_settings(podcast_id: str, body: PodcastSettings) -> PodcastSettings:
    """Per-show playback preferences: speed, skipped intro/outro, ad-free, transcription.

    Sent whole, so clearing a field is expressible — a null means "no opinion
    again", which a patch of only-present-fields could not say.

    `trim_silence` and `normalize_volume` change the clean cut rather than the
    player, so they take effect the next time an episode is processed — the
    existing cut is not re-encoded.

    `auto_transcribe = false` is the one that earns its place beyond taste:
    transcription is the single stage that can cost money or pin a core for
    minutes, so being able to say "never for this show" is a load control, not
    a preference.
    """
    rate = body.playback_rate
    if rate is not None and not (0.5 <= rate <= 3.0):
        raise HTTPException(422, "Playback rate must be between 0.5 and 3.0")
    for field, value in (("skip_intro", body.skip_intro), ("skip_outro", body.skip_outro)):
        if value is not None and not (0 <= value <= 600):
            raise HTTPException(422, f"{field} must be between 0 and 600 seconds")

    db = await get_db()
    try:
        sub = await db.execute_fetchone(
            "SELECT 1 FROM subscriptions WHERE podcast_id = ?", (podcast_id,)
        )
        if not sub:
            raise HTTPException(404, "Not subscribed to this podcast")
        await db.execute(
            """UPDATE subscriptions
                  SET playback_rate = ?, skip_intro = ?, skip_outro = ?,
                      prefer_adfree = ?, auto_transcribe = ?,
                      trim_silence = ?, normalize_volume = ?
                WHERE podcast_id = ?""",
            (rate, body.skip_intro, body.skip_outro,
             None if body.prefer_adfree is None else int(body.prefer_adfree),
             None if body.auto_transcribe is None else int(body.auto_transcribe),
             None if body.trim_silence is None else int(body.trim_silence),
             None if body.normalize_volume is None else int(body.normalize_volume),
             podcast_id),
        )
        await db.commit()
        row = await db.execute_fetchone(
            "SELECT * FROM subscriptions WHERE podcast_id = ?", (podcast_id,)
        )
        return _settings_of(row)
    finally:
        await db.close()


@router.get("/suggestions")
async def get_suggestions() -> list[dict]:
    """Return non-dismissed podcast suggestions generated by the daily job."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM suggestions WHERE dismissed = 0 ORDER BY suggested_at DESC"
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.post("/suggestions/{suggestion_id}/dismiss")
async def dismiss_suggestion(suggestion_id: str):
    db = await get_db()
    try:
        await db.execute("UPDATE suggestions SET dismissed = 1 WHERE id = ?", (suggestion_id,))
        await db.commit()
    finally:
        await db.close()
    return {"status": "dismissed"}


# A refresh outlives the screen that started it. The work runs as a background
# task and its outcome is kept here, so navigating away and coming back can
# still show that one is running, or what the last one found. Single-user app,
# so module state is the right size for this — the same shape as the guards on
# transcription and channel imports.
_refresh: dict = {
    "running": False,
    "new": 0,
    "checked": 0,
    "failed": 0,
    "finished_at": None,
}


async def _refresh_all() -> None:
    """Check every subscription for new episodes. Never raises."""
    added = checked = failed = 0
    try:
        db = await get_db()
        try:
            # Same rule as the nightly job: a one-off video's channel is not a
            # subscription, so it is not polled.
            subs = await db.execute_fetchall(
                """SELECT podcast_id, feed_url, title FROM subscriptions
                   WHERE COALESCE(source, 'podcast') != 'youtube_video'"""
            )
        finally:
            await db.close()

        for sub in subs:
            podcast_id, feed_url = sub["podcast_id"], sub["feed_url"]
            checked += 1
            try:
                if podcast_id.startswith("yt-"):
                    from services import youtube_library
                    before = await _episode_count(podcast_id)
                    await youtube_library.sync_channel(
                        podcast_id[len("yt-"):], transcribe=False
                    )
                    added += max(0, await _episode_count(podcast_id) - before)
                else:
                    episodes = await rss.fetch_episodes(feed_url, podcast_id, limit=20)
                    added += await _insert_episodes(podcast_id, episodes)
            except Exception:
                # One dead feed must not stop the rest being checked.
                failed += 1
                continue
    finally:
        _refresh.update({
            "running": False,
            "new": added,
            "checked": checked,
            "failed": failed,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })


@router.post("/refresh")
async def refresh_subscriptions():
    """Start checking every subscription for new episodes.

    Returns immediately. The feed itself is a local query, so the refresh
    control used to re-read rows that could only change overnight — it looked
    instant because it did nothing. This actually goes out and asks, and
    because that takes seconds it runs in the background and is polled, so
    leaving the screen does not abandon it.

    Cheap on purpose: one RSS fetch per podcast, two requests per YouTube
    channel, and nothing transcribed or downloaded. The nightly job still owns
    that work.
    """
    if _refresh["running"]:
        return {"status": "already_running", **_refresh}
    _refresh.update({"running": True, "new": 0, "checked": 0, "failed": 0})
    asyncio.create_task(_refresh_all())
    return {"status": "started", **_refresh}


@router.get("/refresh/status")
async def refresh_status():
    """Whether a refresh is running, and what the last one found."""
    return {**_refresh}


async def _episode_count(podcast_id: str) -> int:
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT COUNT(*) AS n FROM episodes WHERE podcast_id = ?", (podcast_id,)
        )
        return row["n"] if row else 0
    finally:
        await db.close()


async def _insert_episodes(podcast_id: str, episodes: list) -> int:
    """Insert any episodes not already stored. Returns how many were new."""
    if not episodes:
        return 0
    db = await get_db()
    try:
        before = (await db.execute_fetchone(
            "SELECT COUNT(*) AS n FROM episodes WHERE podcast_id = ?", (podcast_id,)
        ))["n"]
        for ep in episodes:
            await db.execute(
                """INSERT OR IGNORE INTO episodes
                   (id, podcast_id, title, description, audio_url, duration_seconds,
                    published_at, image_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ep.id, ep.podcast_id, ep.title, ep.description, ep.audio_url,
                 ep.duration_seconds,
                 ep.published_at.isoformat() if ep.published_at else None, ep.image_url),
            )
        await db.commit()
        after = (await db.execute_fetchone(
            "SELECT COUNT(*) AS n FROM episodes WHERE podcast_id = ?", (podcast_id,)
        ))["n"]
        return after - before
    finally:
        await db.close()


@router.get("/feed")
async def get_feed(
    q: str = "",
    tag_id: str = "",
    podcast_id: str = "",
    status: str = "",
    unplayed: bool = False,
    min_minutes: int | None = Query(None, ge=0, le=1200),
    max_minutes: int | None = Query(None, ge=0, le=1200),
    sort: str = "newest",
    limit: int = 50,
) -> list[dict]:
    """Combined feed: episodes joined with their podcast, with counts and state.

    Optional filters, all combinable:
      q                      substring of the episode or podcast title
      tag_id                 only podcasts carrying this tag
      podcast_id             a single podcast
      status                 transcribed | distilled | bookmarked | adfree | downloaded
      unplayed               never opened, on any device
      min_minutes/max_minutes  how long you have
      sort                   newest | oldest | shortest | longest

    Filtering happens here rather than in the client because the feed is capped
    at `limit`: filtering an already-truncated page would silently hide matches
    that fall outside the most recent 50 episodes.

    `unplayed` moved server-side with the rest. It used to be applied to the
    page after it arrived, from this browser's localStorage, which meant an
    episode finished on the phone still counted as unplayed on the laptop and
    the filter quietly lied. Playback state has been server-side for a while;
    this makes the filter read it.
    """
    sql, params = episode_query.build(
        q=q, tag_id=tag_id, podcast_id=podcast_id, status=status,
        unplayed=unplayed, min_minutes=min_minutes, max_minutes=max_minutes,
        sort=sort, limit=limit,
    )
    db = await get_db()
    try:
        rows = await db.execute_fetchall(sql, params)
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.get("/inbox")
async def get_inbox():
    """How many episodes have arrived since you last looked.

    "Like email but for podcasts" only works if something tracks the read
    line. Counted from when the row appeared here rather than from its publish
    date, because a channel import backfills years of uploads in one go and
    none of those are news.
    """
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT value FROM app_state WHERE key = ?", (INBOX_SEEN_KEY,)
        )
        since = row["value"] if row else None
        if since:
            counted = await db.execute_fetchone(
                """SELECT COUNT(*) AS n FROM episodes e
                   JOIN subscriptions s ON s.podcast_id = e.podcast_id
                   WHERE COALESCE(e.created_at, e.published_at, '') > ?""",
                (since,),
            )
            new = counted["n"]
        else:
            # Never cleared: an existing library must not read as thousands
            # unread, so the first look establishes the line instead.
            new = 0
        return {"new": new, "since": since}
    finally:
        await db.close()


@router.post("/inbox/seen")
async def mark_inbox_seen():
    """Clear the inbox — everything currently here counts as seen."""
    now = datetime.now(timezone.utc).isoformat()
    db = await get_db()
    try:
        await db.execute(
            """INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (INBOX_SEEN_KEY, now, now),
        )
        await db.commit()
    finally:
        await db.close()
    return {"new": 0, "since": now}


@router.get("/opml")
async def export_opml():
    """Every subscription as an OPML file.

    The migration path every other podcast app advertises, and the only way
    this library survives a rebuild of the box without re-searching forty shows
    by hand. Served as a download rather than JSON because the consumer is
    another app, not this one.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT title, feed_url FROM subscriptions ORDER BY title COLLATE NOCASE"
        )
    finally:
        await db.close()
    body = opml.build([dict(r) for r in rows])
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(
        content=body,
        media_type="text/x-opml",
        headers={
            "Content-Disposition": f'attachment; filename="distillpod-{stamp}.opml"',
        },
    )


class OpmlImport(BaseModel):
    xml: str


@router.post("/opml")
async def import_opml(body: OpmlImport):
    """Subscribe to every feed in an OPML file.

    Episodes are not fetched here. An import can be forty feeds, and forty RSS
    fetches inside one request is a timeout; the nightly job and the refresh
    control both already know how to fill them in, and Refresh is one tap away.

    Feeds already subscribed are counted as skipped rather than re-added, so
    importing the same file twice changes nothing.
    """
    try:
        feeds = opml.parse(body.xml)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if not feeds:
        return {"added": 0, "skipped": 0, "found": 0, "titles": []}

    now = datetime.now(timezone.utc).isoformat()
    added, skipped = 0, 0
    titles: list[str] = []
    db = await get_db()
    try:
        for feed in feeds:
            existing = await db.execute_fetchone(
                "SELECT podcast_id FROM subscriptions WHERE feed_url = ?", (feed["feed_url"],)
            )
            if existing:
                skipped += 1
                continue
            # Derived from the feed URL rather than random, so re-importing the
            # same file cannot create a second row for one show even if the
            # subscription was deleted and restored in between.
            podcast_id = "opml-" + hashlib.sha1(feed["feed_url"].encode()).hexdigest()[:16]
            await db.execute(
                """INSERT OR IGNORE INTO subscriptions
                   (podcast_id, feed_url, title, image_url, subscribed_at, source)
                   VALUES (?, ?, ?, NULL, ?, 'podcast')""",
                (podcast_id, feed["feed_url"], feed["title"], now),
            )
            added += 1
            if len(titles) < 20:
                titles.append(feed["title"])
        await db.commit()
    finally:
        await db.close()
    return {"added": added, "skipped": skipped, "found": len(feeds), "titles": titles}


@router.get("/{podcast_id}/episodes")
async def get_episodes(podcast_id: str, refresh: bool = False, limit: int = 100, offset: int = 0) -> list[Episode]:
    db = await get_db()
    try:
        if refresh:
            row = await db.execute_fetchone(
                "SELECT feed_url FROM subscriptions WHERE podcast_id = ?", (podcast_id,)
            )
            if not row:
                raise HTTPException(404, "Podcast not subscribed")

            # A YouTube channel has no RSS to parse — its stored feed_url is the
            # /videos tab, an HTML page. Refreshing one has to go through the
            # channel sync, or the button on its page silently does nothing.
            if podcast_id.startswith("yt-"):
                # Holds this connection open across the sync, which opens its
                # own. Safe in WAL mode, and it avoids closing a connection the
                # enclosing `finally` will close again if the sync raises.
                from services import youtube_library
                try:
                    await youtube_library.sync_channel(
                        podcast_id[len("yt-"):], transcribe=False
                    )
                except Exception as exc:
                    raise HTTPException(502, f"Could not refresh the channel: {exc}")
                # Asking a channel's own page for its latest videos is asking to
                # follow it. Promote a one-off row rather than filling it with
                # uploads while its badge still says nobody subscribed.
                await db.execute(
                    """UPDATE subscriptions SET source = 'youtube_channel'
                       WHERE podcast_id = ? AND COALESCE(source, '') = 'youtube_video'""",
                    (podcast_id,),
                )
                await db.commit()
                episodes = []
            else:
                episodes = await rss.fetch_episodes(row["feed_url"], podcast_id)
            for ep in episodes:
                await db.execute(
                    """INSERT OR IGNORE INTO episodes
                       (id, podcast_id, title, description, audio_url, duration_seconds, published_at, image_url)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ep.id, ep.podcast_id, ep.title, ep.description, ep.audio_url,
                     ep.duration_seconds, ep.published_at.isoformat() if ep.published_at else None, ep.image_url),
                )
            await db.commit()

        # Bug 8: Paginate — cap at 500, default 100
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        rows = await db.execute_fetchall(
            "SELECT * FROM episodes WHERE podcast_id = ? ORDER BY published_at DESC LIMIT ? OFFSET ?",
            (podcast_id, limit, offset),
        )
        return [Episode(**dict(r)) for r in rows]
    finally:
        await db.close()

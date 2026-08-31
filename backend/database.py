import logging
import aiosqlite
import json
from pathlib import Path
from config import settings
from ids import is_url_safe, safe_episode_id

log = logging.getLogger(__name__)

DB_PATH = str(settings.db_path)

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    podcast_id   TEXT PRIMARY KEY,
    feed_url     TEXT NOT NULL,
    title        TEXT NOT NULL,
    image_url    TEXT,
    last_checked TEXT,
    subscribed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episodes (
    id                TEXT PRIMARY KEY,
    podcast_id        TEXT NOT NULL,
    title             TEXT NOT NULL,
    description       TEXT,
    audio_url         TEXT NOT NULL,
    duration_seconds  INTEGER,
    published_at      TEXT,
    image_url         TEXT,
    downloaded        INTEGER DEFAULT 0,
    local_path        TEXT,
    transcript_status TEXT DEFAULT 'none',
    FOREIGN KEY (podcast_id) REFERENCES subscriptions(podcast_id)
);

CREATE TABLE IF NOT EXISTS transcripts (
    episode_id  TEXT PRIMARY KEY,
    words_json  TEXT NOT NULL,       -- JSON array of {word, start, end}
    language    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suggestions (
    id                TEXT PRIMARY KEY,
    podcast_index_id  TEXT,
    title             TEXT NOT NULL,
    author            TEXT,
    description       TEXT,
    image_url         TEXT,
    feed_url          TEXT NOT NULL,
    reason            TEXT,
    suggested_at      TEXT NOT NULL,
    dismissed         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS gists (
    id             TEXT PRIMARY KEY,
    episode_id     TEXT NOT NULL,
    podcast_id     TEXT NOT NULL,
    episode_title  TEXT NOT NULL,
    podcast_title  TEXT NOT NULL,
    start_seconds  REAL NOT NULL,
    end_seconds    REAL NOT NULL,
    text           TEXT NOT NULL,
    summary        TEXT,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episode_chats (
    id         TEXT PRIMARY KEY,
    episode_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS researches (
    id          TEXT PRIMARY KEY,
    gist_id     TEXT NOT NULL,
    episode_id  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    file_path   TEXT,
    public_url  TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS chapters (
    id          TEXT PRIMARY KEY,
    episode_id  TEXT NOT NULL,
    title       TEXT NOT NULL,
    start_time  REAL NOT NULL,
    FOREIGN KEY (episode_id) REFERENCES episodes(id)
);

CREATE INDEX IF NOT EXISTS idx_chapters_episode ON chapters(episode_id, start_time);
"""

async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")

    # Convenience helpers (aiosqlite doesn't have these natively)
    async def _fetchone(sql, params=()):
        cursor = await db.execute(sql, params)
        return await cursor.fetchone()

    async def _fetchall(sql, params=()):
        cursor = await db.execute(sql, params)
        return await cursor.fetchall()

    db.execute_fetchone = _fetchone
    db.execute_fetchall = _fetchall
    return db

async def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
        # Migrations: add columns if they don't exist yet
        for alter in [
            'ALTER TABLE episodes ADD COLUMN adfree_path TEXT',
            'ALTER TABLE episodes ADD COLUMN ads_detected INTEGER',
            'ALTER TABLE episodes ADD COLUMN summary TEXT',
            'ALTER TABLE episodes ADD COLUMN chapters_status TEXT DEFAULT \'none\'',
        ]:
            try:
                await db.execute(alter)
            except Exception:
                pass
        await db.commit()

        await _migrate_unsafe_episode_ids(db)


# Tables whose episode_id points at episodes.id; all must move together.
_EPISODE_CHILD_TABLES = ("transcripts", "gists", "episode_chats", "researches", "chapters")


async def _migrate_unsafe_episode_ids(db) -> None:
    """Rewrite episode ids that cannot appear in a URL path.

    Ids come from the RSS <guid>, and some feeds use a URL there (Lex Fridman
    emits `https://lexfridman.com/?p=6506`). Those break `/player/:episodeId`
    in the SPA and `/player/episode/{id}` in the API, so such episodes were
    unreachable. Re-map them onto the hash `ids.safe_episode_id` now produces at
    ingest, carrying every child row across so transcripts, gists, chapters,
    chats and research keep pointing at the right episode.

    Already-safe ids are left alone, so this is a no-op on a healthy database.
    """
    rows = await db.execute_fetchall("SELECT id FROM episodes")
    remap = {
        r[0]: safe_episode_id(r[0])
        for r in rows
        if not is_url_safe(r[0])
    }
    if not remap:
        return

    for old_id, new_id in remap.items():
        # A collision would mean the safe id already exists; skip rather than
        # clobber an unrelated episode.
        existing = await db.execute_fetchall(
            "SELECT 1 FROM episodes WHERE id = ?", (new_id,)
        )
        if existing:
            continue
        await db.execute("UPDATE episodes SET id = ? WHERE id = ?", (new_id, old_id))
        for table in _EPISODE_CHILD_TABLES:
            await db.execute(
                f"UPDATE {table} SET episode_id = ? WHERE episode_id = ?", (new_id, old_id)
            )
    await db.commit()
    log.info("migrated %d episode id(s) to a URL-safe form", len(remap))

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
    -- When this row appeared, which is not when the episode was published: a
    -- channel import backfills years of uploads at once. The inbox counts what
    -- arrived, so it has to ask the first question, not the second.
    created_at        TEXT,
    FOREIGN KEY (podcast_id) REFERENCES subscriptions(podcast_id)
);

CREATE TABLE IF NOT EXISTS transcripts (
    episode_id  TEXT PRIMARY KEY,
    words_json  TEXT NOT NULL,       -- JSON array of {word, start, end}
    language    TEXT,
    created_at  TEXT NOT NULL
);

-- Where you are in each episode, and which ones you have finished.
-- Server-side rather than localStorage for the same reason tags are: an
-- episode you started on the phone should resume on the laptop. The client
-- still keeps a localStorage copy so a resume works offline and without
-- waiting on a round trip; this is the copy the devices agree on.
CREATE TABLE IF NOT EXISTS playback (
    episode_id  TEXT PRIMARY KEY,
    position    REAL NOT NULL DEFAULT 0,
    duration    REAL,
    played      INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

-- The model-written half of an episode's export note: key points, the things
-- it mentioned, and the argument map. Cached because it costs a model call
-- over a whole transcript and never changes once the transcript is final.
CREATE TABLE IF NOT EXISTS episode_notes (
    episode_id  TEXT PRIMARY KEY,
    extras_json TEXT NOT NULL,
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

-- User-assigned tags on subscriptions ("tech", "français", "long-form", ...).
-- Server-side rather than local storage so a library organised on the phone is
-- the same library on the laptop.
CREATE TABLE IF NOT EXISTS tags (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- Case-insensitive uniqueness: "Tech" and "tech" are one tag, not two.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_name ON tags(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS podcast_tags (
    podcast_id TEXT NOT NULL,
    tag_id     TEXT NOT NULL,
    PRIMARY KEY (podcast_id, tag_id),
    FOREIGN KEY (podcast_id) REFERENCES subscriptions(podcast_id),
    FOREIGN KEY (tag_id)     REFERENCES tags(id)
);
CREATE INDEX IF NOT EXISTS idx_podcast_tags_tag ON podcast_tags(tag_id);

-- Up Next, server-side. The client keeps a localStorage mirror so the queue
-- renders instantly and works offline, but this is the copy the devices agree
-- on — the same contract `playback` already has for positions. Without it a
-- queue built on the sofa did not exist on the phone in the car.
CREATE TABLE IF NOT EXISTS queue (
    episode_id TEXT PRIMARY KEY,
    position   INTEGER NOT NULL,
    added_at   TEXT NOT NULL,
    FOREIGN KEY (episode_id) REFERENCES episodes(id)
);
CREATE INDEX IF NOT EXISTS idx_queue_position ON queue(position);

-- A quote kept from the transcript. Deliberately not a distillation: a distill
-- costs a CLI round trip and ~30s, so it is a poor fit for the six things you
-- want to keep while driving. A bookmark costs an INSERT.
CREATE TABLE IF NOT EXISTS bookmarks (
    id            TEXT PRIMARY KEY,
    episode_id    TEXT NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds   REAL NOT NULL,
    text          TEXT NOT NULL,
    note          TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (episode_id) REFERENCES episodes(id)
);
CREATE INDEX IF NOT EXISTS idx_bookmarks_episode ON bookmarks(episode_id, start_seconds);

-- Playlists come in two kinds sharing one table, because to everything that
-- reads them — the list, the detail page, "play all" — they are the same thing:
-- an ordered set of episodes. A manual one stores its members in
-- playlist_items; a smart one stores a rule in rules_json and is resolved by a
-- query at read time, so it can never go stale.
CREATE TABLE IF NOT EXISTS playlists (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'manual',   -- manual | smart
    rules_json TEXT,                             -- smart only
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id TEXT NOT NULL,
    episode_id  TEXT NOT NULL,
    position    INTEGER NOT NULL,
    added_at    TEXT NOT NULL,
    PRIMARY KEY (playlist_id, episode_id),
    FOREIGN KEY (playlist_id) REFERENCES playlists(id)
);
CREATE INDEX IF NOT EXISTS idx_playlist_items ON playlist_items(playlist_id, position);

-- Meaning-based search over transcripts: one row per window of speech, with
-- its embedding. Keyword search cannot find "they talked about burning out" if
-- nobody said "burnout", and that is exactly the question people ask of a
-- library. Kept in a plain table rather than a vector extension: a few thousand
-- windows is a single pass over 12MB, and one fewer thing to install on a box
-- that already needs ffmpeg, yt-dlp and a JS runtime.
CREATE TABLE IF NOT EXISTS embeddings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  TEXT NOT NULL,
    start_time  REAL NOT NULL,
    end_time    REAL NOT NULL,
    text        TEXT NOT NULL,
    -- float32 little-endian, pre-normalised so a search is a dot product.
    vector      BLOB NOT NULL,
    model       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (episode_id) REFERENCES episodes(id)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_episode ON embeddings(episode_id);

-- Questions asked of the whole library, and the answers. One conversation
-- rather than one per episode: the point of it is that it crosses episodes.
CREATE TABLE IF NOT EXISTS library_chats (
    id             TEXT PRIMARY KEY,
    role           TEXT NOT NULL,          -- user | assistant
    content        TEXT NOT NULL,
    -- The passages an answer was built from, so a claim can be checked against
    -- the audio it came from rather than taken on trust.
    citations_json TEXT,
    created_at     TEXT NOT NULL
);

-- App-wide state with no natural home of its own: when the inbox was last
-- cleared, how long audio is kept. One row per key rather than a settings
-- table per feature.
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Full-text index over transcript text, for searching what was actually said.
-- `remove_diacritics 2` folds accents, so "retro" finds "rétro-ingénierie" —
-- essential when half the library is French and nobody types accents into a
-- search box. Kept as a plain table rather than an external-content one so a
-- transcript rewrite is a simple delete-then-insert.
CREATE VIRTUAL TABLE IF NOT EXISTS transcripts_fts USING fts5(
    episode_id UNINDEXED,
    text,
    tokenize = 'unicode61 remove_diacritics 2'
);
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
            'ALTER TABLE gists ADD COLUMN auto INTEGER DEFAULT 0',
            'ALTER TABLE subscriptions ADD COLUMN source TEXT',
            'ALTER TABLE episodes ADD COLUMN created_at TEXT',
            # Per-podcast playback preferences. NULL means "no opinion", which
            # is not the same as a stored default: it lets the player keep
            # using whatever the global control is set to.
            'ALTER TABLE subscriptions ADD COLUMN playback_rate REAL',
            'ALTER TABLE subscriptions ADD COLUMN skip_intro INTEGER',
            'ALTER TABLE subscriptions ADD COLUMN skip_outro INTEGER',
            'ALTER TABLE subscriptions ADD COLUMN prefer_adfree INTEGER',
            'ALTER TABLE subscriptions ADD COLUMN auto_transcribe INTEGER',
            'ALTER TABLE subscriptions ADD COLUMN trim_silence INTEGER',
            'ALTER TABLE subscriptions ADD COLUMN normalize_volume INTEGER',
            # The spans of the original that the clean cut keeps. Stored rather
            # than thrown away after the encode because it is the map between
            # two clocks: without it every timestamp — distills, bookmarks,
            # chapters, the read-along — is wrong by however much was removed
            # before that point. See services/timeline.py.
            'ALTER TABLE episodes ADD COLUMN processed_segments TEXT',
            'ALTER TABLE episodes ADD COLUMN trimmed_seconds REAL',
        ]:
            try:
                await db.execute(alter)
            except Exception:
                pass
        await db.commit()

        # Stamp `created_at` from one place rather than at six insert sites.
        # SQLite will not accept a non-constant column default in ALTER TABLE,
        # and every ingest path — RSS, YouTube channel sync, the nightly job —
        # has to be stamped or the inbox undercounts. A trigger is the one
        # mechanism that covers paths not written yet, including in scripts/.
        try:
            await db.execute(
                """CREATE TRIGGER IF NOT EXISTS episodes_stamp_created_at
                   AFTER INSERT ON episodes WHEN NEW.created_at IS NULL
                   BEGIN
                     UPDATE episodes
                        SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                      WHERE id = NEW.id;
                   END"""
            )
        except Exception:
            pass
        # An existing library must not read as one enormous unread inbox, so
        # rows that predate the column are dated by when they were published.
        await db.execute(
            "UPDATE episodes SET created_at = published_at WHERE created_at IS NULL"
        )
        await db.commit()

        await _backfill_subscription_source(db)
        await _migrate_unsafe_episode_ids(db)
        await _backfill_transcript_index(db)


async def _backfill_subscription_source(db) -> None:
    """Label existing subscriptions so the library can say what each one is.

    Rows created before this column existed cannot be asked what they were, so
    YouTube ones are separated by episode count: a channel sync imports up to
    15 at a time, whereas adding a single video creates exactly one. Three is
    comfortably clear of both. A row guessed wrong corrects itself the moment
    the channel is subscribed to again.
    """
    await db.execute(
        """UPDATE subscriptions SET source = CASE
             WHEN podcast_id LIKE 'yt-%' AND (
               SELECT COUNT(*) FROM episodes e WHERE e.podcast_id = subscriptions.podcast_id
             ) >= 3 THEN 'youtube_channel'
             WHEN podcast_id LIKE 'yt-%' THEN 'youtube_video'
             ELSE 'podcast'
           END
           WHERE source IS NULL"""
    )
    await db.commit()


async def index_transcript(db, episode_id: str, words_json: str) -> None:
    """(Re)index one transcript for full-text search.

    Called on every transcript write. Delete-then-insert so re-transcribing an
    episode replaces its index rather than duplicating every hit.
    """
    try:
        words = json.loads(words_json)
    except (TypeError, ValueError):
        return
    text = "".join(w.get("word", "") for w in words).strip()
    await db.execute("DELETE FROM transcripts_fts WHERE episode_id = ?", (episode_id,))
    if text:
        await db.execute(
            "INSERT INTO transcripts_fts (episode_id, text) VALUES (?, ?)",
            (episode_id, text),
        )


async def _backfill_transcript_index(db) -> None:
    """Index transcripts stored before the FTS table existed."""
    rows = await db.execute_fetchall(
        """SELECT t.episode_id, t.words_json FROM transcripts t
           WHERE t.episode_id NOT IN (SELECT episode_id FROM transcripts_fts)"""
    )
    if not rows:
        return
    # init_db uses a bare connection, so rows are tuples rather than sqlite3.Row.
    for episode_id, words_json in rows:
        await index_transcript(db, episode_id, words_json)
    await db.commit()
    log.info("indexed %d transcript(s) for full-text search", len(rows))


# Tables whose episode_id points at episodes.id; all must move together.
# transcripts_fts is included so a rename cannot orphan the search index. Today
# the id migration runs before the index is backfilled, but that ordering is not
# something a future change should have to remember.
_EPISODE_CHILD_TABLES = (
    "transcripts", "gists", "episode_chats", "researches", "chapters", "transcripts_fts",
    "playback", "episode_notes", "bookmarks", "queue", "playlist_items",
    "embeddings",
)


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
            try:
                await db.execute(
                    f"UPDATE {table} SET episode_id = ? WHERE episode_id = ?", (new_id, old_id)
                )
            except Exception as exc:
                # Several of these keep episode_id as a primary key, so an
                # orphan row already holding the new id makes this a constraint
                # violation. That must not abort init_db — the app would refuse
                # to start over one stale queue entry. Losing that row is the
                # right trade.
                log.warning(
                    "could not move %s rows for episode %s -> %s: %s",
                    table, old_id, new_id, exc,
                )
    await db.commit()
    log.info("migrated %d episode id(s) to a URL-safe form", len(remap))

"""Opening a database written by the previous version.

`init_db` runs on every start, against whatever is on the box. The tests
elsewhere all begin from the current schema, so they cannot see the case that
actually matters in production: a database with rows in it, written before these
columns and tables existed.

The pre-change schema is spelled out here rather than read from git, so this
keeps testing the upgrade even after the old definition is long gone.
"""
import asyncio
import sqlite3
import tempfile
from pathlib import Path

import pytest

# The schema as it stood before queue/bookmarks/playlists/app_state, with the
# handful of columns those features added left out.
OLD_SCHEMA = """
CREATE TABLE subscriptions (
    podcast_id   TEXT PRIMARY KEY,
    feed_url     TEXT NOT NULL,
    title        TEXT NOT NULL,
    image_url    TEXT,
    last_checked TEXT,
    subscribed_at TEXT NOT NULL,
    source       TEXT
);
CREATE TABLE episodes (
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
    adfree_path       TEXT,
    ads_detected      INTEGER,
    summary           TEXT,
    chapters_status   TEXT DEFAULT 'none'
);
CREATE TABLE transcripts (
    episode_id TEXT PRIMARY KEY, words_json TEXT NOT NULL,
    language TEXT, created_at TEXT NOT NULL
);
CREATE TABLE playback (
    episode_id TEXT PRIMARY KEY, position REAL NOT NULL DEFAULT 0,
    duration REAL, played INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
);
CREATE TABLE gists (
    id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, podcast_id TEXT NOT NULL,
    episode_title TEXT NOT NULL, podcast_title TEXT NOT NULL,
    start_seconds REAL NOT NULL, end_seconds REAL NOT NULL, text TEXT NOT NULL,
    summary TEXT, created_at TEXT NOT NULL, auto INTEGER DEFAULT 0
);
CREATE TABLE episode_chats (
    id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE researches (
    id TEXT PRIMARY KEY, gist_id TEXT NOT NULL, episode_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', file_path TEXT, public_url TEXT,
    error TEXT, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE TABLE chapters (
    id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, title TEXT NOT NULL,
    start_time REAL NOT NULL
);
CREATE TABLE episode_notes (
    episode_id TEXT PRIMARY KEY, extras_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE suggestions (
    id TEXT PRIMARY KEY, podcast_index_id TEXT, title TEXT NOT NULL, author TEXT,
    description TEXT, image_url TEXT, feed_url TEXT NOT NULL, reason TEXT,
    suggested_at TEXT NOT NULL, dismissed INTEGER DEFAULT 0
);
CREATE TABLE tags (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE podcast_tags (
    podcast_id TEXT NOT NULL, tag_id TEXT NOT NULL, PRIMARY KEY (podcast_id, tag_id)
);
CREATE VIRTUAL TABLE transcripts_fts USING fts5(
    episode_id UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2'
);

INSERT INTO subscriptions (podcast_id, feed_url, title, subscribed_at, source)
VALUES ('pod-old', 'https://example.com/old.xml', 'Long-running Show',
        '2025-01-01T00:00:00', 'podcast');

INSERT INTO episodes (id, podcast_id, title, audio_url, published_at, transcript_status)
VALUES ('ep-old-1', 'pod-old', 'From last year', 'https://a/1.mp3',
        '2025-06-01T00:00:00', 'done'),
       ('ep-old-2', 'pod-old', 'No publish date either', 'https://a/2.mp3', NULL, 'none');

INSERT INTO playback (episode_id, position, played, updated_at)
VALUES ('ep-old-1', 450.0, 1, '2025-06-02T00:00:00');
"""


@pytest.fixture
def old_db(monkeypatch):
    """A populated database in the pre-change shape, with init_db applied."""
    import database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()

    monkeypatch.setattr(database, "DB_PATH", path)
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(database.init_db())
    yield path
    Path(path).unlink(missing_ok=True)


def rows(db_path, sql, params=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class TestUpgrade:

    def test_new_tables_appear(self, old_db):
        names = {r[0] for r in rows(old_db, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"queue", "bookmarks", "playlists", "playlist_items", "app_state"} <= names

    def test_new_subscription_columns_appear(self, old_db):
        cols = {r[1] for r in rows(old_db, "PRAGMA table_info(subscriptions)")}
        assert {"playback_rate", "skip_intro", "skip_outro",
                "prefer_adfree", "auto_transcribe"} <= cols

    def test_existing_rows_survive(self, old_db):
        assert rows(old_db, "SELECT title FROM episodes ORDER BY id") == [
            ("From last year",), ("No publish date either",)]
        assert rows(old_db, "SELECT position, played FROM playback") == [(450.0, 1)]

    def test_an_existing_library_is_not_one_giant_unread_inbox(self, old_db):
        """`created_at` is backfilled from the publish date, so the first look
        at an upgraded library does not report every episode as new."""
        got = dict(rows(old_db, "SELECT id, created_at FROM episodes"))
        assert got["ep-old-1"] == "2025-06-01T00:00:00"
        # Nothing to date it by, which the inbox query treats as "not new".
        assert got["ep-old-2"] is None

    def test_the_stamp_trigger_is_installed(self, old_db):
        conn = sqlite3.connect(old_db)
        try:
            conn.execute(
                """INSERT INTO episodes (id, podcast_id, title, audio_url)
                   VALUES ('ep-fresh', 'pod-old', 'Arrived after the upgrade', 'https://a/3.mp3')"""
            )
            conn.commit()
            stamped = conn.execute(
                "SELECT created_at FROM episodes WHERE id = 'ep-fresh'").fetchone()[0]
        finally:
            conn.close()
        assert stamped, "the trigger did not stamp a row inserted after the upgrade"

    def test_running_it_again_changes_nothing(self, old_db):
        """The app runs init_db on every start."""
        import database
        before = rows(old_db, "SELECT id, created_at FROM episodes ORDER BY id")
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(database.init_db())
        assert rows(old_db, "SELECT id, created_at FROM episodes ORDER BY id") == before

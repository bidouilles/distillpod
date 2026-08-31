"""
Episode ids must be usable as a URL path segment.

Some feeds put a URL in <guid> — Lex Fridman emits `https://lexfridman.com/?p=6506`
— and that id is a path segment in both `/player/:episodeId` (SPA) and
`/player/episode/{id}` (API). An unescaped `/` or `?` makes the episode
unreachable: the route matches only up to the first slash.
"""
import pytest

from ids import is_url_safe, safe_episode_id

UNSAFE = [
    "https://lexfridman.com/?p=6506",
    "http://example.com/ep/1",
    "tag:soundcloud,2010:tracks/123?x=1",
    "with space",
    "has#fragment",
    "a/b",
    "q?uery",
    "pct%20encoded",
]

SAFE = [
    "6a4769db04fac73b2495101d",
    "ep_demo_001",
    "abc-123",
    "a.b~c_d-e",
]


class TestIsUrlSafe:

    @pytest.mark.parametrize("guid", SAFE)
    def test_accepts_safe(self, guid):
        assert is_url_safe(guid)

    @pytest.mark.parametrize("guid", UNSAFE)
    def test_rejects_unsafe(self, guid):
        assert not is_url_safe(guid)

    def test_rejects_empty(self):
        assert not is_url_safe("")

    def test_rejects_overlong(self):
        assert not is_url_safe("a" * 129)


class TestSafeEpisodeId:

    @pytest.mark.parametrize("guid", SAFE)
    def test_safe_guid_passes_through(self, guid):
        """Ids minted before this existed must keep working, or their
        transcripts, gists and chapters would be orphaned."""
        assert safe_episode_id(guid) == guid

    @pytest.mark.parametrize("guid", UNSAFE)
    def test_unsafe_guid_becomes_safe(self, guid):
        out = safe_episode_id(guid)
        assert is_url_safe(out)
        assert out != guid

    def test_deterministic(self):
        """Re-syncing a feed must map an episode onto the same row, not a new one."""
        g = "https://lexfridman.com/?p=6506"
        assert safe_episode_id(g) == safe_episode_id(g)

    def test_distinct_guids_distinct_ids(self):
        ids = {safe_episode_id(f"https://lexfridman.com/?p={n}") for n in range(500)}
        assert len(ids) == 500

    def test_lex_fridman_case(self):
        assert safe_episode_id("https://lexfridman.com/?p=6506") == "ea85f263568a49fc064bb10e298a7467"


# ── Migration ─────────────────────────────────────────────────────────────────

class TestMigration:
    """The migration rewrites primary keys, so child rows must follow or a
    user loses transcripts, gists, chapters, chats and research."""

    @pytest.mark.asyncio
    async def test_rewrites_unsafe_ids_and_carries_children(self, tmp_path, monkeypatch):
        import aiosqlite
        import database

        db_file = tmp_path / "t.db"
        monkeypatch.setattr(database, "DB_PATH", str(db_file))

        async with aiosqlite.connect(str(db_file)) as db:
            await db.executescript(database.SCHEMA)
            bad = "https://lexfridman.com/?p=6506"
            good = "6a4769db04fac73b2495101d"
            await db.execute(
                "INSERT INTO subscriptions (podcast_id, feed_url, title, subscribed_at)"
                " VALUES ('p','u','t','2026-01-01')")
            for eid in (bad, good):
                await db.execute(
                    "INSERT INTO episodes (id, podcast_id, title, audio_url) VALUES (?,'p','t','u')",
                    (eid,))
                await db.execute(
                    "INSERT INTO transcripts (episode_id, words_json, created_at)"
                    " VALUES (?,'[]','2026-01-01')", (eid,))
                await db.execute(
                    "INSERT INTO chapters (id, episode_id, title, start_time)"
                    " VALUES (?,?,'c',0.0)", (f"ch-{eid}", eid))
                await db.execute(
                    "INSERT INTO transcripts_fts (episode_id, text) VALUES (?, 'hello world')",
                    (eid,))
            await db.commit()

            await database._migrate_unsafe_episode_ids(db)

            new_id = safe_episode_id(bad)
            ids = [r[0] for r in await db.execute_fetchall("SELECT id FROM episodes")]
            assert bad not in ids
            assert new_id in ids
            assert good in ids, "already-safe id must be left alone"

            for table in ("transcripts", "chapters", "transcripts_fts"):
                moved = await db.execute_fetchall(
                    f"SELECT COUNT(*) FROM {table} WHERE episode_id = ?", (new_id,))
                orphan = await db.execute_fetchall(
                    f"SELECT COUNT(*) FROM {table} WHERE episode_id = ?", (bad,))
                assert moved[0][0] == 1, f"{table} row did not follow the episode"
                assert orphan[0][0] == 0, f"{table} row left orphaned"

    @pytest.mark.asyncio
    async def test_noop_on_clean_database(self, tmp_path, monkeypatch):
        import aiosqlite
        import database

        db_file = tmp_path / "t2.db"
        monkeypatch.setattr(database, "DB_PATH", str(db_file))
        async with aiosqlite.connect(str(db_file)) as db:
            await db.executescript(database.SCHEMA)
            await db.execute(
                "INSERT INTO subscriptions (podcast_id, feed_url, title, subscribed_at)"
                " VALUES ('p','u','t','2026-01-01')")
            await db.execute(
                "INSERT INTO episodes (id, podcast_id, title, audio_url) VALUES ('ok','p','t','u')")
            await db.commit()
            await database._migrate_unsafe_episode_ids(db)
            ids = [r[0] for r in await db.execute_fetchall("SELECT id FROM episodes")]
            assert ids == ["ok"]

    @pytest.mark.asyncio
    async def test_skips_on_collision(self, tmp_path, monkeypatch):
        """If the hashed id somehow already exists, leave both rows alone rather
        than clobbering an unrelated episode."""
        import aiosqlite
        import database

        db_file = tmp_path / "t3.db"
        monkeypatch.setattr(database, "DB_PATH", str(db_file))
        bad = "https://lexfridman.com/?p=6506"
        clash = safe_episode_id(bad)
        async with aiosqlite.connect(str(db_file)) as db:
            await db.executescript(database.SCHEMA)
            await db.execute(
                "INSERT INTO subscriptions (podcast_id, feed_url, title, subscribed_at)"
                " VALUES ('p','u','t','2026-01-01')")
            for eid in (bad, clash):
                await db.execute(
                    "INSERT INTO episodes (id, podcast_id, title, audio_url) VALUES (?,'p','t','u')",
                    (eid,))
            await db.commit()
            await database._migrate_unsafe_episode_ids(db)
            ids = sorted(r[0] for r in await db.execute_fetchall("SELECT id FROM episodes"))
            assert ids == sorted([bad, clash]), "collision must not drop or overwrite a row"

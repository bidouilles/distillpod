"""Meaning-based search over transcripts.

The embedding backend is stubbed with a deterministic bag-of-words vector, so
these tests are about the parts that are ours: how a transcript is cut into
windows, that a window's timestamp points at real audio, that a model change
cannot corrupt a search, and — most of all — that the whole feature is optional.
A library with no embedding backend has to keep working exactly as it did.
"""
import json
import sqlite3

import pytest
import pytest_asyncio

from conftest import PODCAST_ID
from services import embeddings, librarian, semantic_index

pytestmark = pytest.mark.asyncio


VOCAB = ["exhausted", "sleep", "evals", "spreadsheet", "oven", "dough"]


def fake_vector(text: str) -> list[float]:
    """A tiny bag-of-words embedding: enough for 'closer in meaning' to mean
    something, with no model and no network."""
    lowered = text.lower()
    return embeddings.normalise([1.0 + lowered.count(word) for word in VOCAB])


@pytest.fixture
def stub_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "engine", lambda: "stub")
    monkeypatch.setattr(embeddings, "available", lambda: True)
    monkeypatch.setattr(embeddings, "model_name", lambda: "stub-v1")
    monkeypatch.setattr(embeddings, "embed", lambda texts: [fake_vector(t) for t in texts])
    return embeddings


EPISODES = {
    "ep-tired": ("Working too much",
                 "He was exhausted for a year and could not sleep. " * 12),
    "ep-evals": ("Evaluating models",
                 "Our evals are vibes plus a spreadsheet, read every week. " * 12),
    "ep-bread": ("Baking",
                 "The dough wants a hotter oven and steam for ten minutes. " * 12),
}


def seed(db_path):
    conn = sqlite3.connect(db_path)
    for episode_id, (title, text) in EPISODES.items():
        words, t = [], 0.0
        for w in text.split():
            words.append({"word": " " + w, "start": round(t, 2), "end": round(t + 0.4, 2)})
            t += 0.5
        conn.execute(
            """INSERT OR REPLACE INTO episodes
               (id, podcast_id, title, audio_url, published_at, transcript_status)
               VALUES (?, ?, ?, 'https://a/x.mp3', '2026-02-01T00:00:00', 'done')""",
            (episode_id, PODCAST_ID, title),
        )
        conn.execute(
            """INSERT OR REPLACE INTO transcripts (episode_id, words_json, language, created_at)
               VALUES (?, ?, 'en', '2026-02-01T00:00:00')""",
            (episode_id, json.dumps(words)),
        )
        conn.execute("DELETE FROM transcripts_fts WHERE episode_id = ?", (episode_id,))
        conn.execute("INSERT INTO transcripts_fts (episode_id, text) VALUES (?, ?)",
                     (episode_id, text))
    conn.commit()
    conn.close()


@pytest.fixture
def library(tmp_db, monkeypatch):
    import database
    monkeypatch.setattr(database, "DB_PATH", tmp_db)
    seed(tmp_db)
    return tmp_db


async def open_db():
    from database import get_db
    return await get_db()


class TestWindows:

    def test_nothing_from_nothing(self):
        assert semantic_index.windows([]) == []

    def test_windows_carry_real_timestamps(self):
        words = [{"word": f" w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(400)]
        out = semantic_index.windows(words)
        assert out[0]["start"] == 0.0
        assert all(w["end"] > w["start"] for w in out)
        assert all(w["start"] <= 200.0 for w in out)

    def test_windows_overlap(self):
        """A thought that spans a boundary has to be whole in some window."""
        words = [{"word": f" w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(400)]
        out = semantic_index.windows(words)
        assert out[1]["start"] < out[0]["end"], "windows do not overlap"

    def test_a_short_episode_still_gets_one_window(self):
        words = [{"word": " hello", "start": 0.0, "end": 0.4},
                 {"word": " there", "start": 0.5, "end": 0.9}]
        assert len(semantic_index.windows(words)) == 1

    def test_five_hours_is_walked_not_rescanned(self):
        """Filtering the word list per window is twelve million comparisons on a
        long episode; this is the regression guard for that."""
        words = [{"word": f" w{i}", "start": i * 0.2, "end": i * 0.2 + 0.1}
                 for i in range(90_000)]
        out = semantic_index.windows(words)
        assert len(out) == 400
        assert out[-1]["end"] == pytest.approx(18_000.0, abs=1.0)


class TestIndexing:

    async def test_indexes_an_episode(self, library, stub_embeddings):
        db = await open_db()
        try:
            stored = await semantic_index.index_episode(
                db, "ep-tired",
                (await db.execute_fetchone(
                    "SELECT words_json FROM transcripts WHERE episode_id = 'ep-tired'"
                ))["words_json"],
            )
        finally:
            await db.close()
        assert stored > 0

    async def test_reindexing_replaces_rather_than_duplicates(self, library, stub_embeddings):
        """Re-transcribing an episode must not leave windows pointing at times
        that have moved."""
        db = await open_db()
        try:
            words = (await db.execute_fetchone(
                "SELECT words_json FROM transcripts WHERE episode_id = 'ep-tired'"))["words_json"]
            first = await semantic_index.index_episode(db, "ep-tired", words)
            await semantic_index.index_episode(db, "ep-tired", words)
            total = (await db.execute_fetchone(
                "SELECT COUNT(*) AS n FROM embeddings WHERE episode_id = 'ep-tired'"))["n"]
        finally:
            await db.close()
        assert total == first

    async def test_a_failed_embedding_stores_nothing(self, library, monkeypatch):
        """Half an episode looks indexed and answers worse than none of it."""
        monkeypatch.setattr(embeddings, "available", lambda: True)
        monkeypatch.setattr(embeddings, "model_name", lambda: "stub-v1")
        monkeypatch.setattr(embeddings, "embed", lambda texts: None)
        db = await open_db()
        try:
            words = (await db.execute_fetchone(
                "SELECT words_json FROM transcripts WHERE episode_id = 'ep-tired'"))["words_json"]
            assert await semantic_index.index_episode(db, "ep-tired", words) == 0
            assert (await db.execute_fetchone("SELECT COUNT(*) AS n FROM embeddings"))["n"] == 0
        finally:
            await db.close()

    async def test_without_a_backend_it_does_nothing(self, library, monkeypatch):
        monkeypatch.setattr(embeddings, "available", lambda: False)
        db = await open_db()
        try:
            words = (await db.execute_fetchone(
                "SELECT words_json FROM transcripts WHERE episode_id = 'ep-tired'"))["words_json"]
            assert await semantic_index.index_episode(db, "ep-tired", words) == 0
        finally:
            await db.close()

    async def test_run_indexes_everything_pending(self, library, stub_embeddings, monkeypatch):
        monkeypatch.setattr(semantic_index, "EPISODE_SPACING_SECONDS", 0)
        result = await semantic_index.run()
        assert result["indexed"] == 3 and result["failed"] == 0
        coverage = await semantic_index.coverage()
        assert coverage["pending"] == 0 and coverage["indexed"] == 3
        assert coverage["windows"] > 3

    async def test_the_run_tally_cannot_masquerade_as_coverage(
        self, library, stub_embeddings, monkeypatch,
    ):
        """Both use the word "indexed" for different things — the run means
        "episodes done just now", coverage means "episodes in the index" — and
        merging the two let a finished run report an index that does not exist."""
        monkeypatch.setattr(semantic_index, "EPISODE_SPACING_SECONDS", 0)
        await semantic_index.run()
        semantic_index._state["indexed"] = 99          # as a stale run would leave it
        coverage = await semantic_index.coverage()
        assert coverage["indexed"] == 3
        assert coverage["job"]["indexed"] == 99

    async def test_coverage_reports_an_absent_backend(self, library):
        coverage = await semantic_index.coverage()
        assert coverage["engine"] == "off"
        assert coverage["indexed"] == 0
        assert coverage["transcribed"] == 3


class TestSearch:

    @pytest_asyncio.fixture
    async def indexed(self, library, stub_embeddings, monkeypatch):
        monkeypatch.setattr(semantic_index, "EPISODE_SPACING_SECONDS", 0)
        await semantic_index.run()
        return library

    async def test_finds_the_episode_about_the_subject(self, indexed):
        db = await open_db()
        try:
            hits = await semantic_index.search(db, "sleep and exhaustion", limit=3)
        finally:
            await db.close()
        assert hits[0]["episode_id"] == "ep-tired"
        assert hits[0]["start"] >= 0
        assert hits[0]["podcast_title"] == "Test Podcast"

    async def test_scores_are_ordered(self, indexed):
        db = await open_db()
        try:
            hits = await semantic_index.search(db, "evals spreadsheet", limit=5)
        finally:
            await db.close()
        assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)

    async def test_no_query_no_search(self, indexed):
        db = await open_db()
        try:
            assert await semantic_index.search(db, "   ") == []
        finally:
            await db.close()

    async def test_vectors_from_another_model_are_ignored(self, indexed, monkeypatch):
        """Changing the embedding model leaves rows of a different width behind;
        comparing those is meaningless, not merely wrong."""
        db = await open_db()
        try:
            await db.execute(
                "UPDATE embeddings SET vector = ? WHERE episode_id = 'ep-tired'",
                (embeddings.pack([0.5] * 3),),
            )
            await db.commit()
            hits = await semantic_index.search(db, "sleep and exhaustion", limit=5)
        finally:
            await db.close()
        assert "ep-tired" not in [h["episode_id"] for h in hits]

    async def test_without_a_backend_search_is_empty_not_broken(self, indexed, monkeypatch):
        monkeypatch.setattr(embeddings, "available", lambda: False)
        db = await open_db()
        try:
            assert await semantic_index.search(db, "sleep") == []
        finally:
            await db.close()


class TestFusion:
    """The two retrieval paths together, which is what Ask actually uses."""

    @pytest_asyncio.fixture
    async def indexed(self, library, stub_embeddings, monkeypatch):
        monkeypatch.setattr(semantic_index, "EPISODE_SPACING_SECONDS", 0)
        await semantic_index.run()
        return library

    async def test_meaning_finds_what_keywords_cannot(self, indexed):
        """The reason this exists: the planned keyword misses entirely — nobody
        said "burnout" — and the passage is still found.

        The stubbed embedding is a bag of words, so it cannot know that
        "burnout" means "exhausted"; the question here carries a word it can
        place. What is under test is that a keyword miss no longer means no
        answer, which is the behaviour that changed."""
        db = await open_db()
        try:
            keyword_only = await librarian.gather(db, ["burnout"])
            fused = await librarian.gather(
                db, ["burnout"], question="who could not sleep for a year?")
        finally:
            await db.close()
        assert keyword_only == [], "the keyword path should find nothing here"
        assert fused and fused[0]["episode_id"] == "ep-tired"

    async def test_keyword_hits_still_come_through(self, indexed):
        db = await open_db()
        try:
            passages = await librarian.gather(db, ["spreadsheet"], question="baking bread")
        finally:
            await db.close()
        assert "ep-evals" in [p["episode_id"] for p in passages]

    async def test_a_passage_both_paths_like_ranks_first(self, indexed):
        db = await open_db()
        try:
            passages = await librarian.gather(
                db, ["exhausted", "sleep"], question="exhausted and unable to sleep")
        finally:
            await db.close()
        assert passages[0]["episode_id"] == "ep-tired"

    async def test_fusion_is_a_no_op_without_a_backend(self, library):
        """Ask shipped keyword-only and has to keep working that way."""
        db = await open_db()
        try:
            passages = await librarian.gather(db, ["spreadsheet"], question="anything at all")
        finally:
            await db.close()
        assert [p["episode_id"] for p in passages] == ["ep-evals"]


class TestEndpoints:

    async def test_status_reports_off_by_default(self, client, library):
        r = await client.get("/search/index")
        assert r.status_code == 200
        assert r.json()["engine"] == "off"
        assert r.json()["pending"] == 3

    async def test_building_without_a_backend_says_so(self, client, library):
        r = await client.post("/search/index")
        assert r.json()["status"] == "unavailable"

    async def test_build_and_report(self, client, library, stub_embeddings, monkeypatch):
        monkeypatch.setattr(semantic_index, "EPISODE_SPACING_SECONDS", 0)
        assert (await client.post("/search/index")).json()["status"] == "started"
        # The task runs in the background; give the loop a turn to finish it.
        import asyncio
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not semantic_index.status()["running"]:
                break
        assert (await client.get("/search/index")).json()["pending"] == 0

    async def test_stop_is_polite(self, client):
        assert (await client.post("/search/index/stop")).json()["status"] == "stopping"


class TestOptIn:
    """The automatic backend uses the Mistral key that is usually already there
    for Voxtral, so an upgrade must not silently start uploading transcript text.
    The first index is a deliberate press; after that it maintains itself."""

    async def test_a_fresh_library_has_not_opted_in(self, library, stub_embeddings):
        db = await open_db()
        try:
            assert await semantic_index.opted_in(db) is False
        finally:
            await db.close()

    async def test_an_explicit_backend_is_the_decision(self, library, stub_embeddings, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "embed_backend", "mistral")
        db = await open_db()
        try:
            assert await semantic_index.opted_in(db) is True
        finally:
            await db.close()

    async def test_one_indexed_episode_counts_as_opting_in(
        self, library, stub_embeddings, monkeypatch,
    ):
        monkeypatch.setattr(semantic_index, "EPISODE_SPACING_SECONDS", 0)
        await semantic_index.run()
        db = await open_db()
        try:
            assert await semantic_index.opted_in(db) is True
        finally:
            await db.close()

    async def test_the_nightly_job_does_not_index_unasked(
        self, library, stub_embeddings, monkeypatch,
    ):
        monkeypatch.setattr(semantic_index, "EPISODE_SPACING_SECONDS", 0)
        await semantic_index.run(require_opt_in=True)
        coverage = await semantic_index.coverage()
        assert coverage["indexed"] == 0, "indexed without being asked to"

    async def test_the_nightly_job_keeps_an_opted_in_library_level(
        self, library, stub_embeddings, monkeypatch,
    ):
        monkeypatch.setattr(semantic_index, "EPISODE_SPACING_SECONDS", 0)
        await semantic_index.run()                       # the deliberate press
        db = await open_db()
        try:
            await db.execute("DELETE FROM embeddings WHERE episode_id = 'ep-bread'")
            await db.commit()
        finally:
            await db.close()
        await semantic_index.run(require_opt_in=True)     # unattended, now allowed
        assert (await semantic_index.coverage())["indexed"] == 3

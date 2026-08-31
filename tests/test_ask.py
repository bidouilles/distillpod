"""Asking the whole library a question.

Retrieval is what makes this work or not, so most of these are about which
passages come back and in what order. The model calls are stubbed: what matters
here is that the right context reaches the prompt, that citations map back to
real moments in real episodes, and that a thin library produces an honest answer
rather than an invented one.
"""
import json
import sqlite3

import pytest

from conftest import PODCAST_ID
from services import librarian

pytestmark = pytest.mark.asyncio


EPISODES = {
    "ep-evals": (
        "Episode about evaluation",
        "Everyone asks how we evaluate the models. The honest answer is that "
        "evals are mostly vibes plus a spreadsheet. We hold out a set of hard "
        "cases and we read the failures ourselves, every week, which nobody "
        "enjoys but it is the only thing that has caught real regressions.",
    ),
    "ep-burnout": (
        "Episode about working too much",
        "He talked about being exhausted for a whole year, and how the team "
        "kept shipping anyway. Nobody used the word burnout at the time but "
        "that is plainly what it was, and the recovery took longer than the "
        "crunch did.",
    ),
    "ep-unrelated": (
        "Episode about sourdough",
        "The starter needs feeding twice a day in summer, and the loaf wants a "
        "hotter oven than most people use. Steam for the first ten minutes.",
    ),
}


def seed_transcripts(db_path):
    """Three episodes with real word-level transcripts and an FTS index."""
    conn = sqlite3.connect(db_path)
    for episode_id, (title, text) in EPISODES.items():
        words, t = [], 0.0
        for w in text.split():
            words.append({"word": " " + w, "start": round(t, 2), "end": round(t + 0.4, 2)})
            t += 0.5
        conn.execute(
            """INSERT OR REPLACE INTO episodes
               (id, podcast_id, title, audio_url, duration_seconds, published_at,
                transcript_status)
               VALUES (?, ?, ?, 'https://a/x.mp3', 1800, '2026-02-01T00:00:00', 'done')""",
            (episode_id, PODCAST_ID, title),
        )
        conn.execute(
            """INSERT OR REPLACE INTO transcripts (episode_id, words_json, language, created_at)
               VALUES (?, ?, 'en', '2026-02-01T00:00:00')""",
            (episode_id, json.dumps(words)),
        )
        conn.execute("DELETE FROM transcripts_fts WHERE episode_id = ?", (episode_id,))
        conn.execute(
            "INSERT INTO transcripts_fts (episode_id, text) VALUES (?, ?)",
            (episode_id, text),
        )
    conn.commit()
    conn.close()


@pytest.fixture
def library(tmp_db, monkeypatch):
    """A small transcribed library, and connections pointed at it.

    The `client` fixture patches DB_PATH for requests; these tests also call
    into the service directly, so the patch has to hold outside a request too.
    """
    import database
    monkeypatch.setattr(database, "DB_PATH", tmp_db)
    seed_transcripts(tmp_db)
    return tmp_db


async def open_db():
    from database import get_db
    return await get_db()


class TestGather:

    async def test_finds_the_episode_that_talks_about_it(self, library):
        db = await open_db()
        try:
            passages = await librarian.gather(db, ["evals"])
        finally:
            await db.close()
        assert [p["episode_id"] for p in passages] == ["ep-evals"]
        assert "evals are mostly vibes" in passages[0]["text"]

    async def test_carries_the_timestamp_and_the_show(self, library):
        db = await open_db()
        try:
            passages = await librarian.gather(db, ["starter feeding"])
        finally:
            await db.close()
        assert passages[0]["podcast_title"] == "Test Podcast"
        assert passages[0]["episode_title"] == "Episode about sourdough"
        assert passages[0]["start"] >= 0

    async def test_it_searches_what_was_said_not_the_titles(self, library):
        """The index holds transcript text only, which is the contract: this
        answers from what people said. "sourdough" appears in a title and never
        in the audio, so it finds nothing — worth pinning, because the honest
        "not in your episodes" answer depends on it."""
        db = await open_db()
        try:
            assert await librarian.gather(db, ["sourdough"]) == []
        finally:
            await db.close()

    async def test_a_passage_two_searches_agree_on_ranks_first(self, library):
        """The cheap stand-in for relevance: independent agreement."""
        db = await open_db()
        try:
            passages = await librarian.gather(db, ["exhausted", "burnout", "oven"])
        finally:
            await db.close()
        assert passages[0]["episode_id"] == "ep-burnout"

    async def test_a_search_matching_nothing_is_not_an_error(self, library):
        db = await open_db()
        try:
            assert await librarian.gather(db, ["helicopter parenting"]) == []
        finally:
            await db.close()

    async def test_punctuation_cannot_break_the_query(self, library):
        """A question mark reaching FTS5 raw would raise rather than search."""
        db = await open_db()
        try:
            passages = await librarian.gather(db, ['evals?', '"quoted"', "(paren"])
        finally:
            await db.close()
        assert [p["episode_id"] for p in passages] == ["ep-evals"]

    async def test_the_context_is_capped(self, library, monkeypatch):
        monkeypatch.setattr(librarian, "MAX_CONTEXT_CHARS", 200)
        db = await open_db()
        try:
            passages = await librarian.gather(db, ["evals", "burnout", "oven"])
        finally:
            await db.close()
        assert sum(len(p["text"]) for p in passages) <= 200 + len(passages[0]["text"])

    async def test_the_same_moment_found_twice_is_one_passage(self, library):
        db = await open_db()
        try:
            passages = await librarian.gather(db, ["evals", "evaluate"])
        finally:
            await db.close()
        starts = [(p["episode_id"], p["start"]) for p in passages]
        assert len(starts) == len(set(starts))


class TestPlanQueries:

    async def test_uses_the_model_plan(self, monkeypatch):
        async def fake(prompt, **kw):
            return {"queries": ["evals", "held out set"]}
        monkeypatch.setattr(librarian.llm, "arun_json", fake)
        assert await librarian.plan_queries("how do they evaluate models?") == [
            "evals", "held out set"]

    async def test_a_failed_plan_falls_back_to_the_question(self, monkeypatch):
        """Worse searching beats no answer."""
        async def fake(prompt, **kw):
            return None
        monkeypatch.setattr(librarian.llm, "arun_json", fake)
        assert await librarian.plan_queries("burnout") == ["burnout"]

    async def test_follow_ups_carry_the_subject(self, monkeypatch):
        seen = {}

        async def fake(prompt, **kw):
            seen["prompt"] = prompt
            return {"queries": ["x"]}
        monkeypatch.setattr(librarian.llm, "arun_json", fake)
        await librarian.plan_queries("what about the cost?", [
            {"role": "user", "content": "what did they say about evals"},
            {"role": "assistant", "content": "They read failures weekly [1]."},
        ])
        assert "evals" in seen["prompt"], "a follow-up searched for the pronoun"


class TestPrompt:

    def test_passages_are_numbered_with_episode_and_time(self):
        prompt = librarian.build_prompt("q", [{
            "podcast_title": "Show", "episode_title": "Ep", "published_at": "2026-01-02",
            "start": 3725.0, "text": "what was said",
        }])
        assert "[1] Show — Ep (2026-01-02) at 1:02:05" in prompt
        assert "what was said" in prompt

    def test_the_rules_forbid_inventing_a_quote(self):
        prompt = librarian.build_prompt("q", [])
        assert "Never invent a quote" in prompt
        assert "say so plainly" in prompt


class TestAsk:

    async def test_answer_carries_only_the_cited_passages(self, library, monkeypatch):
        async def fake(prompt, **kw):
            if "keyword searches" in prompt:
                return {"queries": ["evals", "burnout"]}
            return {"answer": "They read the failures weekly [1].", "cited": [1]}
        monkeypatch.setattr(librarian.llm, "arun_json", fake)

        db = await open_db()
        try:
            out = await librarian.ask(db, "how do they evaluate models?")
        finally:
            await db.close()
        assert out["answer"].startswith("They read the failures")
        assert len(out["passages"]) == 1
        assert out["passages"][0]["index"] == 1
        assert out["passages"][0]["episode_id"]

    async def test_a_citation_out_of_range_is_dropped(self, library, monkeypatch):
        """A model citing [9] of three passages must not index into nothing."""
        async def fake(prompt, **kw):
            if "keyword searches" in prompt:
                return {"queries": ["evals"]}
            return {"answer": "See [9].", "cited": [9, 1]}
        monkeypatch.setattr(librarian.llm, "arun_json", fake)
        db = await open_db()
        try:
            out = await librarian.ask(db, "evals?")
        finally:
            await db.close()
        assert out["cited"] == [1]

    async def test_nothing_found_says_so_without_a_second_model_call(
        self, library, monkeypatch,
    ):
        calls = []

        async def fake(prompt, **kw):
            calls.append(prompt)
            return {"queries": ["helicopter parenting"]}
        monkeypatch.setattr(librarian.llm, "arun_json", fake)

        db = await open_db()
        try:
            out = await librarian.ask(db, "what about helicopter parenting?")
        finally:
            await db.close()
        assert out["answer"] == librarian.NOTHING_FOUND
        assert out["passages"] == []
        assert len(calls) == 1, "spent a model call to say nothing was found"


class TestEndpoint:

    @pytest.fixture(autouse=True)
    def stub_model(self, monkeypatch):
        async def fake(prompt, **kw):
            if "keyword searches" in prompt:
                return {"queries": ["evals"]}
            return {"answer": "They read the failures weekly [1].", "cited": [1]}
        monkeypatch.setattr(librarian.llm, "arun_json", fake)

    async def test_starts_empty(self, client):
        assert (await client.get("/ask")).json() == []

    async def test_ask_and_read_back(self, client, library):
        r = await client.post("/ask", json={"message": "how do they evaluate models?"})
        assert r.status_code == 200, r.text
        answer = r.json()
        assert answer["role"] == "assistant"
        assert answer["citations"][0]["episode_id"] == "ep-evals"
        assert answer["queries"] == ["evals"]

        history = (await client.get("/ask")).json()
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert history[0]["content"] == "how do they evaluate models?"
        assert history[1]["citations"][0]["start"] >= 0

    async def test_an_empty_question_is_rejected(self, client):
        assert (await client.post("/ask", json={"message": "   "})).status_code == 422

    async def test_the_question_is_stored_even_if_answering_fails(
        self, client, library, monkeypatch,
    ):
        """So a screen reopened after a failure shows what was asked."""
        async def boom(prompt, **kw):
            if "keyword searches" in prompt:
                return {"queries": ["evals"]}
            return None
        monkeypatch.setattr(librarian.llm, "arun_json", boom)
        r = await client.post("/ask", json={"message": "how do they evaluate models?"})
        assert r.status_code == 502
        history = (await client.get("/ask")).json()
        assert [m["role"] for m in history] == ["user"]

    async def test_clear(self, client, library):
        await client.post("/ask", json={"message": "evals?"})
        assert (await client.delete("/ask")).status_code == 200
        assert (await client.get("/ask")).json() == []

    async def test_history_is_capped(self, client, library, monkeypatch):
        monkeypatch.setattr("routers.ask.MAX_HISTORY", 4)
        for i in range(4):
            await client.post("/ask", json={"message": f"question {i}"})
        history = (await client.get("/ask")).json()
        assert len(history) == 4
        # The oldest exchange is the one dropped.
        assert "question 0" not in [m["content"] for m in history]

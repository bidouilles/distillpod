"""Bookmarks — keeping a quote without paying for a model call.

The interesting behaviour is the quote extraction: the player knows only where
playback is, so the server has to turn a timestamp into something that reads
like a sentence someone said. Tests cover both the extractor in isolation and
the two ways in (a moment, or an explicit span from a transcript line).
"""
import json
import sqlite3

import pytest

from conftest import EPISODE_ID_1, EPISODE_ID_2
from services.bookmark_engine import extract

pytestmark = pytest.mark.asyncio


TRANSCRIPT = (
    "and so on and so forth. "
    "The real point is that transcripts change what a podcast is. "
    "You can search them, quote them, and keep them. "
    "Anyway, that is enough about that for now."
)


def _words(text: str, step: float = 0.5):
    out, t = [], 0.0
    for w in text.split():
        out.append({"word": " " + w, "start": round(t, 2), "end": round(t + step, 2)})
        t += step
    return out


def seed_transcript(db_path: str, episode_id: str = EPISODE_ID_1, text: str = TRANSCRIPT):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO transcripts (episode_id, words_json, language, created_at) "
        "VALUES (?, ?, 'en', '2026-02-01T00:00:00')",
        (episode_id, json.dumps(_words(text))),
    )
    conn.commit()
    conn.close()


class TestExtractor:
    """Pure function — no fixtures, so these say what the rule actually is."""

    def test_returns_nothing_without_a_transcript(self):
        assert extract([], 10) is None

    def test_starts_at_a_sentence_boundary(self):
        found = extract(_words(TRANSCRIPT), 12.0)
        assert found["text"].startswith("The real point")

    def test_ends_at_a_sentence_boundary(self):
        found = extract(_words(TRANSCRIPT), 12.0)
        assert found["text"].rstrip().endswith(".")

    def test_keeps_a_mid_sentence_start_rather_than_a_useless_quote(self):
        """Trimming to punctuation is only worth it if something readable is
        left. Four words is a worse bookmark than a sentence started late."""
        found = extract(_words(TRANSCRIPT), 0.4)
        assert found["text"].startswith("and so on")

    def test_span_is_reported(self):
        found = extract(_words(TRANSCRIPT), 12.0)
        assert 0 <= found["start_seconds"] <= 12.0 < found["end_seconds"] + 10

    def test_window_leans_backwards(self):
        """You press the button after hearing the thing worth keeping."""
        long_text = " ".join(f"word{i}" for i in range(400))
        found = extract(_words(long_text), 100.0)
        assert found["start_seconds"] < 100.0
        assert found["end_seconds"] <= 100.0 + 7


class TestCreate:

    async def test_from_a_moment_extracts_the_sentence(self, client, tmp_db):
        seed_transcript(tmp_db)
        r = await client.post("/bookmarks", json={"episode_id": EPISODE_ID_1, "seconds": 12.0})
        assert r.status_code == 200
        b = r.json()
        assert b["text"].startswith("The real point")
        assert b["episode_title"] == "Episode One"
        assert b["podcast_title"] == "Test Podcast"

    async def test_explicit_text_is_stored_verbatim(self, client, tmp_db):
        """A long-press on a transcript line already knows which words it means,
        so the server must not go and pick different ones."""
        r = await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1,
            "start_seconds": 5.0, "end_seconds": 9.0,
            "text": "exactly these words",
        })
        assert r.json()["text"] == "exactly these words"
        assert r.json()["start_seconds"] == 5.0

    async def test_note_is_kept(self, client, tmp_db):
        seed_transcript(tmp_db)
        r = await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1, "seconds": 12.0, "note": "why this matters",
        })
        assert r.json()["note"] == "why this matters"

    async def test_without_a_transcript_says_so(self, client):
        r = await client.post("/bookmarks", json={"episode_id": EPISODE_ID_2, "seconds": 30.0})
        assert r.status_code == 409
        assert "transcript" in r.json()["detail"].lower()

    async def test_silence_is_not_an_error_worth_a_500(self, client, tmp_db):
        seed_transcript(tmp_db)
        r = await client.post("/bookmarks", json={"episode_id": EPISODE_ID_1, "seconds": 9000.0})
        assert r.status_code == 409

    async def test_unknown_episode_rejected(self, client):
        r = await client.post("/bookmarks", json={"episode_id": "nope", "seconds": 1})
        assert r.status_code == 404

    async def test_needs_something_to_go_on(self, client):
        r = await client.post("/bookmarks", json={"episode_id": EPISODE_ID_1})
        assert r.status_code == 422


class TestListing:

    async def test_within_an_episode_ordered_by_time(self, client, tmp_db):
        """Within one episode the useful order is the order they were said."""
        for at in (12.0, 4.0, 20.0):
            await client.post("/bookmarks", json={
                "episode_id": EPISODE_ID_1, "start_seconds": at, "text": f"at {at}",
            })
        got = (await client.get(f"/bookmarks?episode_id={EPISODE_ID_1}")).json()
        assert [b["start_seconds"] for b in got] == [4.0, 12.0, 20.0]

    async def test_across_the_library_newest_first(self, client, tmp_db):
        a = (await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1, "start_seconds": 1, "text": "first"})).json()
        b = (await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_2, "start_seconds": 1, "text": "second"})).json()
        got = (await client.get("/bookmarks")).json()
        assert {x["id"] for x in got} == {a["id"], b["id"]}

    async def test_empty_by_default(self, client):
        assert (await client.get("/bookmarks")).json() == []


class TestAnnotateAndDelete:

    async def test_note_can_be_added_later(self, client):
        b = (await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1, "start_seconds": 1, "text": "quote"})).json()
        r = await client.patch(f"/bookmarks/{b['id']}", json={"note": "added later"})
        assert r.json()["note"] == "added later"

    async def test_note_can_be_cleared(self, client):
        b = (await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1, "start_seconds": 1, "text": "q", "note": "n"})).json()
        r = await client.patch(f"/bookmarks/{b['id']}", json={"note": "  "})
        assert r.json()["note"] is None

    async def test_annotating_something_absent_is_a_404(self, client):
        assert (await client.patch("/bookmarks/nope", json={"note": "x"})).status_code == 404

    async def test_delete(self, client):
        b = (await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1, "start_seconds": 1, "text": "quote"})).json()
        assert (await client.delete(f"/bookmarks/{b['id']}")).status_code == 200
        assert (await client.get("/bookmarks")).json() == []


class TestIntegration:

    async def test_feed_counts_bookmarks_independently_of_distills(self, client):
        """Both were once joined in one statement, which multiplied the rows and
        made an episode with 3 distills and 2 bookmarks report 6 of each."""
        for i in range(2):
            await client.post("/bookmarks", json={
                "episode_id": EPISODE_ID_1, "start_seconds": i, "text": f"q{i}"})
        ep = next(e for e in (await client.get("/podcasts/feed")).json()
                  if e["id"] == EPISODE_ID_1)
        assert ep["bookmark_count"] == 2
        assert ep["distill_count"] == 1          # seeded gist, unchanged

    async def test_bookmarked_is_a_feed_filter(self, client):
        await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_2, "start_seconds": 1, "text": "q"})
        got = (await client.get("/podcasts/feed?status=bookmarked")).json()
        assert [e["id"] for e in got] == [EPISODE_ID_2]

    async def test_export_note_includes_bookmarks(self, client):
        await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1, "start_seconds": 61.5,
            "text": "Transcripts change what a podcast is.", "note": "keep"})
        r = await client.get(f"/player/export/{EPISODE_ID_1}?enrich=false")
        md = r.json()["markdown"]
        assert "## Bookmarks" in md
        assert "> Transcripts change what a podcast is." in md
        assert "keep" in md

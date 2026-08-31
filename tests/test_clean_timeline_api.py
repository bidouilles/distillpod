"""Positions arriving from the clean cut, at the endpoints that store them.

The cut is a concatenation of the parts worth keeping, so it runs behind the
original by whatever was removed before the current point. Everything stored
uses the original timeline; these are the three places a player can report a
position, and each one has to translate.

Before this, a distill taken while listening to the ad-free version quoted a
passage minutes away from the one the listener had just heard.
"""
import json
import sqlite3

import pytest

from conftest import EPISODE_ID_1, EPISODE_ID_2

pytestmark = pytest.mark.asyncio

# 60 seconds removed at 95s, so cut-100s is original-160s.
SEGMENTS = [[0.0, 95.0], [155.0, 1180.0]]

TRANSCRIPT_TEXT = " ".join(
    f"word{i}." if i % 8 == 7 else f"word{i}" for i in range(3000)
)


def seed_clean_cut(db_path, episode_id=EPISODE_ID_1, segments=SEGMENTS, audio="/tmp/x.mp3"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE episodes SET adfree_path = ?, ads_detected = 1, transcript_status = 'done', "
        "processed_segments = ?, trimmed_seconds = 12.5 WHERE id = ?",
        (audio, json.dumps(segments), episode_id),
    )
    # A transcript, so distills and bookmarks have something to quote. One word
    # every half second, which puts word N at N/2 seconds.
    words = []
    t = 0.0
    for w in TRANSCRIPT_TEXT.split():
        words.append({"word": " " + w, "start": round(t, 2), "end": round(t + 0.5, 2)})
        t += 0.5
    conn.execute(
        "INSERT OR REPLACE INTO transcripts (episode_id, words_json, language, created_at) "
        "VALUES (?, ?, 'en', '2026-02-01T00:00:00')",
        (episode_id, json.dumps(words)),
    )
    conn.commit()
    conn.close()


class TestStatusEndpoint:

    async def test_reports_the_mapping(self, client, tmp_db, tmp_path):
        audio = tmp_path / "ep1_adfree.mp3"
        audio.write_bytes(b"x")
        seed_clean_cut(tmp_db, audio=str(audio))
        r = await client.get(f"/player/adfree-status/{EPISODE_ID_1}")
        assert r.json()["segments"] == SEGMENTS
        assert r.json()["trimmed_seconds"] == 12.5

    async def test_no_cut_no_mapping(self, client):
        r = await client.get(f"/player/adfree-status/{EPISODE_ID_2}")
        assert r.json() == {"has_adfree": False, "ads_count": 0,
                            "segments": None, "trimmed_seconds": 0}

    async def test_a_missing_file_is_not_offered_as_a_cut(self, client, tmp_db):
        """The row can outlive the file — retention clears audio."""
        seed_clean_cut(tmp_db, audio="/nonexistent/gone.mp3")
        r = await client.get(f"/player/adfree-status/{EPISODE_ID_1}")
        assert r.json()["has_adfree"] is False
        assert r.json()["segments"] is None


class TestProgress:

    async def test_a_position_from_the_cut_is_stored_in_the_original(self, client, tmp_db):
        seed_clean_cut(tmp_db)
        await client.put(f"/player/progress/{EPISODE_ID_1}",
                         json={"position": 100.0, "source": "clean"})
        stored = next(p for p in (await client.get("/player/progress")).json()
                      if p["episode_id"] == EPISODE_ID_1)
        assert stored["position"] == 160.0

    async def test_a_position_from_the_original_is_untouched(self, client, tmp_db):
        seed_clean_cut(tmp_db)
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"position": 100.0})
        stored = next(p for p in (await client.get("/player/progress")).json()
                      if p["episode_id"] == EPISODE_ID_1)
        assert stored["position"] == 100.0

    async def test_resume_survives_switching_source(self, client, tmp_db):
        """The point of storing one clock: a position saved while playing the cut
        has to mean the same place when the original is played next."""
        seed_clean_cut(tmp_db)
        await client.put(f"/player/progress/{EPISODE_ID_1}",
                         json={"position": 300.0, "source": "clean"})
        original = next(p for p in (await client.get("/player/progress")).json()
                        if p["episode_id"] == EPISODE_ID_1)["position"]
        assert original == 360.0

    async def test_an_episode_with_no_mapping_is_unchanged(self, client):
        await client.put(f"/player/progress/{EPISODE_ID_2}",
                         json={"position": 42.0, "source": "clean"})
        stored = next(p for p in (await client.get("/player/progress")).json()
                      if p["episode_id"] == EPISODE_ID_2)
        assert stored["position"] == 42.0


class TestBookmarks:

    async def test_a_moment_from_the_cut_quotes_what_was_heard(self, client, tmp_db):
        seed_clean_cut(tmp_db)
        from_cut = (await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1, "seconds": 100.0, "source": "clean"})).json()
        from_original = (await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1, "seconds": 160.0})).json()
        assert from_cut["text"] == from_original["text"]
        assert from_cut["start_seconds"] == from_original["start_seconds"]

    async def test_untranslated_would_have_quoted_somewhere_else(self, client, tmp_db):
        """Guards the fix rather than the plumbing: the two positions really do
        land on different words, so a missing translation is visible."""
        seed_clean_cut(tmp_db)
        a = (await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1, "seconds": 100.0, "source": "clean"})).json()
        b = (await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1, "seconds": 100.0})).json()
        assert a["text"] != b["text"]

    async def test_a_transcript_span_is_never_translated(self, client, tmp_db):
        """A line tapped in the transcript is already in the original timeline —
        translating it again would move the quote."""
        seed_clean_cut(tmp_db)
        b = (await client.post("/bookmarks", json={
            "episode_id": EPISODE_ID_1, "start_seconds": 100.0, "end_seconds": 104.0,
            "text": "exact words", "source": "clean"})).json()
        assert b["start_seconds"] == 100.0
        assert b["text"] == "exact words"


class TestDistills:

    async def test_a_distill_from_the_cut_uses_the_translated_window(
        self, client, tmp_db, monkeypatch,
    ):
        """The window is what the model gets asked about, so this is where a
        wrong clock turns into a quote that was never said."""
        seed_clean_cut(tmp_db)
        import services.snip_engine as engine
        monkeypatch.setattr(engine, "_summarize", None)

        async def no_model(text):
            return None
        monkeypatch.setattr(engine, "_summarize", no_model)

        r = await client.post("/gists/", json={
            "episode_id": EPISODE_ID_1, "current_seconds": 100.0, "source": "clean"})
        assert r.status_code == 200
        gist = r.json()
        # 60s window ending at the translated moment (160s), not at 100s.
        assert gist["end_seconds"] == 160.0
        assert gist["start_seconds"] == 100.0

    async def test_without_a_source_the_position_is_taken_as_given(
        self, client, tmp_db, monkeypatch,
    ):
        seed_clean_cut(tmp_db)
        import services.snip_engine as engine

        async def no_model(text):
            return None
        monkeypatch.setattr(engine, "_summarize", no_model)

        r = await client.post("/gists/", json={
            "episode_id": EPISODE_ID_1, "current_seconds": 100.0})
        assert r.json()["end_seconds"] == 100.0

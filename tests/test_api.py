"""
Backend API tests using httpx AsyncClient against in-memory test DB.
"""
import json
import sqlite3
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from conftest import (
    PODCAST_ID, EPISODE_ID_1, EPISODE_ID_2, EPISODE_ID_3,
    GIST_ID, SUG_ID_1, SUG_ID_2
)

pytestmark = pytest.mark.asyncio


# ── GET /podcasts/feed ────────────────────────────────────────────────────────

class TestFeed:

    async def test_returns_episodes(self, client):
        r = await client.get("/podcasts/feed")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 3

    async def test_includes_podcast_metadata(self, client):
        r = await client.get("/podcasts/feed")
        ep = next(e for e in r.json() if e["id"] == EPISODE_ID_1)
        assert ep["podcast_title"] == "Test Podcast"
        assert ep["podcast_image"] == "https://example.com/img.jpg"

    async def test_distill_count_correct(self, client):
        r = await client.get("/podcasts/feed")
        data = r.json()
        ep1 = next(e for e in data if e["id"] == EPISODE_ID_1)
        ep2 = next(e for e in data if e["id"] == EPISODE_ID_2)
        assert ep1["distill_count"] == 1   # has a gist
        assert ep2["distill_count"] == 0   # no gist

    async def test_no_description_field(self, client):
        r = await client.get("/podcasts/feed")
        for ep in r.json():
            assert "description" not in ep

    async def test_ordered_by_published_at_desc(self, client):
        r = await client.get("/podcasts/feed")
        dates = [ep["published_at"] for ep in r.json() if ep["published_at"]]
        assert dates == sorted(dates, reverse=True)

    async def test_empty_when_no_subscriptions(self, client, tmp_db):
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute("DELETE FROM subscriptions")
        conn.commit()
        conn.close()
        r = await client.get("/podcasts/feed")
        assert r.status_code == 200
        assert r.json() == []


# ── GET /podcasts/suggestions ─────────────────────────────────────────────────

class TestSuggestions:

    async def test_returns_only_active(self, client):
        r = await client.get("/podcasts/suggestions")
        assert r.status_code == 200
        data = r.json()
        ids = [s["id"] for s in data]
        assert SUG_ID_1 in ids
        assert SUG_ID_2 not in ids   # dismissed

    async def test_suggestion_has_reason(self, client):
        r = await client.get("/podcasts/suggestions")
        s = next(s for s in r.json() if s["id"] == SUG_ID_1)
        assert s["reason"] == "Covers LLM research in depth"

    async def test_empty_when_all_dismissed(self, client, tmp_db):
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute("UPDATE suggestions SET dismissed = 1")
        conn.commit()
        conn.close()
        r = await client.get("/podcasts/suggestions")
        assert r.json() == []


# ── POST /podcasts/suggestions/{id}/dismiss ───────────────────────────────────

class TestDismiss:

    async def test_dismiss_removes_from_active(self, client):
        r = await client.post(f"/podcasts/suggestions/{SUG_ID_1}/dismiss")
        assert r.status_code == 200
        assert r.json()["status"] == "dismissed"

        r2 = await client.get("/podcasts/suggestions")
        ids = [s["id"] for s in r2.json()]
        assert SUG_ID_1 not in ids

    async def test_dismiss_unknown_id_graceful(self, client):
        r = await client.post("/podcasts/suggestions/nonexistent_id/dismiss")
        # Should not 500 — 200 or 404 both acceptable
        assert r.status_code in (200, 404)


# ── GET /podcasts/subscriptions ───────────────────────────────────────────────

class TestSubscriptions:

    async def test_returns_subscriptions(self, client):
        r = await client.get("/podcasts/subscriptions")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["podcast_id"] == PODCAST_ID
        assert data[0]["title"] == "Test Podcast"


# ── Auth middleware ───────────────────────────────────────────────────────────

class TestAuth:

    async def test_browser_without_session_redirects(self):
        """Browser request to protected route without session → 302 to /unauthorized."""
        import database, config, main
        from httpx import AsyncClient, ASGITransport

        # Temporarily disable test_mode
        config.settings.test_mode = False
        try:
            async with AsyncClient(
                transport=ASGITransport(app=main.app),
                base_url="http://test",
                follow_redirects=False,
            ) as c:
                r = await c.get(
                    "/podcasts/feed",
                    headers={"Accept": "text/html,application/xhtml+xml"},
                )
            assert r.status_code == 302
            assert r.headers["location"] == "/unauthorized"
        finally:
            config.settings.test_mode = True

    async def test_api_without_session_returns_401(self):
        """API client without session cookie → 401 JSON."""
        import database, config, main
        from httpx import AsyncClient, ASGITransport

        config.settings.test_mode = False
        try:
            async with AsyncClient(
                transport=ASGITransport(app=main.app),
                base_url="http://test",
            ) as c:
                r = await c.get("/podcasts/feed")   # no Accept: text/html
            assert r.status_code == 401
            assert r.json()["detail"] == "Unauthorized"
        finally:
            config.settings.test_mode = True


# ── /chat endpoints ───────────────────────────────────────────────────────────

CHAT_EPISODE_ID = "ep_chat_001"
FAKE_TRANSCRIPT = json.dumps([{"word": w} for w in "This is a test transcript about AI".split()])


def seed_transcript(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO episodes (id, podcast_id, title, audio_url, transcript_status) VALUES (?, ?, ?, ?, ?)",
        (CHAT_EPISODE_ID, PODCAST_ID, "Chat Test Episode", "https://audio.example.com/chat.mp3", "done"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO transcripts (episode_id, words_json, language, created_at) VALUES (?, ?, ?, ?)",
        (CHAT_EPISODE_ID, FAKE_TRANSCRIPT, "en", "2026-03-01T00:00:00"),
    )
    conn.commit()
    conn.close()


class TestChat:

    async def test_get_chat_empty(self, client):
        """GET /chat/{episode_id} on unknown episode returns []."""
        r = await client.get("/chat/nonexistent-episode")
        assert r.status_code == 200
        assert r.json() == []

    async def test_init_chat_no_transcript(self, client):
        """POST /chat/{episode_id}/init with no transcript returns 404."""
        r = await client.post("/chat/no-transcript-episode/init")
        assert r.status_code == 404

    async def test_get_chat_returns_history(self, client, tmp_db):
        """Inserting a message directly then GET returns it."""
        msg_id = str(uuid.uuid4())
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO episode_chats (id, episode_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (msg_id, CHAT_EPISODE_ID, "assistant", "Hello!", "2026-03-01T00:00:00"),
        )
        conn.commit()
        conn.close()

        r = await client.get(f"/chat/{CHAT_EPISODE_ID}")
        assert r.status_code == 200
        data = r.json()
        assert len(data) == 1
        assert data[0]["id"] == msg_id
        assert data[0]["role"] == "assistant"
        assert data[0]["content"] == "Hello!"

    async def test_init_chat_idempotent(self, client, tmp_db):
        """POST /chat/init when history exists returns existing message, no duplicate."""
        msg_id = str(uuid.uuid4())
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO episode_chats (id, episode_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (msg_id, CHAT_EPISODE_ID, "assistant", "Existing summary", "2026-03-01T00:00:00"),
        )
        conn.commit()
        conn.close()

        with patch("routers.chat.llm_call") as mock_llm:
            r = await client.post(f"/chat/{CHAT_EPISODE_ID}/init")

        assert r.status_code == 200
        assert r.json()["id"] == msg_id
        mock_llm.assert_not_called()  # agent not called since message already exists

        # Confirm no duplicate inserted
        r2 = await client.get(f"/chat/{CHAT_EPISODE_ID}")
        assert len(r2.json()) == 1

    async def test_send_message_no_transcript(self, client):
        """POST /chat/{episode_id}/message with no transcript returns 404."""
        r = await client.post(
            "/chat/no-transcript-episode/message",
            json={"message": "What was this about?"},
        )
        assert r.status_code == 404

    async def test_chat_full_flow(self, client, tmp_db):
        """Full flow: init → get → message → get with mocked Claude."""
        seed_transcript(tmp_db)

        async def mock_llm(prompt: str) -> str:
            if "Summarize" in prompt or "bullet" in prompt.lower():
                return "• Key insight 1\n• Key insight 2\n\nWhat would you like to explore?"
            return "The main topic was AI and its implications."

        with patch("routers.chat.llm_call", side_effect=mock_llm):
            # 1. Init chat
            r_init = await client.post(f"/chat/{CHAT_EPISODE_ID}/init")
            assert r_init.status_code == 200
            init_msg = r_init.json()
            assert init_msg["role"] == "assistant"
            assert "Key insight" in init_msg["content"]

            # 2. GET history — 1 message
            r_get1 = await client.get(f"/chat/{CHAT_EPISODE_ID}")
            assert len(r_get1.json()) == 1

            # 3. Send user message
            r_msg = await client.post(
                f"/chat/{CHAT_EPISODE_ID}/message",
                json={"message": "What was the main topic?"},
            )
            assert r_msg.status_code == 200
            reply = r_msg.json()
            assert reply["role"] == "assistant"
            assert "AI" in reply["content"]

            # 4. GET history — 3 messages (init + user + assistant)
            r_get2 = await client.get(f"/chat/{CHAT_EPISODE_ID}")
            assert len(r_get2.json()) == 3
            roles = [m["role"] for m in r_get2.json()]
            assert roles == ["assistant", "user", "assistant"]


class TestProtectedRoutes:
    """Every API surface that reads user data or spends the owner's model
    subscription must sit behind the session cookie."""

    def test_all_api_prefixes_are_protected(self):
        from middleware.auth import PROTECTED_PREFIXES
        for prefix in ("/gists", "/podcasts", "/player", "/chat", "/research", "/tags", "/search"):
            assert prefix in PROTECTED_PREFIXES, f"{prefix} is reachable without a session"


class TestTestModeConsistency:
    """TEST_MODE must mean one thing. It already bypasses the middleware for
    every protected route, so /auth/me gating the SPA behind a login wall would
    leave a wide-open backend behind a door nobody can open."""

    @pytest.mark.asyncio
    async def test_auth_me_reports_a_user_in_test_mode(self, client):
        r = await client.get("/auth/me")
        assert r.status_code == 200
        assert r.json()["email"]

    @pytest.mark.asyncio
    async def test_protected_route_open_in_test_mode(self, client):
        assert (await client.get("/podcasts/feed")).status_code == 200


# ── /player/transcript — the read-along payload ───────────────────────────────

TIMED_EPISODE_ID = "ep_timed_001"
TIMED_WORDS = [
    {"word": "Would",  "start": 0.16,   "end": 0.48},
    {"word": " Rust",  "start": 0.48,   "end": 0.88},
    {"word": " have",  "start": 0.881,  "end": 1.1249},
]


def seed_timed_transcript(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR IGNORE INTO episodes (id, podcast_id, title, audio_url, transcript_status) VALUES (?, ?, ?, ?, ?)",
        (TIMED_EPISODE_ID, PODCAST_ID, "Timed Episode", "https://audio.example.com/t.mp3", "done"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO transcripts (episode_id, words_json, language, created_at) VALUES (?, ?, ?, ?)",
        (TIMED_EPISODE_ID, json.dumps(TIMED_WORDS), "en", "2026-03-01T00:00:00"),
    )
    conn.commit()
    conn.close()


class TestTranscriptEndpoint:

    async def test_words_come_back_as_compact_triples(self, client, tmp_db):
        """[start, end, text], not objects — the payload is ~10k words an hour."""
        seed_timed_transcript(tmp_db)
        r = await client.get(f"/player/transcript/{TIMED_EPISODE_ID}")
        assert r.status_code == 200
        body = r.json()
        assert body["language"] == "en"
        assert body["words"][0] == [0.16, 0.48, "Would"]

    async def test_the_leading_space_survives_the_round_trip(self, client, tmp_db):
        """Joining the words has to read as prose; the space lives on the word."""
        seed_timed_transcript(tmp_db)
        r = await client.get(f"/player/transcript/{TIMED_EPISODE_ID}")
        words = r.json()["words"]
        assert "".join(w[2] for w in words) == "Would Rust have"

    async def test_times_are_rounded_to_ten_milliseconds(self, client, tmp_db):
        """Finer than a spoken word boundary, and it keeps the payload small."""
        seed_timed_transcript(tmp_db)
        r = await client.get(f"/player/transcript/{TIMED_EPISODE_ID}")
        assert r.json()["words"][2] == [0.88, 1.12, " have"]

    async def test_a_transcript_without_timings_still_returns(self, client, tmp_db):
        """Older rows carry no start/end; they must not 500 the reader."""
        seed_transcript(tmp_db)
        r = await client.get(f"/player/transcript/{CHAT_EPISODE_ID}")
        assert r.status_code == 200
        assert all(w[0] == 0 and w[1] == 0 for w in r.json()["words"])

    async def test_an_episode_with_no_transcript_is_a_404(self, client):
        r = await client.get(f"/player/transcript/{EPISODE_ID_1}")
        assert r.status_code == 404


# ── /player/progress — cross-device resume ────────────────────────────────────

class TestProgress:

    async def test_nothing_started_yet(self, client):
        r = await client.get("/player/progress")
        assert r.status_code == 200 and r.json() == []

    async def test_a_saved_position_comes_back_with_its_episode(self, client):
        """A device that has never seen the episode still needs a title to show."""
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"position": 42.5, "duration": 3600})
        entries = (await client.get("/player/progress")).json()
        assert len(entries) == 1
        e = entries[0]
        assert e["episode_id"] == EPISODE_ID_1
        assert e["position"] == 42.5
        assert e["played"] is False
        assert e["title"] == "Episode One"
        assert e["podcast_title"] == "Test Podcast"

    async def test_saving_a_position_does_not_clear_the_finished_flag(self, client):
        """The two events are independent: one fires constantly, one fires once."""
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"played": True})
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"position": 12.0})
        e = (await client.get("/player/progress")).json()[0]
        assert e["played"] is True
        assert e["position"] == 12.0

    async def test_marking_finished_does_not_rewind_the_position(self, client):
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"position": 99.0, "duration": 100.0})
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"played": True})
        e = (await client.get("/player/progress")).json()[0]
        assert e["position"] == 99.0
        assert e["duration"] == 100.0
        assert e["played"] is True

    async def test_a_later_write_wins(self, client):
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"position": 10.0})
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"position": 250.0})
        e = (await client.get("/player/progress")).json()[0]
        assert e["position"] == 250.0

    async def test_two_episodes_are_tracked_separately(self, client):
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"position": 10.0})
        await client.put(f"/player/progress/{EPISODE_ID_2}", json={"position": 20.0})
        by_id = {e["episode_id"]: e for e in (await client.get("/player/progress")).json()}
        assert by_id[EPISODE_ID_1]["position"] == 10.0
        assert by_id[EPISODE_ID_2]["position"] == 20.0

    async def test_a_negative_position_is_clamped(self, client):
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"position": -5.0})
        assert (await client.get("/player/progress")).json()[0]["position"] == 0.0

    async def test_an_empty_update_is_rejected(self, client):
        r = await client.put(f"/player/progress/{EPISODE_ID_1}", json={})
        assert r.status_code == 400

    async def test_forgetting_an_episode(self, client):
        await client.put(f"/player/progress/{EPISODE_ID_1}", json={"position": 42.0})
        assert (await client.delete(f"/player/progress/{EPISODE_ID_1}")).status_code == 200
        assert (await client.get("/player/progress")).json() == []

    async def test_progress_survives_for_an_episode_that_is_gone(self, client):
        """A row whose episode was removed must not break the whole listing."""
        await client.put("/player/progress/ghost_episode", json={"position": 5.0})
        entries = (await client.get("/player/progress")).json()
        ghost = next(e for e in entries if e["episode_id"] == "ghost_episode")
        assert ghost["title"] is None


class TestBriefEndpoint:

    async def test_an_existing_summary_is_returned_without_spending_a_call(self, client, tmp_db):
        seed_timed_transcript(tmp_db)
        conn = sqlite3.connect(tmp_db)
        conn.execute("UPDATE episodes SET summary = ? WHERE id = ?", ("Already written.", TIMED_EPISODE_ID))
        conn.commit(); conn.close()

        with patch("services.note_builder.brief") as build:
            r = await client.get(f"/player/brief/{TIMED_EPISODE_ID}")
        assert r.json()["summary"] == "Already written."
        assert r.json()["generated"] is False
        assert not build.called

    async def test_a_summary_is_generated_and_stored_once(self, client, tmp_db):
        seed_timed_transcript(tmp_db)
        with patch("services.note_builder.brief", return_value="What it is about.") as build:
            first = await client.get(f"/player/brief/{TIMED_EPISODE_ID}")
            second = await client.get(f"/player/brief/{TIMED_EPISODE_ID}")
        assert first.json() == {"episode_id": TIMED_EPISODE_ID, "summary": "What it is about.", "generated": True}
        assert second.json()["generated"] is False      # served from the row
        assert build.call_count == 1

    async def test_an_untranscribed_episode_asks_for_nothing(self, client):
        with patch("services.note_builder.brief") as build:
            r = await client.get(f"/player/brief/{EPISODE_ID_1}")
        assert r.status_code == 200 and r.json()["summary"] is None
        assert not build.called

    async def test_an_unknown_episode_is_a_404(self, client):
        assert (await client.get("/player/brief/nope")).status_code == 404


class TestRefreshSubscriptions:
    """The control implies "go and look"; before this it only re-read the DB.

    The work runs as a background task so it outlives the screen that started
    it, which is why these poll for it to finish rather than reading the POST.
    """

    @staticmethod
    async def _settle(client):
        import asyncio
        for _ in range(50):
            await asyncio.sleep(0.02)
            state = (await client.get("/podcasts/refresh/status")).json()
            if not state["running"]:
                return state
        raise AssertionError("refresh never finished")

    async def test_the_post_returns_before_the_work_does(self, client, seeded_podcast_id):
        with patch("services.rss.fetch_episodes", AsyncMock(return_value=[])):
            r = await client.post("/podcasts/refresh")
            assert r.json()["status"] == "started"
            await self._settle(client)

    async def test_a_second_press_does_not_stack_another_run(self, client, seeded_podcast_id):
        with patch("services.rss.fetch_episodes", AsyncMock(return_value=[])):
            await client.post("/podcasts/refresh")
            again = await client.post("/podcasts/refresh")
            await self._settle(client)
        assert again.json()["status"] in ("already_running", "started")

    async def test_a_podcast_is_refreshed_through_rss(self, client, seeded_podcast_id):
        with patch("services.rss.fetch_episodes", AsyncMock(return_value=[])) as rss_fetch:
            await client.post("/podcasts/refresh")
            state = await self._settle(client)
        assert state["checked"] == 1 and state["failed"] == 0
        assert rss_fetch.called

    async def test_a_youtube_channel_is_refreshed_through_the_channel_sync(self, client, tmp_db):
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO subscriptions (podcast_id, feed_url, title, subscribed_at) VALUES (?, ?, ?, ?)",
            ("yt-UC6biysICWOJ-C3P4Tyeggzg", "https://www.youtube.com/channel/UC.../videos",
             "Low Level", "2026-01-01T00:00:00"),
        )
        conn.commit(); conn.close()

        with patch("services.youtube_library.sync_channel", AsyncMock(return_value={})) as sync, \
             patch("services.rss.fetch_episodes", AsyncMock(return_value=[])):
            await client.post("/podcasts/refresh")
            await self._settle(client)
        sync.assert_awaited_once()
        assert sync.await_args.args[0] == "UC6biysICWOJ-C3P4Tyeggzg"

    async def test_one_dead_feed_does_not_stop_the_others(self, client, tmp_db):
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO subscriptions (podcast_id, feed_url, title, subscribed_at) VALUES (?, ?, ?, ?)",
            ("pod_dead", "https://dead.example.com/feed", "Dead", "2026-01-01T00:00:00"),
        )
        conn.commit(); conn.close()

        async def flaky(feed_url, podcast_id, limit=20):
            if "dead" in feed_url:
                raise RuntimeError("gone")
            return []

        with patch("services.rss.fetch_episodes", AsyncMock(side_effect=flaky)):
            await client.post("/podcasts/refresh")
            state = await self._settle(client)
        assert state["checked"] == 2      # both were attempted
        assert state["failed"] == 1

    async def test_new_episodes_are_counted(self, client, seeded_podcast_id):
        from models import Episode
        fresh = [Episode(id="brand_new_ep", podcast_id=seeded_podcast_id, title="New one",
                         audio_url="https://audio.example.com/new.mp3")]
        with patch("services.rss.fetch_episodes", AsyncMock(return_value=fresh)):
            await client.post("/podcasts/refresh")
            state = await self._settle(client)
        assert state["new"] == 1

    async def test_an_episode_already_stored_is_not_counted_again(self, client, seeded_podcast_id):
        from models import Episode
        existing = [Episode(id=EPISODE_ID_1, podcast_id=seeded_podcast_id, title="Episode One",
                            audio_url="https://audio.example.com/1.mp3")]
        with patch("services.rss.fetch_episodes", AsyncMock(return_value=existing)):
            await client.post("/podcasts/refresh")
            state = await self._settle(client)
        assert state["new"] == 0


VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
CHANNEL_PODCAST_ID = "yt-UC4QobU6STFB0P71PMvOGN5A"
META = {
    "id": "jNQXAC9IVRw", "title": "Me at the zoo", "description": "",
    "webpage_url": VIDEO_URL, "duration": 19, "thumbnail": None,
    "channel": "jawed", "channel_id": "UC4QobU6STFB0P71PMvOGN5A",
    "upload_date": "20050423", "language": "en",
}


class TestSubscriptionSource:
    """The library shows what each row is, so the label has to be right."""

    async def test_a_podcast_subscription_is_labelled_a_podcast(self, client):
        await client.post("/podcasts/subscriptions/pod_new?feed_url=https://f.example.com&title=New")
        subs = (await client.get("/podcasts/subscriptions")).json()
        row = next(s for s in subs if s["podcast_id"] == "pod_new")
        assert row["source"] == "podcast"

    async def test_adding_one_video_marks_its_channel_as_a_single_video(self, client):
        from services import youtube
        with patch.object(youtube, "fetch_metadata", AsyncMock(return_value=META)), \
             patch("routers.youtube._start_ingest"):
            await client.post("/youtube/add", json={"url": VIDEO_URL})
        subs = (await client.get("/podcasts/subscriptions")).json()
        row = next(s for s in subs if s["podcast_id"] == CHANNEL_PODCAST_ID)
        assert row["source"] == "youtube_video"

    async def test_subscribing_promotes_a_single_video_row_to_a_channel(self, client):
        """Adding a video then subscribing must not leave it looking one-off."""
        from services import youtube
        with patch.object(youtube, "fetch_metadata", AsyncMock(return_value=META)), \
             patch("routers.youtube._start_ingest"):
            await client.post("/youtube/add", json={"url": VIDEO_URL})

        channel = {"channel_id": META["channel_id"], "title": "Low Level", "thumbnail": None}
        with patch.object(youtube, "resolve_channel", AsyncMock(return_value=channel)), \
             patch("routers.youtube._start_channel_import"):
            await client.post("/youtube/add", json={"url": "https://www.youtube.com/@LowLevelTV"})

        subs = (await client.get("/podcasts/subscriptions")).json()
        row = next(s for s in subs if s["podcast_id"] == CHANNEL_PODCAST_ID)
        assert row["source"] == "youtube_channel"

    async def test_adding_a_video_does_not_demote_a_subscribed_channel(self, client):
        from services import youtube
        channel = {"channel_id": META["channel_id"], "title": "Low Level", "thumbnail": None}
        with patch.object(youtube, "resolve_channel", AsyncMock(return_value=channel)), \
             patch("routers.youtube._start_channel_import"):
            await client.post("/youtube/add", json={"url": "https://www.youtube.com/@LowLevelTV"})
        with patch.object(youtube, "fetch_metadata", AsyncMock(return_value=META)), \
             patch("routers.youtube._start_ingest"):
            await client.post("/youtube/add", json={"url": VIDEO_URL})

        subs = (await client.get("/podcasts/subscriptions")).json()
        row = next(s for s in subs if s["podcast_id"] == CHANNEL_PODCAST_ID)
        assert row["source"] == "youtube_channel"

    async def test_a_one_off_video_channel_is_not_polled_by_refresh(self, client):
        """Its badge says nobody subscribed, so importing uploads would lie."""
        from services import youtube
        with patch.object(youtube, "fetch_metadata", AsyncMock(return_value=META)), \
             patch("routers.youtube._start_ingest"):
            await client.post("/youtube/add", json={"url": VIDEO_URL})

        with patch("services.youtube_library.sync_channel", AsyncMock()) as sync, \
             patch("services.rss.fetch_episodes", AsyncMock(return_value=[])):
            await client.post("/podcasts/refresh")
            await TestRefreshSubscriptions._settle(client)
        assert not sync.called

    async def test_refreshing_a_one_off_channels_page_follows_it(self, client, tmp_db):
        """Asking that page for new videos is asking to follow the channel; the
        badge must not keep saying nobody subscribed while uploads arrive."""
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "INSERT INTO subscriptions (podcast_id, feed_url, title, subscribed_at, source) VALUES (?, ?, ?, ?, ?)",
            ("yt-UConeoff", "https://www.youtube.com/channel/UConeoff/videos",
             "Some Channel", "2026-01-01T00:00:00", "youtube_video"),
        )
        conn.commit(); conn.close()

        with patch("services.youtube_library.sync_channel", AsyncMock(return_value={})):
            r = await client.get("/podcasts/yt-UConeoff/episodes?refresh=true")
        assert r.status_code == 200

        subs = (await client.get("/podcasts/subscriptions")).json()
        row = next(s for s in subs if s["podcast_id"] == "yt-UConeoff")
        assert row["source"] == "youtube_channel"

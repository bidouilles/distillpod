"""
Unit tests for YouTube ingestion (backend/services/youtube.py, routers/youtube.py).

The load-bearing property is the transcript shape: [{word, start, end}] with the
word's leading space intact, non-overlapping and in order. Distill windows, the
ad segmenter and chapter seeks all index into that array, so captions that
arrive as phrase-long spans have to be split before they are stored.
"""
import json
from datetime import timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import youtube


# ── URL handling ──────────────────────────────────────────────────────────────

class TestUrls:

    @pytest.mark.parametrize("url,expected", [
        ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://youtube.com/watch?v=jNQXAC9IVRw&t=42s", "jNQXAC9IVRw"),
        ("https://m.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://youtu.be/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://youtu.be/jNQXAC9IVRw?si=abcdef", "jNQXAC9IVRw"),
        ("https://www.youtube.com/shorts/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://www.youtube.com/embed/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("https://www.youtube.com/live/jNQXAC9IVRw", "jNQXAC9IVRw"),
        ("  https://www.youtube.com/watch?v=jNQXAC9IVRw  ", "jNQXAC9IVRw"),
    ])
    def test_extracts_the_video_id(self, url, expected):
        assert youtube.video_id(url) == expected

    @pytest.mark.parametrize("url", [
        "",
        "not a url",
        "https://example.com/watch?v=jNQXAC9IVRw",       # right shape, wrong host
        "https://www.youtube.com/@somechannel",           # a channel, not a video
        "https://www.youtube.com/playlist?list=PL123",    # a playlist, not a video
        "https://vimeo.com/12345",
    ])
    def test_rejects_everything_else(self, url):
        assert youtube.video_id(url) is None

    def test_host_check_is_not_fooled_by_a_lookalike_domain(self):
        # A naive substring check would accept this and hand the URL to yt-dlp.
        assert not youtube.is_youtube_url("https://youtube.com.evil.test/watch?v=jNQXAC9IVRw")


# ── json3 caption parsing ─────────────────────────────────────────────────────

def _event(start_ms, dur_ms, segs):
    return {"tStartMs": start_ms, "dDurationMs": dur_ms, "segs": segs}


class TestCaptionParsing:

    def test_auto_captions_keep_their_per_word_timings(self):
        payload = {"events": [
            _event(0, 3000, [
                {"utf8": "This"},
                {"utf8": " video", "tOffsetMs": 320},
                {"utf8": " is", "tOffsetMs": 600},
            ]),
        ]}
        words = youtube._words_from_json3(payload)
        assert [w["word"] for w in words] == ["This", " video", " is"]
        assert words[0] == {"word": "This", "start": 0.0, "end": 0.32}
        assert words[1]["start"] == 0.32

    def test_phrase_level_segs_are_split_into_words(self):
        """Human-written subtitles put a whole line in one seg.

        Stored as-is, a single 'word' would span two seconds and every seek into
        it would land at the start of the line rather than the word.
        """
        payload = {"events": [_event(1200, 2160, [{"utf8": "All right, so here"}])]}
        words = youtube._words_from_json3(payload)
        assert [w["word"] for w in words] == ["All", " right,", " so", " here"]
        assert words[0]["start"] == 1.2
        assert words[-1]["end"] == pytest.approx(3.36, abs=0.01)
        # Interpolated, but strictly ordered and gapless.
        for a, b in zip(words, words[1:]):
            assert a["end"] == pytest.approx(b["start"], abs=0.001)

    def test_line_breaks_inside_a_caption_do_not_leak_into_words(self):
        payload = {"events": [_event(0, 2000, [{"utf8": "in front of the\nelephants"}])]}
        words = youtube._words_from_json3(payload)
        assert [w["word"] for w in words] == ["in", " front", " of", " the", " elephants"]

    def test_rollup_newline_segs_are_dropped_but_the_space_survives(self):
        """The joined text must read as prose, not runwordstogether."""
        payload = {"events": [
            _event(0, 1790, [{"utf8": "the"}, {"utf8": " pie", "tOffsetMs": 500}]),
            _event(1790, 1450, [{"utf8": "\n"}]),
            _event(1800, 2560, [{"utf8": "coding"}, {"utf8": " agent.", "tOffsetMs": 360}]),
        ]}
        words = youtube._words_from_json3(payload)
        assert "".join(w["word"] for w in words) == "the pie coding agent."

    def test_only_the_very_first_word_has_no_leading_space(self):
        payload = {"events": [_event(0, 1000, [{"utf8": "hello world"}])]}
        words = youtube._words_from_json3(payload)
        assert words[0]["word"] == "hello"
        assert all(w["word"].startswith(" ") for w in words[1:])

    def test_overlapping_rollup_lines_are_clamped(self):
        """A line's declared end can run past the next line's start."""
        payload = {"events": [
            _event(0, 5000, [{"utf8": "first"}]),
            _event(1000, 2000, [{"utf8": "second"}]),
        ]}
        words = youtube._words_from_json3(payload)
        assert words[0]["end"] <= words[1]["start"]
        assert all(w["end"] >= w["start"] for w in words)

    def test_last_word_never_runs_past_the_video(self):
        payload = {"events": [_event(0, 30000, [{"utf8": "word"}])]}
        words = youtube._words_from_json3(payload, duration=19)
        assert words[-1]["end"] == 19.0

    def test_empty_and_segless_events_are_ignored(self):
        payload = {"events": [
            {"tStartMs": 0, "dDurationMs": 100},          # the header event
            _event(0, 100, [{"utf8": "  "}]),
            _event(100, 100, [{"utf8": "ok"}]),
        ]}
        assert [w["word"] for w in youtube._words_from_json3(payload)] == ["ok"]

    def test_no_events_gives_no_words(self):
        assert youtube._words_from_json3({}) == []


# ── Caption track selection ───────────────────────────────────────────────────

def _track(ext="json3", url="https://cap.test/x"):
    return [{"ext": "srv1", "url": "ignored"}, {"ext": ext, "url": url}]


class TestCaptionTrackChoice:

    def test_human_written_subtitles_beat_auto_captions(self):
        meta = {
            "language": "en",
            "subtitles": {"en": _track(url="https://manual.test")},
            "automatic_captions": {"en": _track(url="https://auto.test")},
        }
        assert youtube.caption_track(meta) == ("en", "https://manual.test")

    def test_auto_captions_are_used_when_there_are_no_subtitles(self):
        meta = {"language": "en", "automatic_captions": {"en-orig": _track(url="https://auto.test")}}
        assert youtube.caption_track(meta) == ("en-orig", "https://auto.test")

    def test_a_machine_translation_is_never_picked_up(self):
        """automatic_captions lists every language YouTube can translate into.

        Taking "en" for a French video would store an English transcript over
        French audio: search would match words nobody says.
        """
        meta = {
            "language": "fr",
            "automatic_captions": {"en": _track(url="https://translated.test")},
        }
        assert youtube.caption_track(meta) is None

    def test_no_captions_at_all(self):
        assert youtube.caption_track({"language": "en"}) is None

    def test_a_track_without_json3_is_not_usable(self):
        meta = {"language": "en", "automatic_captions": {"en": [{"ext": "vtt", "url": "u"}]}}
        assert youtube.caption_track(meta) is None

    def test_the_original_track_wins_over_the_same_language_translation(self):
        """automatic_captions holds both "fr" and "fr-orig" for a French video.

        "fr-orig" is the speech recognition output; the bare code is nominally
        the machine translation into the same language. YouTube usually serves
        identical bytes, but the original is the one that says what it is.
        """
        meta = {
            "language": "fr",
            "automatic_captions": {
                "fr": _track(url="https://translated-back.test"),
                "fr-orig": _track(url="https://original.test"),
                "en": _track(url="https://english.test"),
            },
        }
        assert youtube.caption_track(meta) == ("fr-orig", "https://original.test")

    def test_a_french_video_is_transcribed_in_french(self):
        meta = {"language": "fr", "automatic_captions": {
            lang: _track(url=f"https://{lang}.test")
            for lang in ("en", "es", "de", "fr", "fr-orig", "ja")
        }}
        lang, url = youtube.caption_track(meta)
        assert (lang, url) == ("fr-orig", "https://fr-orig.test")
        assert youtube.caption_language(meta) == "fr"

    def test_an_english_video_is_transcribed_in_english(self):
        meta = {"language": "en", "automatic_captions": {
            lang: _track(url=f"https://{lang}.test")
            for lang in ("en", "en-orig", "fr", "pt")
        }}
        assert youtube.caption_track(meta) == ("en-orig", "https://en-orig.test")
        assert youtube.caption_language(meta) == "en"

    def test_a_regional_variant_still_counts_as_the_original(self):
        meta = {"language": "fr", "automatic_captions": {
            "en": _track(url="https://en.test"),
            "fr-CA-orig": _track(url="https://quebec.test"),
        }}
        assert youtube.caption_track(meta) == ("fr-CA-orig", "https://quebec.test")


class TestOriginalLanguage:

    def test_the_declared_audio_language_settles_it(self):
        assert youtube.original_language({"language": "fr-FR"}) == "fr"

    def test_falls_back_to_the_orig_caption_key(self):
        """That key names the language speech recognition actually ran on."""
        meta = {"automatic_captions": {"en": [], "pt-orig": [], "ja": []}}
        assert youtube.original_language(meta) == "pt"

    def test_falls_back_to_a_lone_hand_written_subtitle_track(self):
        meta = {"subtitles": {"de": [], "live_chat": []}}
        assert youtube.original_language(meta) == "de"

    def test_several_subtitle_tracks_and_nothing_else_is_not_a_guess(self):
        """Guessing here would risk transcribing French audio into English."""
        assert youtube.original_language({"subtitles": {"en": [], "fr": []}}) == ""

    def test_nothing_to_go_on(self):
        assert youtube.original_language({}) == ""

    def test_an_undeterminable_language_takes_no_captions_at_all(self):
        meta = {"subtitles": {"en": _track(), "fr": _track()}}
        assert youtube.caption_track(meta) is None



# ── Metadata mapping ──────────────────────────────────────────────────────────

class TestMetadata:

    def test_chapters_come_across_in_the_shape_the_table_wants(self):
        meta = {"chapters": [
            {"start_time": 0, "end_time": 46, "title": "Introduction"},
            {"start_time": 46, "end_time": 81, "title": "Setup"},
            {"start_time": 90, "title": "   "},          # unusable
        ]}
        assert youtube.chapters(meta) == [
            {"title": "Introduction", "start_time": 0.0},
            {"title": "Setup", "start_time": 46.0},
        ]

    def test_no_chapters(self):
        assert youtube.chapters({}) == []

    def test_published_at_prefers_the_exact_timestamp(self):
        dt = youtube.published_at({"timestamp": 1114882000, "upload_date": "20050423"})
        assert dt is not None and dt.tzinfo == timezone.utc

    def test_published_at_falls_back_to_upload_date(self):
        dt = youtube.published_at({"upload_date": "20050423"})
        assert dt is not None and (dt.year, dt.month, dt.day) == (2005, 4, 23)

    def test_published_at_tolerates_a_video_with_neither(self):
        assert youtube.published_at({}) is None

    def test_a_playlist_url_is_refused_rather_than_ingested_as_one_episode(self):
        proc = MagicMock(stdout=json.dumps({"_type": "playlist", "entries": []}))
        with patch.object(youtube, "_run", return_value=proc):
            with pytest.raises(youtube.YouTubeError, match="playlist or channel"):
                youtube._fetch_metadata_blocking("https://youtube.com/playlist?list=X")

    def test_unparseable_yt_dlp_output_is_an_error_not_a_crash(self):
        with patch.object(youtube, "_run", return_value=MagicMock(stdout="not json")):
            with pytest.raises(youtube.YouTubeError):
                youtube._fetch_metadata_blocking("https://youtu.be/jNQXAC9IVRw")


# ── The downloader's YouTube branch ───────────────────────────────────────────

@pytest.mark.asyncio
class TestDownloadDispatch:

    @pytest.fixture
    def media_dir(self, tmp_path, monkeypatch):
        from config import settings
        monkeypatch.setattr(settings, "media_dir", tmp_path / "media")
        return tmp_path / "media"

    async def test_a_youtube_url_goes_through_yt_dlp(self, media_dir):
        from services import downloader

        async def fake_download(url, dest):
            Path(dest).write_bytes(b"mp3")
            return dest

        with patch.object(youtube, "download_audio", side_effect=fake_download) as dl:
            path = await downloader.download_episode(
                "yt-jNQXAC9IVRw", "https://www.youtube.com/watch?v=jNQXAC9IVRw"
            )
        assert dl.called
        assert path.exists() and path.suffix == ".mp3"

    async def test_an_rss_url_still_goes_over_plain_http(self, media_dir):
        from services import downloader

        class FakeResponse:
            def raise_for_status(self):
                pass

            async def aiter_bytes(self, chunk_size=0):
                yield b"audio"

        class FakeStream:
            async def __aenter__(self):
                return FakeResponse()

            async def __aexit__(self, *exc):
                return False

        class FakeClient:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def stream(self, method, url):
                return FakeStream()

        with patch.object(youtube, "download_audio") as dl, \
             patch("services.downloader.httpx.AsyncClient", FakeClient):
            path = await downloader.download_episode("ep1", "https://cdn.test/ep1.mp3")
        assert not dl.called
        assert path.read_bytes() == b"audio"

    async def test_a_failed_download_leaves_nothing_that_looks_complete(self, media_dir):
        """A half-written file must not be mistaken for a finished download."""
        from services import downloader

        async def boom(url, dest):
            Path(dest).write_bytes(b"partial")
            raise youtube.YouTubeError("network died")

        with patch.object(youtube, "download_audio", side_effect=boom):
            with pytest.raises(youtube.YouTubeError):
                await downloader.download_episode(
                    "yt-jNQXAC9IVRw", "https://www.youtube.com/watch?v=jNQXAC9IVRw"
                )
        assert list(media_dir.glob("*")) == []


# ── POST /youtube/add ─────────────────────────────────────────────────────────

VIDEO_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
EPISODE_ID = "yt-jNQXAC9IVRw"
CHANNEL_PODCAST_ID = "yt-UC4QobU6STFB0P71PMvOGN5A"

META = {
    "id": "jNQXAC9IVRw",
    "title": "Me at the zoo",
    "description": "The first video.",
    "webpage_url": VIDEO_URL,
    "duration": 19,
    "thumbnail": "https://i.ytimg.com/vi/jNQXAC9IVRw/hqdefault.jpg",
    "channel": "jawed",
    "channel_id": "UC4QobU6STFB0P71PMvOGN5A",
    "upload_date": "20050423",
    "language": "en",
    "chapters": [
        {"start_time": 0, "end_time": 5, "title": "Intro"},
        {"start_time": 5, "end_time": 17, "title": "The cool thing"},
    ],
    "automatic_captions": {"en-orig": [{"ext": "json3", "url": "https://cap.test/x"}]},
}


async def _rows(db_path, sql, params=()):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


@pytest.mark.asyncio
class TestAddEndpoint:

    async def test_a_video_becomes_an_episode_under_its_channel(self, client, tmp_db):
        with patch.object(youtube, "fetch_metadata", AsyncMock(return_value=META)), \
             patch("routers.youtube._start_ingest") as ingest:
            r = await client.post("/youtube/add", json={"url": VIDEO_URL})

        assert r.status_code == 200
        body = r.json()
        assert body["episode_id"] == EPISODE_ID
        assert body["podcast_id"] == CHANNEL_PODCAST_ID
        assert body["title"] == "Me at the zoo"
        assert body["has_captions"] is True
        assert body["already_added"] is False
        assert ingest.called      # transcript + audio continue in the background

        eps = await _rows(tmp_db, "SELECT * FROM episodes WHERE id = ?", (EPISODE_ID,))
        assert len(eps) == 1
        assert eps[0]["audio_url"] == VIDEO_URL      # the downloader resolves this
        assert eps[0]["duration_seconds"] == 19
        assert eps[0]["transcript_status"] == "queued"
        assert eps[0]["published_at"].startswith("2005-04-23")

        # The feed joins on subscriptions, so the channel has to exist as one.
        subs = await _rows(tmp_db, "SELECT * FROM subscriptions WHERE podcast_id = ?",
                           (CHANNEL_PODCAST_ID,))
        assert subs[0]["title"] == "jawed"

    async def test_the_uploaders_own_chapters_are_kept(self, client, tmp_db):
        with patch.object(youtube, "fetch_metadata", AsyncMock(return_value=META)), \
             patch("routers.youtube._start_ingest"):
            await client.post("/youtube/add", json={"url": VIDEO_URL})

        chapters = await _rows(
            tmp_db, "SELECT title, start_time FROM chapters WHERE episode_id = ? ORDER BY start_time",
            (EPISODE_ID,))
        assert [c["title"] for c in chapters] == ["Intro", "The cool thing"]
        eps = await _rows(tmp_db, "SELECT chapters_status FROM episodes WHERE id = ?", (EPISODE_ID,))
        assert eps[0]["chapters_status"] == "done"

    async def test_the_episode_shows_up_in_the_feed(self, client):
        with patch.object(youtube, "fetch_metadata", AsyncMock(return_value=META)), \
             patch("routers.youtube._start_ingest"):
            await client.post("/youtube/add", json={"url": VIDEO_URL})

        feed = (await client.get("/podcasts/feed")).json()
        assert any(e["id"] == EPISODE_ID and e["podcast_title"] == "jawed" for e in feed)

    async def test_re_adding_returns_the_existing_episode_without_refetching(self, client):
        with patch.object(youtube, "fetch_metadata", AsyncMock(return_value=META)), \
             patch("routers.youtube._start_ingest"):
            await client.post("/youtube/add", json={"url": VIDEO_URL})
        # First add left it 'queued'; pretend the ingest finished.
        import database, sqlite3
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute("UPDATE episodes SET transcript_status = 'done' WHERE id = ?", (EPISODE_ID,))
        conn.commit(); conn.close()

        with patch.object(youtube, "fetch_metadata", AsyncMock()) as fetch:
            r = await client.post("/youtube/add", json={"url": VIDEO_URL})
        assert r.json()["already_added"] is True
        assert not fetch.called      # no second yt-dlp round-trip

    async def test_a_failed_ingest_can_be_retried(self, client):
        """An episode stuck at 'error' must not be treated as already added."""
        with patch.object(youtube, "fetch_metadata", AsyncMock(return_value=META)), \
             patch("routers.youtube._start_ingest"):
            await client.post("/youtube/add", json={"url": VIDEO_URL})
        import database, sqlite3
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute("UPDATE episodes SET transcript_status = 'error' WHERE id = ?", (EPISODE_ID,))
        conn.commit(); conn.close()

        with patch.object(youtube, "fetch_metadata", AsyncMock(return_value=META)), \
             patch("routers.youtube._start_ingest") as ingest:
            r = await client.post("/youtube/add", json={"url": VIDEO_URL})
        assert r.json()["already_added"] is False
        assert ingest.called

    async def test_a_non_youtube_url_is_refused_before_yt_dlp_runs(self, client):
        with patch.object(youtube, "fetch_metadata", AsyncMock()) as fetch:
            r = await client.post("/youtube/add", json={"url": "https://example.com/x"})
        assert r.status_code == 400
        assert not fetch.called

    async def test_a_yt_dlp_failure_surfaces_as_a_gateway_error(self, client):
        with patch.object(youtube, "fetch_metadata",
                          AsyncMock(side_effect=youtube.YouTubeError("video unavailable"))):
            r = await client.post("/youtube/add", json={"url": VIDEO_URL})
        assert r.status_code == 502
        assert "unavailable" in r.json()["detail"]

    async def test_the_endpoint_requires_a_session(self):
        """New routers are only protected if their prefix is in the allowlist."""
        import config, main
        from httpx import AsyncClient, ASGITransport

        config.settings.test_mode = False
        try:
            async with AsyncClient(transport=ASGITransport(app=main.app),
                                   base_url="http://test") as c:
                r = await c.post("/youtube/add", json={"url": VIDEO_URL})
            assert r.status_code == 401
        finally:
            config.settings.test_mode = True


# ── The background ingest ─────────────────────────────────────────────────────

CAPTION_WORDS = [
    {"word": "All", "start": 1.2, "end": 1.35},
    {"word": " right", "start": 1.35, "end": 1.66},
]


@pytest.mark.asyncio
class TestIngest:

    async def _add(self, client):
        with patch.object(youtube, "fetch_metadata", AsyncMock(return_value=META)), \
             patch("routers.youtube._start_ingest"):
            await client.post("/youtube/add", json={"url": VIDEO_URL})

    async def test_captions_become_the_transcript_without_waking_the_stt_backend(
            self, client, tmp_db, tmp_path):
        from routers.youtube import _ingest
        await self._add(client)

        audio = tmp_path / "a.mp3"; audio.write_bytes(b"mp3")
        with patch.object(youtube, "fetch_caption_words", AsyncMock(return_value=CAPTION_WORDS)), \
             patch("routers.youtube.download_episode", AsyncMock(return_value=audio)), \
             patch("routers.youtube.transcribe_episode", AsyncMock()) as stt_run:
            await _ingest(EPISODE_ID, META)

        assert not stt_run.called
        rows = await _rows(tmp_db, "SELECT words_json, language FROM transcripts WHERE episode_id = ?",
                           (EPISODE_ID,))
        assert json.loads(rows[0]["words_json"]) == CAPTION_WORDS
        assert rows[0]["language"] == "en"
        eps = await _rows(tmp_db, "SELECT transcript_status, downloaded, local_path FROM episodes WHERE id = ?",
                          (EPISODE_ID,))
        assert eps[0]["transcript_status"] == "done"
        assert eps[0]["downloaded"] == 1        # audio is fetched either way
        assert eps[0]["local_path"] == str(audio)

    async def test_a_caption_transcript_is_searchable(self, client, tmp_db, tmp_path):
        """index_transcript must run on every transcript write, whatever wrote it."""
        from routers.youtube import _ingest
        await self._add(client)

        audio = tmp_path / "a.mp3"; audio.write_bytes(b"mp3")
        with patch.object(youtube, "fetch_caption_words", AsyncMock(return_value=CAPTION_WORDS)), \
             patch("routers.youtube.download_episode", AsyncMock(return_value=audio)):
            await _ingest(EPISODE_ID, META)

        hits = await _rows(tmp_db, "SELECT episode_id FROM transcripts_fts WHERE transcripts_fts MATCH ?",
                           ("right",))
        assert [h["episode_id"] for h in hits] == [EPISODE_ID]

    async def test_a_video_without_captions_falls_back_to_stt(self, client, tmp_path):
        from routers.youtube import _ingest
        await self._add(client)

        audio = tmp_path / "a.mp3"; audio.write_bytes(b"mp3")
        with patch.object(youtube, "fetch_caption_words", AsyncMock(return_value=[])), \
             patch("routers.youtube.download_episode", AsyncMock(return_value=audio)), \
             patch("routers.youtube.transcribe_episode", AsyncMock()) as stt_run:
            await _ingest(EPISODE_ID, META)

        stt_run.assert_awaited_once_with(EPISODE_ID, audio)

    async def test_a_download_failure_after_good_captions_keeps_the_transcript(
            self, client, tmp_db):
        """The transcript is still worth having even when the audio never arrives."""
        from routers.youtube import _ingest
        await self._add(client)

        with patch.object(youtube, "fetch_caption_words", AsyncMock(return_value=CAPTION_WORDS)), \
             patch("routers.youtube.download_episode",
                   AsyncMock(side_effect=youtube.YouTubeError("gone"))):
            await _ingest(EPISODE_ID, META)

        eps = await _rows(tmp_db, "SELECT transcript_status, downloaded FROM episodes WHERE id = ?",
                          (EPISODE_ID,))
        assert eps[0]["transcript_status"] == "done"
        assert eps[0]["downloaded"] == 0

    async def test_a_download_failure_with_no_captions_marks_the_episode_errored(
            self, client, tmp_db):
        from routers.youtube import _ingest
        await self._add(client)

        with patch.object(youtube, "fetch_caption_words", AsyncMock(return_value=[])), \
             patch("routers.youtube.download_episode",
                   AsyncMock(side_effect=youtube.YouTubeError("gone"))), \
             patch("routers.youtube.transcribe_episode", AsyncMock()) as stt_run:
            await _ingest(EPISODE_ID, META)

        assert not stt_run.called
        eps = await _rows(tmp_db, "SELECT transcript_status FROM episodes WHERE id = ?", (EPISODE_ID,))
        assert eps[0]["transcript_status"] == "error"


# ── Channel subscriptions ─────────────────────────────────────────────────────

class TestChannelUrls:

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/@LowLevelTV",
        "https://www.youtube.com/@NetworkChuck/videos",
        "https://www.youtube.com/@someone/streams",
        "https://youtube.com/channel/UC6biysICWOJ-C3P4Tyeggzg",
        "https://www.youtube.com/channel/UC6biysICWOJ-C3P4Tyeggzg/videos",
        "https://www.youtube.com/c/SomeChannel",
        "https://www.youtube.com/user/SomeUser",
    ])
    def test_channel_urls_are_recognised(self, url):
        assert youtube.is_channel_url(url)

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://youtu.be/jNQXAC9IVRw",
        "https://www.youtube.com/shorts/jNQXAC9IVRw",
        "https://example.com/@someone",
        "",
    ])
    def test_a_video_is_not_a_channel(self, url):
        assert not youtube.is_channel_url(url)

    def test_the_listing_url_is_the_videos_tab(self):
        """Not the Atom feed: the tab is what excludes Shorts and streams."""
        url = youtube.channel_videos_url("UC6biysICWOJ-C3P4Tyeggzg")
        assert url.endswith("/videos")
        assert "UC6biysICWOJ-C3P4Tyeggzg" in url


def _entry(vid, duration=600, live=None, ts=1788000000, title="A video"):
    return {"id": vid, "title": title, "duration": duration,
            "live_status": live, "timestamp": ts,
            "url": f"https://www.youtube.com/watch?v={vid}"}


def _listing(entries):
    proc = MagicMock(stdout=json.dumps({"entries": entries}))
    return patch.object(youtube, "_run", return_value=proc)


class TestChannelListing:

    def test_regular_videos_come_through(self):
        with _listing([_entry("aaaaaaaaaaa"), _entry("bbbbbbbbbbb")]):
            vids = youtube._channel_videos_blocking("UC123", 15)
        assert [v["video_id"] for v in vids] == ["aaaaaaaaaaa", "bbbbbbbbbbb"]
        assert vids[0]["duration_seconds"] == 600
        assert vids[0]["published_at"] is not None

    def test_shorts_are_left_out(self):
        """The tab excludes them structurally; this is the backstop."""
        with _listing([_entry("shortshort", duration=45), _entry("longlonglon")]):
            vids = youtube._channel_videos_blocking("UC123", 15)
        assert [v["video_id"] for v in vids] == ["longlonglon"]

    @pytest.mark.parametrize("status", ["is_live", "is_upcoming", "was_live", "post_live"])
    def test_streams_are_left_out(self, status):
        with _listing([_entry("streamvideo", live=status), _entry("normalvideo")]):
            vids = youtube._channel_videos_blocking("UC123", 15)
        assert [v["video_id"] for v in vids] == ["normalvideo"]

    def test_an_entry_with_no_duration_is_skipped(self):
        """Usually a stream placeholder rather than something to listen to."""
        with _listing([_entry("nodurationx", duration=None), _entry("hasduration")]):
            vids = youtube._channel_videos_blocking("UC123", 15)
        assert [v["video_id"] for v in vids] == ["hasduration"]

    def test_a_missing_upload_date_is_not_fatal(self):
        """yt-dlp returns a null timestamp for some channels; verified live."""
        with _listing([_entry("notimestamp", ts=None)]):
            vids = youtube._channel_videos_blocking("UC123", 15)
        assert len(vids) == 1 and vids[0]["published_at"] is None

    def test_an_empty_channel(self):
        with _listing([]):
            assert youtube._channel_videos_blocking("UC123", 15) == []


class TestChannelIdentity:

    def test_a_feed_import_and_a_manual_add_are_the_same_episode(self):
        """Otherwise subscribing would duplicate every video already added."""
        from services import youtube_library
        from routers.youtube import _episode_id
        assert youtube_library.episode_id_for("uQV6hYwyjMY") == _episode_id("uQV6hYwyjMY")

    def test_the_channel_subscription_id_matches_the_one_videos_create(self):
        from services import youtube_library
        from routers.youtube import _podcast_id
        channel_id = "UC6biysICWOJ-C3P4Tyeggzg"
        assert youtube_library.podcast_id_for(channel_id) == _podcast_id({"channel_id": channel_id})

    def test_a_channel_that_cannot_be_resolved_is_an_error(self):
        proc = MagicMock(stdout=json.dumps({"title": "Something", "entries": []}))
        with patch.object(youtube, "_run", return_value=proc):
            with pytest.raises(youtube.YouTubeError, match="which channel"):
                youtube._resolve_channel_blocking("https://www.youtube.com/@mystery")

    def test_a_channel_id_is_recovered_from_the_url_when_absent(self):
        proc = MagicMock(stdout=json.dumps({
            "title": "Low Level",
            "channel_url": "https://www.youtube.com/channel/UC6biysICWOJ-C3P4Tyeggzg",
        }))
        with patch.object(youtube, "_run", return_value=proc):
            ch = youtube._resolve_channel_blocking("https://www.youtube.com/@LowLevelTV")
        assert ch["channel_id"] == "UC6biysICWOJ-C3P4Tyeggzg"
        assert ch["title"] == "Low Level"


@pytest.mark.asyncio
class TestChannelRefresh:
    """The refresh control on a channel page must not go through the RSS parser."""

    async def test_refreshing_a_channel_syncs_it(self, client):
        import sqlite3, database
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions (podcast_id, feed_url, title, subscribed_at) VALUES (?, ?, ?, ?)",
            ("yt-UC6biysICWOJ-C3P4Tyeggzg",
             "https://www.youtube.com/channel/UC6biysICWOJ-C3P4Tyeggzg/videos",
             "Low Level", "2026-01-01T00:00:00"),
        )
        conn.commit(); conn.close()

        with patch("services.youtube_library.sync_channel", AsyncMock(return_value={})) as sync, \
             patch("services.rss.fetch_episodes", AsyncMock(return_value=[])) as rss_fetch:
            r = await client.get("/podcasts/yt-UC6biysICWOJ-C3P4Tyeggzg/episodes?refresh=true")

        assert r.status_code == 200
        sync.assert_awaited_once()
        assert sync.await_args.args[0] == "UC6biysICWOJ-C3P4Tyeggzg"
        assert not rss_fetch.called      # the /videos URL is HTML, not a feed

    async def test_a_podcast_still_refreshes_through_rss(self, client, seeded_podcast_id):
        with patch("services.youtube_library.sync_channel", AsyncMock()) as sync, \
             patch("services.rss.fetch_episodes", AsyncMock(return_value=[])) as rss_fetch:
            r = await client.get(f"/podcasts/{seeded_podcast_id}/episodes?refresh=true")
        assert r.status_code == 200
        assert rss_fetch.called
        assert not sync.called

    async def test_a_failed_channel_refresh_surfaces_rather_than_silently_doing_nothing(self, client):
        import sqlite3, database
        conn = sqlite3.connect(database.DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions (podcast_id, feed_url, title, subscribed_at) VALUES (?, ?, ?, ?)",
            ("yt-UCbroken", "https://www.youtube.com/channel/UCbroken/videos", "Broken", "2026-01-01T00:00:00"),
        )
        conn.commit(); conn.close()

        with patch("services.youtube_library.sync_channel",
                   AsyncMock(side_effect=youtube.YouTubeError("rate limited"))):
            r = await client.get("/podcasts/yt-UCbroken/episodes?refresh=true")
        assert r.status_code == 502
        assert "rate limited" in r.json()["detail"]

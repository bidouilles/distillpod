"""
Unit tests for the speech-to-text adapter (backend/services/stt.py).

Both backends must return the same shape — [{word, start, end}] with the word's
leading space intact — because every downstream feature indexes into it.
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from config import settings
from services import stt


def _voxtral_response(segments, status=200):
    return httpx.Response(
        status,
        json={"model": "voxtral-mini-latest", "text": "...", "segments": segments},
        request=httpx.Request("POST", stt.MISTRAL_URL),
    )


WORDS = [
    {"type": "transcription_segment", "text": "Si", "start": 0.2, "end": 0.3, "speaker_id": None},
    {"type": "transcription_segment", "text": " vous", "start": 0.3, "end": 0.4, "speaker_id": None},
    {"type": "transcription_segment", "text": " avez", "start": 0.5, "end": 0.6, "speaker_id": None},
]


@pytest.fixture
def voxtral(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "stt_backend", "voxtral")
    monkeypatch.setattr(settings, "stt_model", "voxtral-mini-latest")
    monkeypatch.setattr(settings, "stt_language", "")
    monkeypatch.setattr(settings, "mistral_api_key", "test-key")
    # Stand in for the ffmpeg downmix so tests need no real audio.
    def fake_ffmpeg(audio_path, dest):
        Path(dest).write_bytes(b"fake mp3")
        return Path(dest)
    monkeypatch.setattr(stt, "_to_speech_audio", fake_ffmpeg)


# ── Backend selection ─────────────────────────────────────────────────────────

class TestBackendSelection:

    def test_auto_picks_voxtral_when_key_present(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_backend", "auto")
        monkeypatch.setattr(settings, "mistral_api_key", "sk-test")
        assert settings.stt == "voxtral"

    def test_auto_stays_local_without_key(self, monkeypatch):
        """Without a key, auto must never reach a hosted backend. Which local
        engine it lands on depends on whether the GPU extra is installed —
        see TestAutoPrefersGpuOverCpu."""
        monkeypatch.setattr(settings, "stt_backend", "auto")
        monkeypatch.setattr(settings, "mistral_api_key", "")
        assert settings.stt in ("mlx", "whisper")
        assert settings.stt != "voxtral"

    def test_explicit_whisper_wins_over_key(self, monkeypatch):
        """A key present must not silently override an explicit local-only choice."""
        monkeypatch.setattr(settings, "stt_backend", "whisper")
        monkeypatch.setattr(settings, "mistral_api_key", "sk-test")
        assert settings.stt == "whisper"

    def test_explicit_voxtral_without_key_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_backend", "voxtral")
        monkeypatch.setattr(settings, "mistral_api_key", "")
        with pytest.raises(stt.STTError, match="MISTRAL_API_KEY is not set"):
            stt.transcribe("/tmp/whatever.mp3")


# ── Voxtral ───────────────────────────────────────────────────────────────────

class TestVoxtral:

    def test_maps_segments_to_words(self, voxtral):
        with patch("httpx.post", return_value=_voxtral_response(WORDS)):
            out = stt.transcribe("/tmp/ep.mp3")
        assert out == [
            {"word": "Si", "start": 0.2, "end": 0.3},
            {"word": " vous", "start": 0.3, "end": 0.4},
            {"word": " avez", "start": 0.5, "end": 0.6},
        ]

    def test_preserves_leading_space(self, voxtral):
        """faster-whisper's convention; the app joins words without separators."""
        with patch("httpx.post", return_value=_voxtral_response(WORDS)):
            out = stt.transcribe("/tmp/ep.mp3")
        assert "".join(w["word"] for w in out) == "Si vous avez"

    def test_requests_word_granularity(self, voxtral):
        """Without this the API returns phrase spans, which breaks distill windows."""
        with patch("httpx.post", return_value=_voxtral_response(WORDS)) as p:
            stt.transcribe("/tmp/ep.mp3")
        assert p.call_args.kwargs["data"]["timestamp_granularities"] == "word"

    def test_sends_bearer_auth_and_model(self, voxtral):
        with patch("httpx.post", return_value=_voxtral_response(WORDS)) as p:
            stt.transcribe("/tmp/ep.mp3")
        assert p.call_args.kwargs["headers"]["Authorization"] == "Bearer test-key"
        assert p.call_args.kwargs["data"]["model"] == "voxtral-mini-latest"

    def test_language_omitted_when_unset(self, voxtral):
        with patch("httpx.post", return_value=_voxtral_response(WORDS)) as p:
            stt.transcribe("/tmp/ep.mp3")
        assert "language" not in p.call_args.kwargs["data"]

    def test_language_sent_when_set(self, voxtral, monkeypatch):
        monkeypatch.setattr(settings, "stt_language", "fr")
        with patch("httpx.post", return_value=_voxtral_response(WORDS)) as p:
            stt.transcribe("/tmp/ep.mp3")
        assert p.call_args.kwargs["data"]["language"] == "fr"

    def test_clamps_inverted_timings(self, voxtral):
        """0.1s quantisation occasionally rounds a short word's end below its start."""
        segs = [{"text": " me", "start": 1493.5, "end": 1493.4}]
        with patch("httpx.post", return_value=_voxtral_response(segs)):
            out = stt.transcribe("/tmp/ep.mp3")
        assert out[0]["end"] == out[0]["start"] == 1493.5

    def test_zero_length_words_kept(self, voxtral):
        """start == end is normal at 0.1s resolution; dropping them loses text."""
        segs = [{"text": " et", "start": 4.0, "end": 4.0}]
        with patch("httpx.post", return_value=_voxtral_response(segs)):
            assert len(stt.transcribe("/tmp/ep.mp3")) == 1

    def test_skips_entries_missing_timings(self, voxtral):
        segs = WORDS + [{"text": " x", "start": None, "end": None}]
        with patch("httpx.post", return_value=_voxtral_response(segs)):
            assert len(stt.transcribe("/tmp/ep.mp3")) == 3

    def test_http_error_raises(self, voxtral):
        resp = httpx.Response(401, text="unauthorized",
                              request=httpx.Request("POST", stt.MISTRAL_URL))
        with patch("httpx.post", return_value=resp):
            with pytest.raises(stt.STTError, match="401"):
                stt.transcribe("/tmp/ep.mp3")

    def test_network_error_raises(self, voxtral):
        with patch("httpx.post", side_effect=httpx.ConnectTimeout("timed out")):
            with pytest.raises(stt.STTError, match="request failed"):
                stt.transcribe("/tmp/ep.mp3")

    def test_empty_result_raises(self, voxtral):
        with patch("httpx.post", return_value=_voxtral_response([])):
            with pytest.raises(stt.STTError, match="no word timings"):
                stt.transcribe("/tmp/ep.mp3")

    def test_unreadable_audio_raises(self, monkeypatch, voxtral):
        def boom(audio_path, dest):
            raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"invalid data")
        monkeypatch.setattr(stt, "_to_speech_audio", boom)
        with pytest.raises(stt.STTError, match="ffmpeg could not read"):
            stt.transcribe("/tmp/broken.mp3")


class TestDownmix:

    def test_builds_16k_mono_ffmpeg_command(self, tmp_path):
        dest = tmp_path / "out.mp3"
        with patch("subprocess.run") as run:
            stt._to_speech_audio("/tmp/in.mp3", dest)
        argv = run.call_args[0][0]
        assert argv[argv.index("-ar") + 1] == "16000"
        assert argv[argv.index("-ac") + 1] == "1"
        assert argv[0] == "ffmpeg"


# ── faster-whisper ────────────────────────────────────────────────────────────

class TestWhisper:

    def test_maps_segment_words(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_backend", "whisper")
        word = MagicMock(word=" bonjour", start=1.0, end=1.4)
        segment = MagicMock(words=[word])
        model = MagicMock()
        model.transcribe.return_value = ([segment], None)
        monkeypatch.setattr(stt, "_get_model", lambda: model)
        monkeypatch.setattr(stt, "_model", model)
        assert stt.transcribe("/tmp/ep.mp3") == [{"word": " bonjour", "start": 1.0, "end": 1.4}]
        assert model.transcribe.call_args.kwargs["word_timestamps"] is True

    def test_empty_result_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_backend", "whisper")
        model = MagicMock()
        model.transcribe.return_value = ([], None)
        monkeypatch.setattr(stt, "_get_model", lambda: model)
        with pytest.raises(stt.STTError, match="no words"):
            stt.transcribe("/tmp/ep.mp3")


class TestLongEpisodeChunking:
    """Voxtral accepts at most 3 hours per request. Lex Fridman routinely runs
    past five, so long episodes are split and stitched."""

    def test_short_episode_takes_single_request(self, voxtral, monkeypatch):
        monkeypatch.setattr(stt, "_audio_duration", lambda p: 1645.0)
        with patch("httpx.post", return_value=_voxtral_response(WORDS)) as p:
            stt.transcribe("/tmp/ep.mp3")
        assert p.call_count == 1

    def test_long_episode_is_split(self, voxtral, monkeypatch, tmp_path):
        """A 5h21m episode (Lex #501) must not be sent as one request."""
        parts = []
        for i in range(6):
            f = tmp_path / f"part{i}.mp3"
            f.write_bytes(b"x")
            parts.append(f)
        monkeypatch.setattr(stt, "_audio_duration",
                            lambda p: 19317.0 if str(p).endswith("ep.mp3") else 3600.0)
        monkeypatch.setattr(stt, "_split_audio", lambda a, d: parts)
        with patch("httpx.post", return_value=_voxtral_response(WORDS)) as p:
            out = stt.transcribe("/tmp/ep.mp3")
        assert p.call_count == 6
        assert len(out) == 6 * len(WORDS)

    def test_chunk_timestamps_are_shifted_onto_the_episode_timeline(self, voxtral, monkeypatch, tmp_path):
        """Each chunk's timings come back relative to that chunk. Without an
        offset every hour would restart at 0 and every seek would be wrong."""
        parts = []
        for i in range(3):
            f = tmp_path / f"p{i}.mp3"
            f.write_bytes(b"x")
            parts.append(f)
        monkeypatch.setattr(stt, "_audio_duration",
                            lambda p: 10000.0 if str(p).endswith("ep.mp3") else 3600.0)
        monkeypatch.setattr(stt, "_split_audio", lambda a, d: parts)
        with patch("httpx.post", return_value=_voxtral_response(WORDS)):
            out = stt.transcribe("/tmp/ep.mp3")
        starts = [w["start"] for w in out]
        assert starts[0] == pytest.approx(0.2)          # chunk 0, unshifted
        assert starts[3] == pytest.approx(3600.2)       # chunk 1, +1h
        assert starts[6] == pytest.approx(7200.2)       # chunk 2, +2h
        assert starts == sorted(starts), "stitched timeline must be monotonic"

    def test_split_command_enforces_the_chunk_length(self, tmp_path):
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0)
            stt._split_audio("/tmp/in.mp3", tmp_path)
        argv = run.call_args[0][0]
        assert argv[argv.index("-f") + 1] == "segment"
        assert argv[argv.index("-segment_time") + 1] == str(stt.VOXTRAL_CHUNK_SECONDS)
        assert int(argv[argv.index("-segment_time") + 1]) <= 3 * 3600

    def test_unreadable_long_audio_raises(self, voxtral, monkeypatch):
        monkeypatch.setattr(stt, "_audio_duration", lambda p: 19317.0)
        def boom(a, d):
            raise subprocess.CalledProcessError(1, "ffmpeg", stderr=b"bad data")
        monkeypatch.setattr(stt, "_split_audio", boom)
        with pytest.raises(stt.STTError, match="ffmpeg could not read"):
            stt.transcribe("/tmp/ep.mp3")


class TestWhisperLanguage:
    """STT_LANGUAGE must apply to both backends, not just Voxtral."""

    def _run(self, monkeypatch, language):
        monkeypatch.setattr(settings, "stt_backend", "whisper")
        monkeypatch.setattr(settings, "stt_language", language)
        word = MagicMock(word=" bonjour", start=1.0, end=1.4)
        model = MagicMock()
        model.transcribe.return_value = ([MagicMock(words=[word])], None)
        monkeypatch.setattr(stt, "_get_model", lambda: model)
        stt.transcribe("/tmp/ep.mp3")
        return model.transcribe.call_args.kwargs

    def test_language_pinned_when_set(self, monkeypatch):
        assert self._run(monkeypatch, "fr")["language"] == "fr"

    def test_language_omitted_when_unset(self, monkeypatch):
        assert "language" not in self._run(monkeypatch, "")


# ── mlx-whisper (Apple Silicon GPU) ──────────────────────────────────────────

def _mlx_result(words):
    return {"segments": [{"words": words}]}


@pytest.fixture
def mlx(monkeypatch):
    monkeypatch.setattr(settings, "stt_backend", "mlx")
    monkeypatch.setattr(settings, "stt_language", "")
    monkeypatch.setattr(settings, "whisper_model", "medium")
    monkeypatch.setattr(settings, "mlx_model_repo", "")


class TestMlx:

    def _patch_mlx(self, monkeypatch, result):
        mod = MagicMock()
        mod.transcribe.return_value = result
        monkeypatch.setitem(__import__("sys").modules, "mlx_whisper", mod)
        return mod

    def test_maps_words(self, mlx, monkeypatch):
        mod = self._patch_mlx(monkeypatch, _mlx_result([
            {"word": " jeu", "start": 0.0, "end": 0.4, "probability": 0.19},
            {"word": " dans", "start": 0.4, "end": 0.58, "probability": 0.81},
        ]))
        assert stt.transcribe("/tmp/ep.mp3") == [
            {"word": " jeu", "start": 0.0, "end": 0.4},
            {"word": " dans", "start": 0.4, "end": 0.58},
        ]

    def test_casts_numpy_floats(self, mlx, monkeypatch):
        """mlx returns np.float64. transcriber.py json.dumps() the result
        straight into the DB, and numpy floats are not JSON-serialisable."""
        np = pytest.importorskip("numpy")
        self._patch_mlx(monkeypatch, _mlx_result(
            [{"word": " jeu", "start": np.float64(0.0), "end": np.float64(0.4)}]))
        out = stt.transcribe("/tmp/ep.mp3")
        assert type(out[0]["start"]) is float
        json.dumps(out)   # would raise on np.float64

    def test_derives_repo_from_whisper_model(self, mlx, monkeypatch):
        mod = self._patch_mlx(monkeypatch, _mlx_result([{"word": " a", "start": 0.0, "end": 0.1}]))
        stt.transcribe("/tmp/ep.mp3")
        assert mod.transcribe.call_args.kwargs["path_or_hf_repo"] == "mlx-community/whisper-medium-mlx"

    def test_pinned_repo_wins(self, mlx, monkeypatch):
        monkeypatch.setattr(settings, "mlx_model_repo", "mlx-community/whisper-large-v3-mlx")
        mod = self._patch_mlx(monkeypatch, _mlx_result([{"word": " a", "start": 0.0, "end": 0.1}]))
        stt.transcribe("/tmp/ep.mp3")
        assert mod.transcribe.call_args.kwargs["path_or_hf_repo"] == "mlx-community/whisper-large-v3-mlx"

    def test_language_pinned(self, mlx, monkeypatch):
        monkeypatch.setattr(settings, "stt_language", "fr")
        mod = self._patch_mlx(monkeypatch, _mlx_result([{"word": " a", "start": 0.0, "end": 0.1}]))
        stt.transcribe("/tmp/ep.mp3")
        assert mod.transcribe.call_args.kwargs["language"] == "fr"

    def test_word_timestamps_always_requested(self, mlx, monkeypatch):
        mod = self._patch_mlx(monkeypatch, _mlx_result([{"word": " a", "start": 0.0, "end": 0.1}]))
        stt.transcribe("/tmp/ep.mp3")
        assert mod.transcribe.call_args.kwargs["word_timestamps"] is True

    def test_empty_result_raises(self, mlx, monkeypatch):
        self._patch_mlx(monkeypatch, {"segments": []})
        with pytest.raises(stt.STTError, match="no words"):
            stt.transcribe("/tmp/ep.mp3")

    def test_engine_failure_raises(self, mlx, monkeypatch):
        mod = MagicMock()
        mod.transcribe.side_effect = RuntimeError("metal oom")
        monkeypatch.setitem(__import__("sys").modules, "mlx_whisper", mod)
        with pytest.raises(stt.STTError, match="mlx-whisper failed"):
            stt.transcribe("/tmp/ep.mp3")

    def test_missing_package_raises_actionable_error(self, mlx, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "mlx_whisper", None)
        with pytest.raises(stt.STTError, match="pip install mlx-whisper"):
            stt.transcribe("/tmp/ep.mp3")


class TestAutoPrefersGpuOverCpu:

    def test_auto_picks_mlx_when_available(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_backend", "auto")
        monkeypatch.setattr(settings, "mistral_api_key", "")
        monkeypatch.setattr(stt, "mlx_available", lambda: True)
        assert settings.stt == "mlx"

    def test_auto_falls_back_to_whisper_without_mlx(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_backend", "auto")
        monkeypatch.setattr(settings, "mistral_api_key", "")
        monkeypatch.setattr(stt, "mlx_available", lambda: False)
        assert settings.stt == "whisper"

    def test_key_still_wins_over_mlx(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_backend", "auto")
        monkeypatch.setattr(settings, "mistral_api_key", "sk-test")
        monkeypatch.setattr(stt, "mlx_available", lambda: True)
        assert settings.stt == "voxtral"

    def test_explicit_whisper_beats_available_gpu(self, monkeypatch):
        monkeypatch.setattr(settings, "stt_backend", "whisper")
        monkeypatch.setattr(stt, "mlx_available", lambda: True)
        assert settings.stt == "whisper"

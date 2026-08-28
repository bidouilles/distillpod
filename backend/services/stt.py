"""
Speech-to-text adapter — the single place audio becomes a word list.

Two backends, both returning the same shape the rest of the app depends on:
a list of {word, start, end} where `word` carries its own leading space.

  voxtral  Mistral's hosted API. Fast (a 27-minute episode in ~35s) and strong
           on non-English audio, but the audio leaves the machine.
  mlx      mlx-whisper on Apple Silicon's GPU. Local like whisper, but ~8x
           faster: measured 14.5x realtime with `medium`, against
           faster-whisper's 1.79x on the same machine and clip. macOS arm64.
  whisper  faster-whisper, local and private, but CPU-bound everywhere. It is
           built on CTranslate2, which has no Metal backend at all — `mps` and
           `metal` are rejected outright — so on a Mac it cannot touch the GPU
           whatever WHISPER_DEVICE says. Use `mlx` there instead.

`STT_BACKEND=auto` (the default) prefers voxtral when MISTRAL_API_KEY is set,
then mlx where it is importable, then whisper. That fallback only ever trades
one local engine for a faster local one, so it can never silently move audio
off the machine — reaching voxtral requires a key.
"""
import importlib.util
import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

import httpx

from config import settings

log = logging.getLogger(__name__)

MISTRAL_URL = "https://api.mistral.ai/v1/audio/transcriptions"

# Generous: covers a long episode plus upload. Voxtral chunks internally.
VOXTRAL_TIMEOUT = 900

# Voxtral accepts recordings up to 3 hours per request, and long uploads are
# slow and failure-prone well before that. Anything longer is split and the
# pieces stitched back together with their timestamps offset. Lex Fridman
# regularly runs past five hours, so this is not hypothetical.
#
# Splitting is not free: a word straddling a cut is lost, measured at ~1.5% of
# words when a 27-minute episode was forced into 10-minute pieces. So the
# threshold is deliberately high — episodes under an hour (the overwhelming
# majority) take the single-request path and pay nothing.
VOXTRAL_CHUNK_SECONDS = 3600


class STTError(RuntimeError):
    """Transcription failed. The caller marks the episode 'error'."""


def mlx_available() -> bool:
    """True when mlx-whisper can be imported — Apple Silicon with the extra installed."""
    return importlib.util.find_spec("mlx_whisper") is not None


def transcribe(audio_path: str) -> list[dict]:
    """Blocking. Returns [{word, start, end}, ...]; raises STTError on failure."""
    backend = settings.stt
    log.info("transcribing with %s", backend)
    if backend == "voxtral":
        return _transcribe_voxtral(audio_path)
    if backend == "mlx":
        return _transcribe_mlx(audio_path)
    return _transcribe_whisper(audio_path)


# ── Voxtral ───────────────────────────────────────────────────────────────────

def _to_speech_audio(audio_path: str, dest: Path) -> Path:
    """Downmix to 16 kHz mono at a low bitrate before upload.

    Speech recognition gains nothing from stereo or music-grade bitrates, and
    this turns a 25 MB episode into roughly 4 MB — which keeps long episodes
    under the API's upload limit and makes the request far quicker.
    """
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", audio_path,
         "-ar", "16000", "-ac", "1", "-b:a", "32k", str(dest), "-y"],
        check=True, capture_output=True,
    )
    return dest


def _audio_duration(path: str) -> float:
    """Seconds of audio, or 0.0 if ffprobe cannot tell (caller then assumes short)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            check=True, capture_output=True, text=True,
        )
        return float(out.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


def _split_audio(audio_path: str, outdir: Path) -> list[Path]:
    """Downmix and split into <= VOXTRAL_CHUNK_SECONDS pieces, in order."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-i", audio_path,
         "-ar", "16000", "-ac", "1", "-b:a", "32k",
         "-f", "segment", "-segment_time", str(VOXTRAL_CHUNK_SECONDS),
         "-reset_timestamps", "1",
         str(outdir / "part%04d.mp3"), "-y"],
        check=True, capture_output=True,
    )
    return sorted(outdir.glob("part*.mp3"))


def _transcribe_voxtral(audio_path: str) -> list[dict]:
    if not settings.mistral_api_key:
        raise STTError("STT_BACKEND=voxtral but MISTRAL_API_KEY is not set")

    with tempfile.TemporaryDirectory(prefix="distillpod-stt-") as tmp:
        tmpdir = Path(tmp)
        try:
            if _audio_duration(audio_path) > VOXTRAL_CHUNK_SECONDS:
                parts = _split_audio(audio_path, tmpdir)
            else:
                parts = [_to_speech_audio(audio_path, tmpdir / "audio.mp3")]
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or b"").decode("utf-8", "replace")[:200]
            raise STTError(f"ffmpeg could not read {audio_path}: {detail}") from exc

        if not parts:
            raise STTError(f"no audio produced from {audio_path}")

        words: list[dict] = []
        offset = 0.0
        for part in parts:
            chunk = _transcribe_one(part)
            # Timestamps come back relative to the chunk, so shift them onto the
            # episode timeline before appending.
            for w in chunk:
                w["start"] += offset
                w["end"] += offset
            words.extend(chunk)
            offset += _audio_duration(str(part))

    if not words:
        raise STTError("Voxtral returned no word timings — check the audio is speech")
    return _normalise(words)


def _transcribe_one(upload: Path) -> list[dict]:
    """One request against one file that is already downmixed and short enough.

    Timestamps are relative to this file; the caller shifts them.
    """
    data = {
        "model": settings.stt_model,
        # Without this the API returns coarse phrase spans; the app needs
        # per-word timings for distill windows and ad cuts.
        "timestamp_granularities": "word",
    }
    if settings.stt_language:
        data["language"] = settings.stt_language

    try:
        with upload.open("rb") as fh:
            resp = httpx.post(
                MISTRAL_URL,
                headers={"Authorization": f"Bearer {settings.mistral_api_key}"},
                data=data,
                files={"file": (upload.name, fh, "audio/mpeg")},
                timeout=VOXTRAL_TIMEOUT,
            )
    except httpx.HTTPError as exc:
        raise STTError(f"Voxtral request failed: {exc}") from exc

    if resp.status_code != 200:
        raise STTError(f"Voxtral returned {resp.status_code}: {resp.text[:300]}")

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise STTError(f"Voxtral returned non-JSON: {resp.text[:200]}") from exc

    # With granularity=word each "segment" is one word, and its text already
    # carries a leading space — the same convention faster-whisper uses.
    segments = payload.get("segments") or []
    return [
        {"word": s["text"], "start": float(s["start"]), "end": float(s["end"])}
        for s in segments
        if s.get("text") and s.get("start") is not None and s.get("end") is not None
    ]


def _normalise(words: list[dict]) -> list[dict]:
    """Enforce start <= end on every word.

    Voxtral quantises timings to 0.1s, which occasionally rounds a short word's
    end just below its start. Everything downstream — the distill window filter,
    the ad segmenter, the chapter seeker — assumes the interval is well formed.
    """
    for w in words:
        if w["end"] < w["start"]:
            w["end"] = w["start"]
    return words


# ── mlx-whisper (Apple Silicon GPU) ──────────────────────────────────────────

def _mlx_repo() -> str:
    """Map WHISPER_MODEL onto an MLX community repo, unless one is pinned."""
    return settings.mlx_model_repo or f"mlx-community/whisper-{settings.whisper_model}-mlx"


def _transcribe_mlx(audio_path: str) -> list[dict]:
    try:
        import mlx_whisper
    except ImportError as exc:
        raise STTError(
            "STT_BACKEND=mlx but mlx-whisper is not installed "
            "(pip install mlx-whisper; Apple Silicon only)"
        ) from exc

    kwargs = {"word_timestamps": True, "path_or_hf_repo": _mlx_repo()}
    if settings.stt_language:
        kwargs["language"] = settings.stt_language
    try:
        result = mlx_whisper.transcribe(audio_path, **kwargs)
    except Exception as exc:
        raise STTError(f"mlx-whisper failed: {exc}") from exc

    # mlx hands back numpy floats, which json.dumps cannot serialise — and the
    # caller writes this straight into the transcripts table. Cast here.
    words = [
        {"word": w["word"], "start": float(w["start"]), "end": float(w["end"])}
        for seg in result.get("segments", [])
        for w in seg.get("words", [])
        if w.get("word")
    ]
    if not words:
        raise STTError("mlx-whisper produced no words")
    return _normalise(words)


# ── faster-whisper ────────────────────────────────────────────────────────────

_model = None


def _get_model():
    """Imported lazily so a voxtral-only deployment needn't install the model."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type="int8",
        )
    return _model


def _transcribe_whisper(audio_path: str) -> list[dict]:
    # STT_LANGUAGE applies to both backends. Pinning it skips faster-whisper's
    # detection pass, which is a real risk on a bilingual feed: it samples only
    # the opening seconds, so an English sponsor read over a French episode can
    # flip the whole transcript to the wrong language.
    kwargs = {"word_timestamps": True}
    if settings.stt_language:
        kwargs["language"] = settings.stt_language
    try:
        model = _get_model()
        segments, _ = model.transcribe(audio_path, **kwargs)
    except ImportError as exc:
        raise STTError("faster-whisper is not installed; set MISTRAL_API_KEY to use voxtral") from exc
    words = []
    for segment in segments:
        if segment.words:
            for w in segment.words:
                words.append({"word": w.word, "start": w.start, "end": w.end})
    if not words:
        raise STTError("faster-whisper produced no words")
    return words

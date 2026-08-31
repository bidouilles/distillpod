"""Producing the clean cut of an episode, and saying what it did.

One pass, one output file, one mapping. It removes what the model flagged as
advertising and — where the podcast asks for it — the long pauses, and can
level the loudness on the way out.

Two decisions worth knowing:

**Silence is found by measuring, not by filtering.** ffmpeg's `silenceremove`
would do the job in a single filter, but it reports nothing about what it
removed, and a cut whose extent is unknown cannot be mapped back to the
transcript. `silencedetect` measures and prints, so the removal is expressed as
the same kind of keep-list the ad cut already produced, and both flow through
`services/timeline.py`.

**Pauses are shortened, not deleted.** Speech with every gap closed is
exhausting and often unintelligible — a question and its answer run together.
So a long pause keeps a beat of its length, which is what makes the result
listenable rather than merely shorter.
"""
import logging
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Quiet enough to be a pause rather than a soft passage, for long enough to be
# worth removing. Measured on speech, where -35dB sits below breath noise and
# room tone but above an actual voice.
SILENCE_THRESHOLD_DB = -35
MIN_SILENCE_SECONDS = 0.8

# What is left of a removed pause. Long enough to hear a boundary, short enough
# that the time saved is the point.
PAUSE_KEPT_SECONDS = 0.22

# Nothing shorter than this is worth a cut: each one is a seam in the audio and
# a row in the mapping.
MIN_TRIM_SECONDS = 0.35

# EBU R128 at the level streaming services settle on, so an episode mastered
# quietly sits at the same volume as everything else.
LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1.5:LRA=11"

_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def parse_silence(stderr: str) -> list[list[float]]:
    """The spans `silencedetect` reported, as `[[start, end], ...]`.

    ffmpeg prints one line per boundary, and a file that ends quietly gets a
    `silence_start` with no matching end — that trailing span is dropped rather
    than guessed at, since its length is unknown here.
    """
    starts: list[float] = []
    ends: list[float] = []
    for line in stderr.splitlines():
        m = _SILENCE_START.search(line)
        if m:
            starts.append(float(m.group(1)))
        m = _SILENCE_END.search(line)
        if m:
            ends.append(float(m.group(1)))
    spans = []
    for start, end in zip(starts, ends):
        if end > start:
            spans.append([max(0.0, start), end])
    return spans


def detect_silence(audio_path: str, threshold_db: int = SILENCE_THRESHOLD_DB,
                   min_seconds: float = MIN_SILENCE_SECONDS) -> list[list[float]]:
    """Measure the pauses in a file. Returns [] if ffmpeg cannot be run."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", audio_path,
             "-af", f"silencedetect=noise={threshold_db}dB:d={min_seconds}",
             "-f", "null", "-"],
            capture_output=True, text=True, timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("silence detection failed for %s: %s", audio_path, exc)
        return []
    return parse_silence(proc.stderr or "")


def _remove(keep: list[list[float]], spans: list[list[float]]) -> list[list[float]]:
    """Subtract `spans` from a keep-list."""
    result = keep
    for cut_start, cut_end in spans:
        next_result: list[list[float]] = []
        for start, end in result:
            if cut_end <= start or cut_start >= end:
                next_result.append([start, end])     # no overlap
                continue
            if cut_start > start:
                next_result.append([start, cut_start])
            if cut_end < end:
                next_result.append([cut_end, end])
        result = next_result
    return result


def build_keep_segments(
    total_duration: float,
    ads: list[dict] | None = None,
    silence: list[list[float]] | None = None,
    pause_kept: float = PAUSE_KEPT_SECONDS,
) -> list[list[float]]:
    """The spans of the original to keep, given what should go.

    Ads are removed with a second of air either side, since a model's boundary
    lands on a word rather than on the edit. Pauses are shortened rather than
    closed: `pause_kept` seconds of each one survive.
    """
    if total_duration <= 0:
        return []

    keep = [[0.0, float(total_duration)]]

    cuts: list[list[float]] = []
    for ad in ads or []:
        try:
            start = max(0.0, float(ad["start"]) - 1.0)
            end = min(float(ad["end"]) + 1.0, total_duration)
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            cuts.append([start, end])

    for span in silence or []:
        start, end = float(span[0]), float(span[1])
        # Leave a beat: take the middle of the pause, not all of it.
        keep_each_side = pause_kept / 2
        start += keep_each_side
        end -= keep_each_side
        if end - start >= MIN_TRIM_SECONDS:
            cuts.append([max(0.0, start), min(end, total_duration)])

    if not cuts:
        return keep

    from services.timeline import normalise
    merged = normalise(cuts) or []
    result = _remove(keep, merged)
    # Drop slivers: a fragment shorter than this is a click rather than audio,
    # and each one costs an ffmpeg invocation.
    return [s for s in result if s[1] - s[0] >= 0.5]


def probe_duration(audio_path: str) -> float:
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, timeout=30,
        )
        return float(probe.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("could not probe %s: %s", audio_path, exc)
        return 0.0


def render(audio_path: str, segments: list[list[float]], output_path: str,
           normalize: bool = False) -> list[float] | None:
    """Write the kept spans out as one file.

    Returns the duration each span actually became, or None on failure.

    Those measured lengths matter more than they look. Copying an MP3 stream
    cuts on frame boundaries, so every span comes out a few tens of
    milliseconds longer than asked for. One cut is inaudible; an episode with
    two hundred shortened pauses accumulates seconds, and the mapping would
    drift away from the file it describes until the read-along highlighted the
    wrong sentence. So the segments are corrected to what was written rather
    than to what was requested.

    Copies the stream unless the loudness is being levelled, which needs a real
    re-encode. That distinction matters on a small box: a copy is I/O, an encode
    of a three-hour episode is minutes of CPU.
    """
    if not segments or not Path(audio_path).exists():
        return None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            parts = []
            lengths: list[float] = []
            for i, (start, end) in enumerate(segments):
                part = f"{tmpdir}/seg_{i:04d}.mp3"
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(start), "-to", str(end),
                     "-i", audio_path, "-acodec", "copy", part],
                    capture_output=True, timeout=180, check=True,
                )
                parts.append(part)
                lengths.append(probe_duration(part) or (end - start))

            list_path = f"{tmpdir}/concat.txt"
            with open(list_path, "w") as f:
                for part in parts:
                    f.write(f"file '{part}'\n")

            codec = ["-af", LOUDNORM_FILTER, "-c:a", "libmp3lame", "-q:a", "4"] \
                if normalize else ["-acodec", "copy"]
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                 *codec, output_path],
                capture_output=True, timeout=1800, check=True,
            )
        if not (Path(output_path).exists() and Path(output_path).stat().st_size > 0):
            return None
        return lengths
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not render %s: %s", output_path, exc)
        return None


def process(audio_path: str, output_path: str, ads: list[dict] | None = None,
            trim_silence: bool = False, normalize: bool = False) -> dict | None:
    """Make the clean cut. Returns what it did, or None if there was nothing to do.

    `{"segments": [[s, e], ...], "removed_seconds": float, "silence_spans": int,
      "total_duration": float}` — the segments being the mapping every stored
    timestamp is translated through.
    """
    total = probe_duration(audio_path)
    if total <= 0:
        return None

    silence = detect_silence(audio_path) if trim_silence else []
    segments = build_keep_segments(total, ads=ads, silence=silence)
    if not segments:
        return None

    removed = total - sum(end - start for start, end in segments)
    # An encode is only worth it if it changes something audible. Levelling the
    # loudness does even when no time is removed, which is why it is checked
    # separately.
    if removed < 1.0 and not normalize:
        return None

    lengths = render(audio_path, segments, output_path, normalize=normalize)
    if not lengths:
        return None

    # Correct the mapping to the audio that was actually written.
    #
    # Two roundings are at work: cutting an MP3 stream lands on frame
    # boundaries, and concatenating the pieces pads each join. Neither is
    # audible on its own, but an episode with two hundred shortened pauses
    # accumulates seconds, and the mapping would drift away from the file it
    # describes until the read-along highlighted the wrong sentence.
    #
    # So each span keeps its start in the original and takes the length it came
    # out as, then all of them are scaled to the duration the finished file
    # actually reports. That makes the two clocks agree exactly at the end —
    # where drift would otherwise be worst — and leaves at most a frame of error
    # in the middle, which is inside a spoken word.
    rendered_total = sum(lengths)
    actual_total = probe_duration(output_path)
    scale = (actual_total / rendered_total) if rendered_total > 0 and actual_total > 0 else 1.0
    segments = [
        [start, round(start + length * scale, 3)]
        for (start, _), length in zip(segments, lengths)
    ]

    return {
        "segments": segments,
        "removed_seconds": round(removed, 2),
        "silence_spans": len(silence),
        "total_duration": round(total, 2),
    }

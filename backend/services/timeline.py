"""Translating between the original audio and a cut of it.

The ad-free file is a concatenation of the parts worth keeping, so it runs on a
different clock: at any point after the first cut, its position is behind the
original by however much was removed before it. Nothing accounted for that, so
with the ad-free version playing, everything driven by a timestamp was wrong by
minutes — the read-along drifted, chapter jumps landed in the wrong place, and a
distill or a bookmark captured a passage the listener had not heard.

That is the reason the cut list is now stored with the episode rather than
thrown away after the encode. It is the map between the two clocks:

    segments = [[0, 95.0], [155.0, 1180.0]]   # kept spans of the ORIGINAL

    original:  0 ────────── 95   (ad)   155 ─────────────── 1180
    cut:       0 ────────── 95 ──────────── 1120

Everything stored — playback positions, distills, bookmarks, chapters — is in
the original timeline, which is the one that exists whether or not a cut was
ever made. Conversion happens at the edges: on the way in from a player that is
playing the cut, and on the way out when seeking one.

`frontend/src/lib/timeline.ts` mirrors `to_original` and `to_cut` for the
player's own seeking and read-along. Keep the two in step; the tests here are
the specification.
"""
import json
from typing import Optional

Segments = list[list[float]]


def parse(raw: Optional[str]) -> Optional[Segments]:
    """Read a stored cut list. Returns None when there is nothing usable.

    Tolerant because a broken mapping must not make an episode unplayable: a
    caller with None simply treats the two clocks as identical, which is the
    behaviour from before the column existed.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return normalise(data)


def normalise(data) -> Optional[Segments]:
    """Sorted, non-empty, non-overlapping spans — or None."""
    if not isinstance(data, list):
        return None
    spans: Segments = []
    for item in data:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            start, end = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            continue
        if end > start:
            spans.append([start, end])
    if not spans:
        return None
    spans.sort(key=lambda s: s[0])
    merged: Segments = [spans[0]]
    for start, end in spans[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)   # overlapping: one span
        else:
            merged.append([start, end])
    return merged


def to_original(segments: Optional[Segments], cut_seconds: float) -> float:
    """Where a position in the cut falls in the original.

    Past the end of the cut, this returns the end of the last kept span — the
    honest answer, since there is no original time after it.
    """
    if not segments:
        return max(0.0, cut_seconds)
    remaining = max(0.0, cut_seconds)
    for start, end in segments:
        length = end - start
        if remaining <= length:
            return start + remaining
        remaining -= length
    return segments[-1][1]


def to_cut(segments: Optional[Segments], original_seconds: float) -> float:
    """Where a position in the original falls in the cut.

    A position inside a removed span has no place in the cut, so it snaps
    forward to where the audio resumes. Snapping forward rather than back
    matters for chapters: a chapter mark that lands inside a sponsor read
    should start the chapter, not replay the end of the previous one.
    """
    if not segments:
        return max(0.0, original_seconds)
    target = max(0.0, original_seconds)
    elapsed = 0.0
    for start, end in segments:
        if target < start:
            return elapsed                 # inside a removed span
        if target <= end:
            return elapsed + (target - start)
        elapsed += end - start
    return elapsed                          # after everything kept


def kept_duration(segments: Optional[Segments]) -> float:
    """How long the cut runs."""
    if not segments:
        return 0.0
    return sum(end - start for start, end in segments)


def removed_duration(segments: Optional[Segments], total: float) -> float:
    """How much of `total` the cut leaves out."""
    if not segments or total <= 0:
        return 0.0
    return max(0.0, total - kept_duration(segments))


async def resolve(db, episode_id: str, seconds: float, source: str) -> float:
    """A position as reported by a player, in the original timeline.

    `source="clean"` means it came from the ad-free/trimmed file, which runs on
    its own clock. Everything stored — positions, distills, bookmarks — uses the
    original, so this is the single conversion on the way in. An episode with no
    stored mapping is unchanged, which is also what happens for `"original"`.
    """
    if source != "clean":
        return seconds
    row = await db.execute_fetchone(
        "SELECT processed_segments FROM episodes WHERE id = ?", (episode_id,)
    )
    return to_original(parse(row["processed_segments"]) if row else None, seconds)

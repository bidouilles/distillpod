"""
Auto-snips — the moments worth keeping, picked without you being there.

A distill normally costs attention: you have to be listening, recognise the
moment, and tap within the minute. That works for the episode you are actually
playing and not at all for the four you subscribed to and never got round to.

This runs over a finished transcript and asks the agent for the handful of
moments that would have been worth tapping, storing each as an ordinary gist
row so the distills library, sharing and research all work on them unchanged.
The only difference is the `auto` flag, so the UI can say where they came from.

Timestamps come back from the model and are therefore not trusted: each one is
clamped into the episode and snapped to real transcript words, so a hallucinated
figure produces a slightly-off excerpt rather than a seek into nothing.
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from config import settings
from services import llm
# The dense-segment walk is the same job chapterizer already does: turn a word
# list into readable, timestamped blocks. Reusing it keeps the two features
# seeing the transcript the same way.
from services.chapterizer import _words_to_dense_segments

log = logging.getLogger(__name__)

# Roughly what a long episode costs in prompt. Same order as chapterizer's cap.
MAX_TRANSCRIPT_CHARS = 60_000
DEFAULT_MAX_SNIPS = 4

SNIPS_SCHEMA = {
    "type": "object",
    "properties": {
        "snips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_seconds": {"type": "number"},
                    "quote": {"type": "string"},
                    "insight": {"type": "string"},
                },
                "required": ["start_seconds", "quote", "insight"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["snips"],
    "additionalProperties": False,
}


def _transcript_text(words: list[dict]) -> str:
    """[MM:SS]-prefixed lines, sampled evenly if the episode is very long."""
    segments = _words_to_dense_segments(words, chunk_sec=90.0)
    lines = [
        f"[{int(s['start'] // 60):02d}:{int(s['start'] % 60):02d}] {s['text']}"
        for s in segments
    ]
    text = "\n".join(lines)
    if len(text) > MAX_TRANSCRIPT_CHARS:
        # Sample across the whole episode rather than truncating: the best
        # moment is as likely to be in the last ten minutes as the first.
        per_line = sum(len(l) for l in lines) / max(len(lines), 1)
        keep = max(1, int(MAX_TRANSCRIPT_CHARS / max(per_line, 1)))
        step = max(1, len(lines) // keep)
        text = "\n".join(lines[::step])
    return text


def _excerpt(words: list[dict], start: float, end: float) -> str:
    picked = [w for w in words if w["start"] >= start and w["end"] <= end + 1.0]
    return " ".join(w["word"].strip() for w in picked).strip()


def pick_snips(
    words_json: str,
    max_snips: int = DEFAULT_MAX_SNIPS,
) -> list[dict]:
    """Blocking. Returns [{start_seconds, end_seconds, text, summary}, ...].

    Empty list when there is nothing usable — no transcript, a failed call, or
    a reply with no snips in it. The caller treats that as "nothing to add"
    rather than an error, because a missing auto-snip costs the user nothing.
    """
    try:
        words = json.loads(words_json)
    except (TypeError, ValueError):
        return []
    if not words:
        return []

    duration = float(words[-1].get("end") or 0)
    if duration <= 0:
        return []

    total_minutes = int(duration // 60)
    prompt = (
        "You are picking out the moments in a podcast episode that a listener "
        "would want to save and come back to.\n\n"
        f"The transcript below uses [MM:SS] timestamps. Duration: {total_minutes} minutes.\n\n"
        f"TRANSCRIPT:\n{_transcript_text(words)}\n\n"
        f"Pick at most {max_snips} moments. Fewer is better than padding — only "
        "include a moment if it genuinely stands on its own.\n\n"
        "For each one give:\n"
        "- start_seconds: when the moment begins, in seconds from the start\n"
        "- quote: the most quotable sentence, verbatim from the transcript\n"
        "- insight: 1-2 sentences on why it matters\n\n"
        "Prefer concrete claims, surprising facts, numbers, strong opinions and "
        "advice. Skip introductions, sponsor reads, sign-offs and small talk."
    )

    data = llm.run_json(prompt, schema=SNIPS_SCHEMA, timeout=180, default=None)
    if not data:
        return []

    context = settings.gist_context_seconds
    out: list[dict] = []
    seen: list[float] = []

    for item in (data.get("snips") or [])[:max_snips]:
        try:
            start = float(item.get("start_seconds"))
        except (TypeError, ValueError):
            continue
        # Never trust a generated timestamp: clamp into the episode, and leave
        # room for the window so the excerpt is not empty at the very end.
        start = max(0.0, min(start, max(0.0, duration - 5.0)))
        end = min(duration, start + context)

        # Two "moments" a few seconds apart are one moment described twice.
        if any(abs(start - s) < context / 2 for s in seen):
            continue

        text = _excerpt(words, start, end)
        if not text:
            continue
        seen.append(start)
        out.append({
            "start_seconds": start,
            "end_seconds": end,
            "text": text,
            # Same shape manual distills store, so the UI renders both identically.
            "summary": json.dumps({
                "quote": (item.get("quote") or "").strip(),
                "insight": (item.get("insight") or "").strip(),
            }),
        })

    return out


def build_rows(
    snips: list[dict],
    episode_id: str,
    podcast_id: str,
    episode_title: str,
    podcast_title: str,
) -> list[tuple]:
    """Turn picked snips into gists rows, ready to executemany."""
    now = datetime.now(timezone.utc).isoformat()
    return [
        (str(uuid.uuid4()), episode_id, podcast_id, episode_title, podcast_title,
         s["start_seconds"], s["end_seconds"], s["text"], s["summary"], now, 1)
        for s in snips
    ]

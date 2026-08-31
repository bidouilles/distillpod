"""
Gist engine — extracts text from pre-computed transcript by timestamp range.
Zero latency, zero cost. Optionally generates a summary via the agent CLI.
"""
import json
import uuid
from datetime import datetime, timezone

from config import settings
from models import Gist
from services import llm
from services.transcriber import get_transcript_words

GIST_SCHEMA = {
    "type": "object",
    "properties": {
        "quote": {"type": "string"},
        "insight": {"type": "string"},
    },
    "required": ["quote", "insight"],
    "additionalProperties": False,
}


async def _summarize(text: str) -> str | None:
    """Ask the agent for a verbatim quote plus a short insight.

    Returns the JSON string stored on the gist, or None if the call failed —
    a missing summary degrades the gist, it doesn't invalidate it.
    """
    prompt = (
        "From this podcast transcript excerpt, extract two things:\n"
        "1. The single most memorable/quotable sentence — pick verbatim from the text\n"
        "2. A 1-2 sentence insight capturing the core idea\n\n"
        f"Transcript:\n{text}"
    )
    data = await llm.arun_json(prompt, schema=GIST_SCHEMA, timeout=60, default=None)
    return json.dumps(data) if data else None


async def create_gist(
    episode_id: str,
    podcast_id: str,
    episode_title: str,
    podcast_title: str,
    current_seconds: float,
    with_summary: bool = False,
) -> Gist:
    """
    Extract the last N seconds of transcript up to current_seconds.
    Optionally generates a summary via the agent CLI.
    """
    context = settings.gist_context_seconds
    start = max(0.0, current_seconds - context)
    end = current_seconds

    # A tap near the start of an episode used to capture only the seconds before
    # it: tapping at 0:05 produced a five-second quote that ended mid-sentence,
    # which is not a moment anyone can use — and research built on one has
    # nothing to work with. Where there is no room behind the tap, take the room
    # in front of it instead, so the window is always a usable length.
    if end - start < context:
        end = start + context

    words = await get_transcript_words(episode_id)
    if not words:
        raise ValueError(f"No transcript available for episode {episode_id}")

    # Never past the end of what was said.
    spoken_until = float(words[-1].get("end", end)) if words else end
    end = min(end, max(spoken_until, start + 1.0))

    # Filter words in time window
    shot_words = [w for w in words if w["start"] >= start and w["end"] <= end + 1.0]
    text = " ".join(w["word"].strip() for w in shot_words).strip()

    if not text:
        raise ValueError("No transcribed content in the selected time range")

    summary = await _summarize(text) if with_summary else None

    gist = Gist(
        id=str(uuid.uuid4()),
        episode_id=episode_id,
        podcast_id=podcast_id,
        episode_title=episode_title,
        podcast_title=podcast_title,
        start_seconds=start,
        end_seconds=end,
        text=text,
        summary=summary,
        created_at=datetime.now(timezone.utc),
    )
    return gist

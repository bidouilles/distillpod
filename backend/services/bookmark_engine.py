"""Turning "bookmark this" into a quote, using only the transcript.

Deliberately model-free. A distillation asks the agent CLI for a quote and an
insight and takes ~30 seconds; that is the right price for one moment you
really want, and the wrong one for the six you want on a drive. This costs a
lookup in the word-level transcript already on disk, so the button can be
tapped as often as it is useful.

The span is trimmed to sentence boundaries because a bookmark's whole job is to
be readable later, and a quote that starts mid-clause reads as a mis-hearing
rather than as something someone said.
"""
import re

# You press the button a beat *after* hearing the thing worth keeping, so the
# window leans backwards. A few seconds forward catch the end of the sentence
# still being spoken.
LOOKBACK_SECONDS = 28.0
LOOKAHEAD_SECONDS = 6.0

# A bookmark has to read as a quote, so it is measured in sentences and capped
# in characters. 320 is about three spoken sentences: enough for a claim and its
# qualifier, short enough to paste into a note without editing.
TARGET_CHARS = 320
MAX_CHARS = 600
MIN_KEEP_CHARS = 60

_SENTENCE_END = re.compile(r"[.!?…](?:[\"')\]]+)?\s*$")


def _text_of(words: list[dict]) -> str:
    return " ".join(w.get("word", "").strip() for w in words).strip()


def _sentences(words: list[dict]) -> list[list[dict]]:
    """Split a run of words at sentence-final punctuation."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    for w in words:
        current.append(w)
        if _SENTENCE_END.search(w.get("word", "")):
            groups.append(current)
            current = []
    if current:
        groups.append(current)      # trailing, incomplete sentence
    return groups


def extract(words: list[dict], seconds: float) -> dict | None:
    """The sentence(s) being spoken around `seconds`.

    Returns `{start_seconds, end_seconds, text}`, or None when the transcript
    has nothing in range — which happens at the very start of an episode and
    over long silences, and is not an error.

    The window is read from its end backwards, because the end is where the
    button was pressed: the last thing said is what was being kept.
    """
    if not words:
        return None

    lo = max(0.0, float(seconds) - LOOKBACK_SECONDS)
    hi = float(seconds) + LOOKAHEAD_SECONDS
    window = [w for w in words if w.get("end", 0) >= lo and w.get("start", 0) <= hi]
    if not window:
        return None

    groups = _sentences(window)

    # The window almost certainly opened mid-sentence, so the first group is a
    # fragment. Drop it — unless it is all there is, since a quote that starts
    # late still beats no quote.
    if len(groups) > 1 and len(_text_of([w for g in groups[1:] for w in g])) >= MIN_KEEP_CHARS:
        groups = groups[1:]

    # Likewise the last group may be a sentence still being spoken past the tap.
    if len(groups) > 1 and not _SENTENCE_END.search(groups[-1][-1].get("word", "")):
        if len(_text_of([w for g in groups[:-1] for w in g])) >= MIN_KEEP_CHARS:
            groups = groups[:-1]

    # Keep whole sentences from the end, within budget, always at least one.
    picked: list[list[dict]] = []
    total = 0
    for group in reversed(groups):
        text = _text_of(group)
        if picked and total + len(text) > TARGET_CHARS:
            break
        picked.insert(0, group)
        total += len(text) + 1

    flat = [w for g in picked for w in g]
    text = _text_of(flat)
    if not text:
        return None
    if len(text) > MAX_CHARS:
        # One unpunctuated run — auto-captions sometimes have no punctuation at
        # all. Keep the end, and say that something was cut.
        text = "…" + text[-MAX_CHARS:].lstrip()

    return {
        "start_seconds": round(float(flat[0].get("start", lo)), 2),
        "end_seconds": round(float(flat[-1].get("end", hi)), 2),
        "text": text,
    }

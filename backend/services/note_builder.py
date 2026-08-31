"""
Episode notes for Obsidian.

Everything worth keeping about an episode already exists somewhere: the
chapterizer's summary, timestamped chapters, and distills holding a verbatim
quote and an insight each. This assembles them into one Markdown note, and adds
the three things that only make sense in a written note — key points, the
things the episode mentioned, and a map of how the argument fits together.

The map is the reason the model is asked for structure rather than for Mermaid.
A model writing Mermaid directly emits syntax that fails to render often enough
to matter, and a broken diagram is worse than none in a vault. So it returns
labelled nodes and this module renders the diagram, which cannot be malformed.
"""
import json
import logging
import re
from typing import Optional

from services import llm
from services.auto_snipper import _transcript_text

log = logging.getLogger(__name__)

ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "key_points": {"type": "array", "items": {"type": "string"}},
        "mentioned": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["book", "paper", "tool", "person", "company", "link", "other"],
                    },
                    "detail": {"type": "string"},
                    "start_seconds": {"type": "number"},
                },
                "required": ["name", "kind", "detail", "start_seconds"],
                "additionalProperties": False,
            },
        },
        "map": {
            "type": "object",
            "properties": {
                "root": {"type": "string"},
                "branches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "children": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["label", "children"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["root", "branches"],
            "additionalProperties": False,
        },
    },
    "required": ["key_points", "mentioned", "map"],
    "additionalProperties": False,
}

KIND_ICON = {
    "book": "📚", "paper": "📄", "tool": "🛠", "person": "👤",
    "company": "🏢", "link": "🔗", "other": "•",
}


def enrich(words_json: str, title: str) -> Optional[dict]:
    """One model call for key points, mentions and the map. None on failure.

    Returning None is fine: the note still has the summary, the chapters and
    every highlight, which are all free.
    """
    try:
        words = json.loads(words_json)
    except (TypeError, ValueError):
        return None
    if not words:
        return None

    prompt = (
        f'You are writing study notes on a podcast episode titled "{title}".\n\n'
        f"The transcript below uses [MM:SS] timestamps.\n\nTRANSCRIPT:\n"
        f"{_transcript_text(words)}\n\n"
        "Produce three things.\n\n"
        "key_points: 4-8 substantive takeaways, each one sentence. Claims and "
        "conclusions, not a description of what the episode covers.\n\n"
        "mentioned: every book, paper, tool, product, company or person "
        "referred to by name, with a few words on what it is and the time in "
        "seconds when it comes up. Omit the hosts themselves and any sponsor. "
        "An empty list is correct when nothing is named.\n\n"
        "map: a short hierarchy of the episode's argument — a root label of at "
        "most six words, then 2-5 branches, each with up to 4 children. Keep "
        "every label under about eight words."
    )
    return llm.run_json(prompt, schema=ENRICH_SCHEMA, timeout=180, default=None)


BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "tldr": {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["tldr", "bullets"],
    "additionalProperties": False,
}


def brief(words_json: str, title: str) -> Optional[str]:
    """A couple of lines on what an episode is actually about.

    Deliberately a smaller, cheaper call than `enrich`: this one runs the first
    time an episode is opened, so it has to be quick, whereas the export note is
    asked for on purpose. A title alone often says nothing — half of them are a
    joke or a hook — and a YouTube description is mostly sponsor links.

    Returns the text to store in `episodes.summary`, or None on failure, which
    just leaves the page showing the original description as it does today.
    """
    try:
        words = json.loads(words_json)
    except (TypeError, ValueError):
        return None
    if not words:
        return None

    prompt = (
        f'Someone is deciding whether to listen to an episode titled "{title}". '
        "They can see the title and nothing else useful.\n\n"
        f"TRANSCRIPT:\n{_transcript_text(words)}\n\n"
        "tldr: one or two sentences on what this episode is actually about. "
        "Concrete and specific — name what is discussed. No preamble like "
        '"In this episode".\n\n'
        "bullets: 2-4 very short lines on what it covers. Fewer is fine. An "
        "empty list is fine when the tldr already says it."
    )
    data = llm.run_json(prompt, schema=BRIEF_SCHEMA, timeout=120, default=None)
    if not data:
        return None

    tldr = (data.get("tldr") or "").strip()
    bullets = [b.strip() for b in (data.get("bullets") or []) if b and b.strip()]
    if not tldr and not bullets:
        return None
    parts = [tldr] if tldr else []
    if bullets:
        parts.append("\n".join(f"• {b}" for b in bullets))
    return "\n\n".join(parts)


# ── Rendering ────────────────────────────────────────────────────────────────

def _fmt_time(seconds: float) -> str:
    s = max(0, int(seconds))
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _label(text: str) -> str:
    """Mermaid-safe node text: no quotes, brackets or newlines to break parsing.

    Returns "" for anything that cleans away to nothing, so callers drop the
    node. Substituting a placeholder here would put an empty box in the diagram
    instead of leaving it out.
    """
    clean = re.sub(r"[\"'\[\]{}()<>|]", "", (text or "").replace("\n", " ")).strip()
    return re.sub(r"\s+", " ", clean)[:60]


def render_mermaid(node_map: dict) -> str:
    """A flowchart from labelled nodes. Ids are generated, so labels are inert."""
    root = _label(node_map.get("root") or "")
    branches = node_map.get("branches") or []
    if not root or not branches:
        return ""

    lines = ["flowchart TD", f'    N0["{root}"]']
    n = 0
    for branch in branches[:5]:
        blabel = _label(branch.get("label") or "")
        if not blabel:
            continue
        n += 1
        bid = f"N{n}"
        lines.append(f'    {bid}["{blabel}"]')
        lines.append(f"    N0 --> {bid}")
        for child in (branch.get("children") or [])[:4]:
            clabel = _label(child)
            if not clabel:
                continue
            n += 1
            lines.append(f'    N{n}["{clabel}"]')
            lines.append(f"    {bid} --> N{n}")
    return "\n".join(lines) if n else ""


def _deep_link(audio_url: str, seconds: float) -> Optional[str]:
    """A link that lands on the moment, where the source supports one.

    YouTube takes ?t=; a podcast's MP3 URL does not, and a link that silently
    ignores its timestamp is worse than plain text.
    """
    if not audio_url or "youtu" not in audio_url:
        return None
    sep = "&" if "?" in audio_url else "?"
    return f"{audio_url}{sep}t={int(max(0, seconds))}s"


def _yaml(value: str) -> str:
    return '"' + (value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_markdown(
    episode: dict,
    chapters: list[dict],
    gists: list[dict],
    extras: Optional[dict] = None,
    bookmarks: Optional[list[dict]] = None,
) -> str:
    """Assemble the note. Every section is omitted when it has nothing in it."""
    title = episode.get("title") or "Untitled episode"
    audio_url = episode.get("audio_url") or ""
    show = episode.get("podcast_title") or ""
    published = (episode.get("published_at") or "")[:10]
    duration = episode.get("duration_seconds") or 0

    front = ["---", f"title: {_yaml(title)}"]
    if show:
        front.append(f"show: {_yaml(show)}")
    if audio_url:
        front.append(f"source: {audio_url}")
    if published:
        front.append(f"published: {published}")
    if duration:
        front.append(f"duration: {_fmt_time(duration)}")
    front += ["tags: [podcast, distillpod]", "---", ""]

    out = front + [f"# {title}", ""]

    meta = " · ".join(x for x in [show, published, _fmt_time(duration) if duration else ""] if x)
    if meta:
        out += [f"*{meta}*", ""]

    if episode.get("summary"):
        out += ["## Summary", "", episode["summary"].strip(), ""]

    if extras and extras.get("key_points"):
        out += ["## Key points", ""]
        out += [f"- {p.strip()}" for p in extras["key_points"] if p and p.strip()]
        out.append("")

    if extras and extras.get("map"):
        diagram = render_mermaid(extras["map"])
        if diagram:
            out += ["## Map", "", "```mermaid", diagram, "```", ""]

    if extras and extras.get("mentioned"):
        rows = []
        for m in extras["mentioned"]:
            name = (m.get("name") or "").strip()
            if not name:
                continue
            icon = KIND_ICON.get(m.get("kind"), "•")
            detail = (m.get("detail") or "").strip().replace("|", "\\|")
            at = m.get("start_seconds")
            link = _deep_link(audio_url, at) if at is not None else None
            when = f"[{_fmt_time(at)}]({link})" if link else (_fmt_time(at) if at is not None else "")
            rows.append(f"| {icon} {name.replace('|', chr(92) + '|')} | {detail} | {when} |")
        if rows:
            out += ["## Mentioned", "", "| What | Detail | Where |", "|---|---|---|", *rows, ""]

    if gists:
        out += ["## Highlights", ""]
        for g in gists:
            start = g.get("start_seconds") or 0
            link = _deep_link(audio_url, start)
            stamp = f"[{_fmt_time(start)}]({link})" if link else _fmt_time(start)
            quote, insight = "", ""
            try:
                parsed = json.loads(g.get("summary") or "")
                quote = (parsed.get("quote") or "").strip()
                insight = (parsed.get("insight") or "").strip()
            except (TypeError, ValueError, AttributeError):
                insight = (g.get("summary") or "").strip()
            body = quote or (g.get("text") or "").strip()
            if body:
                out.append(f"> {body}")
                out.append("")
            if insight:
                out.append(insight)
                out.append("")
            out.append(f"— {stamp}{'  *(auto)*' if g.get('auto') else ''}")
            out.append("")

    # Kept separate from Highlights rather than merged into one list. A distill
    # is the model's reading of a moment; a bookmark is the listener's. In a
    # vault six months later, which of the two said a thing is the difference
    # between a quote you can stand behind and one you have to go and check.
    if bookmarks:
        out += ["## Bookmarks", ""]
        for b in bookmarks:
            start = b.get("start_seconds") or 0
            link = _deep_link(audio_url, start)
            stamp = f"[{_fmt_time(start)}]({link})" if link else _fmt_time(start)
            text = (b.get("text") or "").strip()
            if not text:
                continue
            out += [f"> {text}", ""]
            note = (b.get("note") or "").strip()
            if note:
                out += [note, ""]
            out += [f"— {stamp}", ""]

    if chapters:
        out += ["## Chapters", ""]
        for ch in chapters:
            start = ch.get("start_time") or 0
            link = _deep_link(audio_url, start)
            stamp = f"[{_fmt_time(start)}]({link})" if link else _fmt_time(start)
            out.append(f"- {stamp} — {ch.get('title') or ''}")
        out.append("")

    out += ["---", "", f"*Exported from DistillPod*{f' · [source]({audio_url})' if audio_url else ''}", ""]
    return "\n".join(out)

"""Typesetting a research report.

A third renderer over the same stored structure as the page and the Markdown —
never a scrape of either. That is the whole reason the report is kept as data.

The layout is a briefing note, not a paper. What this produces is a model's
synthesis of web sources; giving it an abstract, an author list and a DOI would
claim a rigour it does not have. What it does have — a claim, a verdict,
evidence with numbered citations, and the questions left open — is worth
typesetting properly, and reads far better on a page than in a browser tab that
only resolves inside one tailnet.

Optional. With no `typst` binary the PDF is simply not offered; the Markdown
covers the same ground.
"""
import json
import logging
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

TEMPLATE = Path(__file__).parent.parent / "templates" / "report.typ"

# A three-page note compiles in well under a second; this is only a guard
# against a pathological input hanging the lane.
COMPILE_TIMEOUT = 60

VERDICT_LABELS = {
    "supported": "Supported",
    "mixed": "Mixed evidence",
    "contested": "Contested",
    "unsupported": "Not supported",
    "no_evidence": "No evidence found",
}

LABELS = {
    "en": {"open_questions": "Not settled by these sources",
           "echoes": "Elsewhere in your library",
           "sources": "Sources"},
    "fr": {"open_questions": "Ce que les sources ne tranchent pas",
           "echoes": "Ailleurs dans votre bibliothèque",
           "sources": "Sources"},
}

# Enough French function words that a sentence in French is unmistakable, and
# short enough that an English sentence quoting one of them is not.
_FRENCH = re.compile(
    r"\b(les|des|une|dans|pour|avec|est|sont|que|qui|plus|cette|leur|nous)\b",
    re.IGNORECASE,
)


def binary() -> str | None:
    return shutil.which(settings.typst_bin or "typst")


def available() -> bool:
    return bool(binary()) and TEMPLATE.exists()


def guess_language(text: str) -> str:
    """Which hyphenation and quotation rules to typeset with.

    A typographic nicety, not a claim about the content: French text set with
    English rules hyphenates in the wrong places and gets the wrong quotes.
    Two languages because those are the two this library is in.
    """
    hits = len(_FRENCH.findall(text or ""))
    return "fr" if hits >= 3 else "en"


def _fmt_time(seconds: float) -> str:
    total = int(seconds or 0)
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def payload(data: dict) -> dict:
    """Everything the template needs, already formatted.

    The template does no logic beyond looping: values arrive as strings and are
    inserted as text, never parsed as markup, so a transcript quote containing
    `#`, `[` or `*` cannot break the document.
    """
    report = data.get("report", {}) or {}
    episode = data.get("episode", {}) or {}
    claim = (data.get("claim") or "Research note").strip()
    language = data.get("language") or guess_language(
        claim + " " + report.get("verdict_note", "")
    )

    quote = (data.get("quote") or "").strip()
    if len(quote) > 420:
        quote = quote[:420].rsplit(" ", 1)[0] + " …"

    sources = []
    for source in data.get("sources", []) or []:
        url = source.get("url", "")
        domain = url.split("/")[2] if "//" in url else url
        dated = f" · {source['published'][:10]}" if source.get("published") else ""
        searched = f" · {source['query']}" if source.get("query") else ""
        sources.append({
            "title": source.get("title") or url,
            "url": url,
            "meta": f"{domain}{dated}{searched}",
        })

    echoes = []
    for echo in data.get("echoes", []) or []:
        echoes.append({
            "title": f"{echo.get('podcast_title', '')} — {echo.get('episode_title', '')}",
            "detail": f"{_fmt_time(echo.get('start', 0))} · {(echo.get('text') or '')[:180]}",
        })

    episode_line = " · ".join(
        x for x in (episode.get("podcast_title"), episode.get("title")) if x
    )

    return {
        "claim": claim,
        "episode_line": episode_line,
        "generated": datetime.now().strftime("%d %B %Y"),
        "verdict": report.get("verdict", "no_evidence"),
        "verdict_label": VERDICT_LABELS.get(report.get("verdict", ""), "Findings"),
        "verdict_note": (report.get("verdict_note") or "").strip(),
        "quote": quote,
        "sections": [
            {"heading": (s.get("heading") or "").strip(),
             "body": (s.get("body") or "").strip()}
            for s in report.get("sections", []) if (s.get("body") or "").strip()
        ],
        "open_questions": [q for q in (report.get("open_questions") or []) if q],
        "echoes": echoes,
        "sources": sources,
        "lang": language,
        "labels": LABELS.get(language, LABELS["en"]),
        "footer": (
            f"Synthesised by DistillPod from {len(sources)} web source"
            f"{'' if len(sources) == 1 else 's'}"
            + (f" · searched: {', '.join(data.get('queries', [])[:3])}"
               if data.get("queries") else "")
        ),
    }


def render(data: dict, out_path: Path) -> Path | None:
    """Compile the report to PDF. Returns the path, or None if it could not be.

    Never raises: a PDF is an extra, and failing to produce one must not take
    down the report it was made from.
    """
    if not available():
        return None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            work = Path(tmpdir)
            (work / "report.json").write_text(
                json.dumps(payload(data), ensure_ascii=False), encoding="utf-8"
            )
            shutil.copy(TEMPLATE, work / "report.typ")
            result = subprocess.run(
                [binary(), "compile", "--root", str(work),
                 str(work / "report.typ"), str(work / "report.pdf")],
                capture_output=True, text=True, timeout=COMPILE_TIMEOUT,
            )
            if result.returncode != 0 or not (work / "report.pdf").exists():
                log.warning("typst failed: %s", (result.stderr or "")[:500])
                return None
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(work / "report.pdf", out_path)
            return out_path
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not typeset the report: %s", exc)
        return None

"""Checking a distilled claim against the world outside the podcast.

The purpose is narrow and worth stating, because without it the feature drifts
into "write me an essay": you heard something interesting, and you want to know
whether it holds up. What actually happened, who says otherwise, and what has
been published since. Everything else in this app reasons about audio you
already have; this is the one feature that goes and looks elsewhere.

The first version produced a report that argued with itself — four sections
explaining that no sources had been provided — because `TAVILY_API_KEY` was
unset in production, every search returned an empty list, and nothing anywhere
treated "no results" as different from "no key". It then wrote the report,
marked the research `done`, and announced it on Telegram. Three rules came out
of that:

  * **A report is never written from nothing.** No key, or no sources, is a
    failure with a reason attached, not a document.
  * **The premise has to be worth researching.** A distillation is a quote and
    an insight, sometimes only a few seconds long. On its own that is not enough
    to search for, so the episode, its summary and the transcript around the
    moment go in too.
  * **Every claim carries its source.** Sections cite `[n]`, and the numbers
    link to the URLs they came from. A synthesis nobody can check is the thing
    the reader complained about.

It also answers a question the web cannot: whether the same subject came up
elsewhere in the library. That costs no model call — the retrieval built for
Ask already does it — and it is the part no other tool could produce.
"""
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx
import markdown

from config import settings
from services import llm

log = logging.getLogger(__name__)

QUERIES_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"}},
        "claim": {"type": "string"},
    },
    "required": ["queries", "claim"],
    "additionalProperties": False,
}

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["supported", "mixed", "contested", "unsupported", "no_evidence"],
        },
        "verdict_note": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["heading", "body"],
                "additionalProperties": False,
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "verdict_note", "sections", "open_questions"],
    "additionalProperties": False,
}

TAVILY_URL = "https://api.tavily.com/search"
MAX_QUERIES = 4
RESULTS_PER_QUERY = 5
# One site rarely has more than a couple of pages worth reading on a claim, and
# a `site:`-scoped query returns five variations of the same documentation. A cap
# keeps the source list something a person would actually scan.
MAX_PER_DOMAIN = 3
# Enough of a source for the model to reason about; short enough that a dozen
# of them still fit in one call.
SOURCE_CHARS = 1200
# Transcript either side of the distilled moment. A five-second quote is not a
# premise; a couple of minutes of what was actually being said is.
CONTEXT_BEFORE = 45.0
CONTEXT_AFTER = 120.0

VERDICT_LABELS = {
    "supported": ("Supported", "#22c55e"),
    "mixed": ("Mixed evidence", "#eab308"),
    "contested": ("Contested", "#f97316"),
    "unsupported": ("Not supported", "#ef4444"),
    "no_evidence": ("No evidence found", "#94a3b8"),
}


def available() -> bool:
    """Whether research can run at all.

    The agent CLI is sandboxed with no network, so a search API is the only way
    out of the box. Without it there is nothing to research *with*, which is a
    refusal rather than a degraded mode.
    """
    return bool(settings.tavily_api_key)


def _db():
    # Read through `database` rather than `settings` so this module sees the same
    # database as every other — the two diverge whenever the path is redirected.
    import database
    conn = sqlite3.connect(database.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _update(research_id: str, status: str, **fields) -> None:
    conn = _db()
    try:
        if fields:
            assignments = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE researches SET status = ?, {assignments} WHERE id = ?",
                [status, *fields.values(), research_id],
            )
        else:
            conn.execute("UPDATE researches SET status = ? WHERE id = ?", [status, research_id])
        conn.commit()
    finally:
        conn.close()


def _fail(research_id: str, reason: str) -> dict:
    """Record a failure, tell Telegram, and write no report."""
    log.warning("research %s failed: %s", research_id, reason)
    _update(research_id, "error", error=reason[:500],
            finished_at=datetime.now(timezone.utc).isoformat())
    return {"status": "error", "error": reason}


def _fmt_time(seconds: float) -> str:
    total = int(seconds)
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_premise(gist: dict, episode: dict, transcript_window: str) -> str:
    """What is being researched, in enough detail to be searchable.

    The distilled quote alone is often a fragment — "something happened that got
    brushed over in the news" names nothing a search engine can find. The
    episode title, its summary and the surrounding speech are what make it a
    subject.
    """
    parts = [f"Podcast episode: {episode.get('title') or 'Unknown episode'}"]
    if episode.get("podcast_title"):
        parts.append(f"Show: {episode['podcast_title']}")
    if episode.get("summary"):
        parts.append(f"What the episode is about: {episode['summary'].strip()}")

    quote = (gist.get("text") or "").strip()
    if quote:
        parts.append(f"The moment being checked, at {_fmt_time(gist.get('start_seconds') or 0)}:\n\"{quote}\"")

    insight = gist.get("summary")
    if insight:
        try:
            parsed = json.loads(insight)
            if parsed.get("quote"):
                parts.append(f"Quoted as: \"{parsed['quote']}\"")
            if parsed.get("insight"):
                parts.append(f"Read as: {parsed['insight']}")
        except (TypeError, ValueError):
            parts.append(f"Read as: {insight.strip()}")

    if transcript_window:
        parts.append(f"What was being said around it:\n{transcript_window}")
    return "\n\n".join(parts)


async def plan(premise: str) -> tuple[str, list[str]]:
    """The claim under test, and the searches that would test it."""
    prompt = (
        "Below is a moment from a podcast that a listener wants checked against "
        "outside sources.\n\n"
        f"{premise}\n\n"
        "Return:\n"
        "- \"claim\": one sentence stating the checkable claim or question this "
        "moment raises. Name the actual subject; never write \"the speaker "
        "claims something happened\".\n"
        "- \"queries\": 2 to 4 web searches that would establish whether it "
        "holds up. Concrete and specific: name the people, companies, papers or "
        "events involved. No years unless the moment named one, and no words "
        "like \"research\" or \"analysis\" padding them out. No search "
        "operators — a site: query comes back as five pages of the same "
        "documentation.\n\n"
        "If the moment is too vague to check, say so in \"claim\" and make the "
        "queries about the episode's actual subject instead."
    )
    data = await llm.arun_json(prompt, schema=QUERIES_SCHEMA, timeout=120, default=None)
    if not data:
        return "", []
    claim = (data.get("claim") or "").strip()
    queries = [q.strip() for q in data.get("queries", []) if isinstance(q, str) and q.strip()]
    return claim, queries[:MAX_QUERIES]


async def search_web(queries: list[str]) -> list[dict]:
    """Sources for every query, deduplicated by URL, in first-seen order.

    A failed search is logged and skipped rather than swallowed silently: the
    caller counts what came back and refuses to write a report from nothing.
    """
    sources: dict[str, dict] = {}
    per_domain: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for query in queries:
            try:
                r = await client.post(TAVILY_URL, json={
                    "api_key": settings.tavily_api_key,
                    "query": query,
                    "search_depth": "advanced",
                    "max_results": RESULTS_PER_QUERY,
                })
                r.raise_for_status()
                results = r.json().get("results", [])
            except Exception as exc:
                log.warning("search failed for %r: %s", query, exc)
                continue
            for result in results:
                url = (result.get("url") or "").strip()
                if not url or url in sources:
                    continue
                domain = url.split("/")[2] if "//" in url else url
                if per_domain.get(domain, 0) >= MAX_PER_DOMAIN:
                    continue
                per_domain[domain] = per_domain.get(domain, 0) + 1
                sources[url] = {
                    "url": url,
                    "title": (result.get("title") or url)[:200],
                    "content": (result.get("content") or "")[:SOURCE_CHARS],
                    "published": result.get("published_date") or "",
                    "query": query,
                }
    return list(sources.values())


async def synthesise(claim: str, premise: str, sources: list[dict]) -> dict | None:
    """One call over every source, so the sections can actually compare them."""
    blocks = []
    for i, source in enumerate(sources, start=1):
        dated = f" ({source['published'][:10]})" if source.get("published") else ""
        blocks.append(f"[{i}] {source['title']}{dated}\n{source['url']}\n{source['content']}")
    numbered = "\n\n".join(blocks)
    prompt = (
        "You are checking a claim from a podcast against web sources a listener "
        "cannot be bothered to read themselves. Be useful, not diplomatic.\n\n"
        f"The claim: {claim or 'see the moment below'}\n\n"
        f"The moment it came from:\n{premise}\n\n"
        f"Sources:\n{numbered}\n\n"
        "Return:\n"
        "- \"verdict\": one of supported, mixed, contested, unsupported, "
        "no_evidence — judging the claim, not the quality of the sources.\n"
        "- \"verdict_note\": one or two sentences saying why, citing sources.\n"
        "- \"sections\": 2 to 4 sections, each with a heading and a body of one "
        "or two prose paragraphs. Cover what actually happened, what is "
        "disputed, and what it means. Markdown in the body is fine.\n"
        "- \"open_questions\": what the sources do not settle. Empty if nothing.\n\n"
        "Rules:\n"
        "- Cite every factual claim inline as [1], [2]. A sentence with no "
        "citation must be your own reasoning, and should read as such.\n"
        "- Where sources disagree, say who says what rather than averaging them.\n"
        "- Never invent a fact, a date, a number or a source.\n"
        "- If the sources are about a different subject than the claim, say that "
        "plainly and set the verdict to no_evidence.\n"
        "- No preamble, no restating the question, no closing summary."
    )
    return await llm.arun_json(prompt, schema=REPORT_SCHEMA, timeout=300, default=None)


async def library_echoes(episode_id: str, claim: str, queries: list[str]) -> list[dict]:
    """Other moments in the library that are genuinely about the same subject.

    This asked with the claim as a single keyword query, which is how a report
    about AI-discovered vulnerabilities came to cite an episode about telephone
    scammers: a French sentence ORed token by token matches everything, since
    "les", "des" and "pour" appear in every transcript.

    So it asks with the planned searches — short and specific, the same ones the
    web was searched with — and demands corroboration: a passage has to be found
    by two of them, or by meaning as well as by word. A weak match is worse than
    no section, because it claims you heard something you did not.
    """
    from database import get_db
    from services import librarian

    terms = [q for q in queries if q.strip()][:MAX_QUERIES]
    if not terms:
        return []

    db = await get_db()
    try:
        passages = await librarian.gather(
            db, terms, question=claim or " ".join(terms), min_signals=2,
        )
    except Exception as exc:
        log.warning("library cross-reference failed: %s", exc)
        return []
    finally:
        await db.close()
    return [p for p in passages if p["episode_id"] != episode_id][:5]


def _linkify(text: str) -> str:
    """Turn [n] into a link to the numbered source."""
    html = markdown.markdown(text or "", extensions=["extra"])
    return re.sub(
        r"\[(\d+)\]",
        lambda m: f'<a class="cite" href="#source-{m.group(1)}">[{m.group(1)}]</a>',
        html,
    )


def build_markdown(*, claim: str, report: dict, sources: list[dict],
                   echoes: list[dict], episode: dict, gist: dict,
                   queries: list[str]) -> str:
    """The same report as text, for pasting anywhere.

    A finished report used to exist only as a page to look at. Markdown is what
    makes it portable — into a note, a message, an issue — and it is also the
    honest substrate: every other rendering, including a typeset one, is built
    from the same structure rather than scraped back out of HTML.
    """
    verdict_label = VERDICT_LABELS.get(report.get("verdict", "no_evidence"),
                                       VERDICT_LABELS["no_evidence"])[0]
    show = episode.get("podcast_title") or ""
    title = episode.get("title") or ""

    out: list[str] = [f"# {claim or 'Research report'}", ""]
    meta = " · ".join(x for x in (show, title) if x)
    if meta:
        out += [f"*{meta}*", ""]
    out += [f"**{verdict_label}** — {report.get('verdict_note', '').strip()}", ""]

    quote = (gist.get("text") or "").strip()
    if quote:
        if len(quote) > 420:
            quote = quote[:420].rsplit(" ", 1)[0] + " …"
        out += ["> " + quote.replace("\n", " "), ""]

    for section in report.get("sections", []):
        out += [f"## {section.get('heading', '').strip()}", "",
                section.get("body", "").strip(), ""]

    questions = report.get("open_questions") or []
    if questions:
        out += ["## Not settled by these sources", ""]
        out += [f"- {q}" for q in questions]
        out.append("")

    if echoes:
        out += ["## Elsewhere in your library", ""]
        for echo in echoes:
            out.append(
                f"- **{echo['podcast_title']}** — {echo['episode_title']} "
                f"({_fmt_time(echo['start'])})"
            )
        out.append("")

    out += ["## Sources", ""]
    for i, source in enumerate(sources, start=1):
        dated = f", {source['published'][:10]}" if source.get("published") else ""
        out.append(f"{i}. [{source['title']}]({source['url']}){dated}")
    out.append("")

    if queries:
        out += [f"*Searched: {', '.join(queries)}*", ""]
    out += ["*Generated by DistillPod*", ""]
    return "\n".join(out)


def build_html(*, claim: str, report: dict, sources: list[dict], echoes: list[dict],
               episode: dict, gist: dict, queries: list[str]) -> str:
    verdict_label, verdict_colour = VERDICT_LABELS.get(
        report.get("verdict", "no_evidence"), VERDICT_LABELS["no_evidence"])

    sections_html = "".join(
        f'<section class="topic"><h2>{s.get("heading", "")}</h2>'
        f'<div class="synthesis">{_linkify(s.get("body", ""))}</div></section>'
        for s in report.get("sections", [])
    )

    questions = report.get("open_questions") or []
    questions_html = (
        '<section class="topic"><h2>Not settled by these sources</h2><ul>'
        + "".join(f"<li>{q}</li>" for q in questions)
        + "</ul></section>"
    ) if questions else ""

    source_items = []
    for i, source in enumerate(sources, start=1):
        domain = source["url"].split("/")[2] if "//" in source["url"] else ""
        dated = f" · {source['published'][:10]}" if source.get("published") else ""
        source_items.append(
            f'<li id="source-{i}">'
            f'<a href="{source["url"]}" target="_blank" rel="noopener">{source["title"]}</a>'
            f'<div class="src-meta">{domain}{dated} · searched: {source["query"]}</div>'
            f'</li>'
        )
    sources_html = "".join(source_items)

    echoes_html = ""
    if echoes:
        rows = "".join(
            f'<li><strong>{e["podcast_title"]}</strong> — {e["episode_title"]} '
            f'<span class="src-meta">at {_fmt_time(e["start"])}</span>'
            f'<div class="echo">{e["text"][:400]}</div></li>'
            for e in echoes
        )
        echoes_html = (
            '<section class="topic"><h2>Elsewhere in your library</h2>'
            f'<ul class="echoes">{rows}</ul></section>'
        )

    quote = (gist.get("text") or "").strip()
    if len(quote) > 420:
        quote = quote[:420].rsplit(" ", 1)[0] + " …"
    generated_at = datetime.now().strftime("%B %d, %Y at %H:%M")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Research: {claim[:70] or episode.get('title', 'Report')}</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          max-width: 820px; margin: 0 auto; padding: 1.5rem 1rem;
          background: #1a1a1a; color: #e5e5e5; line-height: 1.7; }}
  h1 {{ color: #FFD700; font-size: 1.45rem; margin-bottom: 0.25rem; }}
  h2 {{ color: #FFD700; margin-top: 0; font-size: 1.15rem; }}
  .meta {{ color: #888; font-size: 0.85rem; margin-bottom: 1.25rem; }}
  .claim {{ background: #242424; border-left: 4px solid #FFD700;
            padding: 1rem 1.25rem; border-radius: 0 8px 8px 0; margin-bottom: 1rem; }}
  .claim blockquote {{ margin: 0.5rem 0 0; color: #bbb; font-style: italic;
                       border-left: 2px solid #444; padding-left: 0.75rem; }}
  .verdict {{ display: inline-block; font-weight: 700; font-size: 0.8rem;
              letter-spacing: 0.04em; text-transform: uppercase;
              padding: 0.25rem 0.6rem; border-radius: 999px;
              color: #111; background: {verdict_colour}; margin-bottom: 0.75rem; }}
  .topic {{ background: #242424; border-radius: 8px; padding: 1.1rem 1.35rem;
            margin-bottom: 1rem; }}
  .synthesis p {{ margin: 0.6rem 0; }}
  .cite {{ color: #FFD700; text-decoration: none; font-size: 0.8em;
           vertical-align: super; padding: 0 0.1em; }}
  .sources {{ background: #242424; border-radius: 8px; padding: 1.1rem 1.35rem; }}
  .sources ol {{ padding-left: 1.4rem; }}
  .sources li {{ margin-bottom: 0.7rem; font-size: 0.92rem; }}
  .src-meta {{ color: #777; font-size: 0.8rem; }}
  .echoes li {{ margin-bottom: 0.8rem; }}
  .echo {{ color: #aaa; font-size: 0.85rem; margin-top: 0.2rem; }}
  ul {{ padding-left: 1.3rem; }}
  a {{ color: #60a5fa; }}
  .footer {{ color: #555; font-size: 0.78rem; margin-top: 2rem; text-align: center; }}
</style>
</head>
<body>
<h1>{claim or 'Research report'}</h1>
<div class="meta">{episode.get('podcast_title') or ''}{' · ' if episode.get('podcast_title') else ''}
  {episode.get('title') or ''} · Generated {generated_at}</div>

<div class="claim">
  <div class="verdict">{verdict_label}</div>
  <div>{_linkify(report.get('verdict_note', ''))}</div>
  {f'<blockquote>{quote}</blockquote>' if quote else ''}
</div>

{sections_html}
{questions_html}
{echoes_html}

<div class="sources">
  <h2>Sources</h2>
  <ol>{sources_html}</ol>
  <div class="src-meta" style="margin-top:0.75rem">
    Searched: {', '.join(queries) if queries else '—'}
  </div>
</div>

<div class="footer">DistillPod · {len(sources)} source(s) via Tavily, synthesis via the agent CLI</div>
</body>
</html>"""


async def notify(text: str) -> None:
    if not (settings.telegram_bot_token and settings.telegram_chat_id):
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                data={"chat_id": settings.telegram_chat_id, "text": text},
            )
    except Exception as exc:
        log.warning("telegram notification failed: %s", exc)


async def run_research(research_id: str, gist_id: str) -> dict:
    """Research one distilled moment. Never raises; records why it stopped."""
    if not available():
        return _fail(research_id,
                     "Research needs a TAVILY_API_KEY: the agent CLI has no "
                     "network, so a search API is the only way to look outside "
                     "the episode.")

    conn = _db()
    try:
        gist = conn.execute(
            "SELECT g.*, e.summary AS episode_summary, e.title AS ep_title, "
            "       s.title AS podcast_title "
            "  FROM gists g "
            "  LEFT JOIN episodes e      ON e.id = g.episode_id "
            "  LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id "
            " WHERE g.id = ?", (gist_id,),
        ).fetchone()
        if not gist:
            return _fail(research_id, "That distillation no longer exists.")
        gist = dict(gist)
        transcript_row = conn.execute(
            "SELECT words_json FROM transcripts WHERE episode_id = ?",
            (gist["episode_id"],),
        ).fetchone()
    finally:
        conn.close()

    # The speech around the moment, which is what makes a fragment searchable.
    window = ""
    if transcript_row:
        try:
            words = json.loads(transcript_row["words_json"])
            lo = max(0.0, (gist["start_seconds"] or 0) - CONTEXT_BEFORE)
            hi = (gist["end_seconds"] or 0) + CONTEXT_AFTER
            window = " ".join(
                w.get("word", "").strip() for w in words
                if lo <= w.get("start", 0) <= hi
            ).strip()[:4000]
        except (TypeError, ValueError):
            window = ""

    episode = {
        "title": gist.get("ep_title") or gist.get("episode_title"),
        "podcast_title": gist.get("podcast_title"),
        "summary": gist.get("episode_summary"),
    }
    premise = build_premise(gist, episode, window)

    _update(research_id, "running")

    claim, queries = await plan(premise)
    if not queries:
        return _fail(research_id, "Could not work out what to search for.")

    sources = await search_web(queries)
    if not sources:
        await notify(
            f"🔬 Research failed\n\n{episode['title']}\n"
            f"No web sources came back for: {', '.join(queries)}"
        )
        return _fail(
            research_id,
            "No web sources came back for " + ", ".join(f'"{q}"' for q in queries)
            + ". A report written from nothing would say nothing, so none was written.",
        )

    report = await synthesise(claim, premise, sources)
    if not report:
        return _fail(research_id, "The model did not return a usable report.")

    echoes = await library_echoes(gist["episode_id"], claim, queries)

    html = build_html(claim=claim, report=report, sources=sources, echoes=echoes,
                      episode=episode, gist=gist, queries=queries)

    reports_dir = Path(settings.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    file_path = reports_dir / f"{research_id}.html"
    file_path.write_text(html, encoding="utf-8")
    public_url = f"{settings.public_url}/reports/{research_id}.html"

    _update(
        research_id, "done", file_path=str(file_path), public_url=public_url,
        finished_at=datetime.now(timezone.utc).isoformat(),
        report_json=json.dumps({
            "claim": claim, "report": report, "sources": sources,
            "echoes": echoes, "episode": episode, "queries": queries,
            "quote": (gist.get("text") or "").strip(),
        }),
    )

    verdict_label = VERDICT_LABELS.get(report.get("verdict", ""), ("", ""))[0]
    await notify(
        f"🔬 {verdict_label}: {claim[:120]}\n\n"
        f"{episode['title']}\n{len(sources)} sources\n\n{public_url}"
    )
    return {"status": "done", "public_url": public_url, "verdict": report.get("verdict")}

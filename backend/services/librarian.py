"""Answering a question from the whole library.

Episode chat can only answer about the episode in front of you. The question
worth asking a library of three hundred episodes is the one that crosses them:
"what have I heard about evaluating models?" — and answering it needs retrieval
before generation, because no model call can hold three hundred transcripts.

Three steps, in the order that keeps the expensive one small:

  1. Turn the question into keyword searches (one model call, tiny prompt).
     A question is prose; FTS5 matches words people actually said, and the
     question's own wording rarely matches the answer's.
  2. Retrieve passages with the transcript index that already exists, ranked by
     how many of those searches agree on them.
  3. Answer from those passages only, citing them (one model call).

The citations are the point as much as the answer. Every claim carries the
episode and the second it came from, so it can be checked against the audio —
which is what makes this trustworthy in a way a summary of a summary is not.
"""
import logging

from services import llm
from services.transcript_search import find_matches, fts_any_query

log = logging.getLogger(__name__)

QUERIES_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["queries"],
    "additionalProperties": False,
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "cited": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["answer", "cited"],
    "additionalProperties": False,
}

# How many episodes each search may pull, and how many passages reach the model.
EPISODES_PER_QUERY = 6
SNIPPETS_PER_EPISODE = 2
MAX_PASSAGES = 20
MAX_CONTEXT_CHARS = 14_000

# Wider than the search screen's window: the model has to see what was being
# said around a hit, not just that the word occurred.
PASSAGE_RADIUS = 45

# Passages within this many seconds of each other in the same episode are the
# same moment found twice, not two sources.
SAME_MOMENT_SECONDS = 45.0

NOTHING_FOUND = (
    "I could not find anything about that in your transcribed episodes. "
    "It may be in an episode that has not been transcribed yet, or worth "
    "asking in different words."
)


async def plan_queries(question: str, history: list[dict] | None = None) -> list[str]:
    """Keyword searches that would find the answer.

    Kept deliberately cheap — a short prompt and a handful of words back — and
    given the recent conversation so a follow-up like "what about the cost?"
    still searches for the subject rather than for the pronoun.
    """
    recent = ""
    for message in (history or [])[-4:]:
        recent += f"{message['role']}: {message['content'][:400]}\n"

    prompt = (
        "Someone is searching their own podcast library. Turn their question "
        "into the keyword searches most likely to find the passages that "
        "answer it.\n\n"
        + (f"Recent conversation:\n{recent}\n" if recent else "")
        + f"Question: {question}\n\n"
        "Return 2 to 5 searches under the \"queries\" key. Rules:\n"
        "- Keywords, not sentences. One to four words each.\n"
        "- Use the words a speaker would actually say out loud, not formal or "
        "academic phrasing.\n"
        "- Include an obvious synonym or two, since the transcript may use a "
        "different word than the question does.\n"
        "- No punctuation, no quotes, no boolean operators."
    )
    data = await llm.arun_json(prompt, schema=QUERIES_SCHEMA, timeout=90, default=None)
    queries = [q.strip() for q in (data or {}).get("queries", []) if isinstance(q, str) and q.strip()]

    # A failed or empty plan must not end the request: the question's own words
    # are a worse search than a planned one, but far better than nothing.
    if not queries:
        queries = [question.strip()]
    return queries[:5]


async def gather(db, queries: list[str]) -> list[dict]:
    """Passages matching any of `queries`, best first.

    Ranked by how many distinct searches found the same moment. That is the
    cheap stand-in for relevance: a passage two independent searches agree on is
    far more likely to be about the subject than one matching a single common
    word.
    """
    found: dict[tuple[str, int], dict] = {}

    for query in queries:
        # ANY of the words, matched as prefixes: the queries are a model's guess
        # at the vocabulary a speaker used, so requiring every word to appear
        # verbatim throws away most of the library.
        match = fts_any_query(query)
        if not match:
            continue
        try:
            rows = await db.execute_fetchall(
                """
                SELECT f.episode_id, t.words_json,
                       e.title AS episode_title, e.published_at, e.podcast_id,
                       s.title AS podcast_title, s.image_url AS podcast_image
                FROM transcripts_fts f
                JOIN transcripts   t ON t.episode_id = f.episode_id
                JOIN episodes      e ON e.id = f.episode_id
                JOIN subscriptions s ON s.podcast_id = e.podcast_id
                WHERE transcripts_fts MATCH ?
                ORDER BY bm25(transcripts_fts)
                LIMIT ?
                """,
                (match, EPISODES_PER_QUERY),
            )
        except Exception:
            # A malformed MATCH is a bad query, not a server fault; the other
            # queries still stand.
            continue

        for row in rows:
            count, snippets = find_matches(
                row["words_json"], query,
                max_snippets=SNIPPETS_PER_EPISODE, radius=PASSAGE_RADIUS,
            )
            if not count:
                continue
            for snippet in snippets:
                bucket = int(snippet["start"] // SAME_MOMENT_SECONDS)
                key = (row["episode_id"], bucket)
                existing = found.get(key)
                if existing:
                    existing["queries"].add(query)
                    existing["hits"] += count
                    continue
                found[key] = {
                    "episode_id": row["episode_id"],
                    "episode_title": row["episode_title"],
                    "podcast_id": row["podcast_id"],
                    "podcast_title": row["podcast_title"],
                    "podcast_image": row["podcast_image"],
                    "published_at": row["published_at"],
                    "start": round(float(snippet["start"]), 2),
                    "text": snippet["text"],
                    "queries": {query},
                    "hits": count,
                }

    passages = sorted(
        found.values(),
        key=lambda p: (len(p["queries"]), p["hits"]),
        reverse=True,
    )

    # Trim to what a single call can carry, and drop the set that was only
    # needed for ranking.
    kept: list[dict] = []
    total = 0
    for passage in passages[:MAX_PASSAGES]:
        length = len(passage["text"])
        if kept and total + length > MAX_CONTEXT_CHARS:
            break
        total += length
        passage.pop("queries", None)
        passage.pop("hits", None)
        kept.append(passage)
    return kept


def _fmt_time(seconds: float) -> str:
    total = int(seconds)
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_prompt(question: str, passages: list[dict], history: list[dict] | None = None) -> str:
    lines = []
    for i, p in enumerate(passages, start=1):
        published = (p.get("published_at") or "")[:10]
        lines.append(
            f"[{i}] {p['podcast_title']} — {p['episode_title']}"
            f"{f' ({published})' if published else ''} at {_fmt_time(p['start'])}\n"
            f"{p['text']}"
        )
    context = "\n\n".join(lines)

    recent = ""
    for message in (history or [])[-4:]:
        recent += f"{message['role']}: {message['content'][:600]}\n"

    return (
        "You are answering a question about podcasts someone has listened to, "
        "using passages retrieved from their own transcripts.\n\n"
        + (f"Recent conversation:\n{recent}\n" if recent else "")
        + f"Question: {question}\n\n"
        f"Passages:\n{context}\n\n"
        "Answer from these passages only. Rules:\n"
        "- Cite the passages you use inline as [1], [2] — every claim that comes "
        "from a passage needs one.\n"
        "- List the numbers you cited under \"cited\".\n"
        "- Quote sparingly and verbatim when you do. Never invent a quote, a "
        "name, a number or an episode.\n"
        "- If the passages do not answer the question, say so plainly and say "
        "what they do cover instead. That is a useful answer, not a failure.\n"
        "- Where speakers disagree, say so rather than averaging them.\n"
        "- Two or three short paragraphs at most. No preamble, no summary of "
        "the question."
    )


async def ask(db, question: str, history: list[dict] | None = None) -> dict:
    """Answer a question from the library. Returns the answer and its sources."""
    question = (question or "").strip()
    if not question:
        return {"answer": "", "passages": [], "cited": []}

    queries = await plan_queries(question, history)
    passages = await gather(db, queries)

    if not passages:
        # No point spending a model call to say nothing was found.
        return {"answer": NOTHING_FOUND, "passages": [], "cited": [], "queries": queries}

    data = await llm.arun_json(
        build_prompt(question, passages, history),
        schema=ANSWER_SCHEMA, timeout=240, default=None,
    )
    if not data:
        raise llm.LLMError("The model did not answer")

    cited = [n for n in data.get("cited", []) if isinstance(n, int) and 1 <= n <= len(passages)]
    # Only the passages actually used are worth showing: a list of twenty
    # sources under a two-paragraph answer is noise, and implies a thoroughness
    # the answer does not have.
    used = [dict(passages[n - 1], index=n) for n in dict.fromkeys(cited)]
    return {
        "answer": data.get("answer", "").strip(),
        "passages": used or [dict(p, index=i) for i, p in enumerate(passages[:3], start=1)],
        "cited": cited,
        "queries": queries,
    }

"""
Full-text search across transcripts, with a timestamp for every hit.

Two stages, because neither alone is enough:

  1. SQLite FTS5 finds *which* episodes contain the query. That is what an index
     is for — scanning every stored transcript in Python would not scale past a
     handful of episodes.
  2. Python then walks that episode's word list to find *where*. FTS5 can return
     a snippet but not a position in the audio, and a transcript search that
     cannot tell you when something was said is barely worth having.
"""
import json
import re
import unicodedata

# Words of context on either side of a hit in the returned snippet.
SNIPPET_RADIUS = 12
# Hits closer together than this are one moment in the conversation, not two.
CLUSTER_GAP_WORDS = 15


def normalise(text: str) -> str:
    """Fold to the same shape FTS5's `unicode61 remove_diacritics 2` matches on."""
    text = unicodedata.normalize("NFD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", text)


def query_terms(q: str) -> list[str]:
    return [t for t in (normalise(tok) for tok in re.split(r"\s+", q.strip())) if t]


def fts_query(q: str) -> str:
    """Build a safe FTS5 MATCH expression.

    Each token is quoted, so punctuation a user types — a hyphen, an apostrophe,
    a stray parenthesis — is matched literally instead of being parsed as FTS5
    operator syntax and raising.
    """
    tokens = [t.replace('"', '""') for t in re.split(r"\s+", q.strip()) if t]
    return " ".join(f'"{t}"' for t in tokens)


def find_matches(words_json: str, q: str, max_snippets: int = 3) -> tuple[int, list[dict]]:
    """Locate `q` inside one transcript.

    Returns (total hits, up to `max_snippets` snippets), each snippet carrying
    the timestamp of its first matching word so the player can seek there.
    """
    terms = query_terms(q)
    if not terms:
        return 0, []
    try:
        words = json.loads(words_json)
    except (TypeError, ValueError):
        return 0, []

    normalised = [normalise(w.get("word", "")) for w in words]
    term_set = set(terms)

    hits = [i for i, w in enumerate(normalised) if w and w in term_set]
    if not hits:
        # Fall back to substring hits so "engineer" still finds "engineering".
        hits = [
            i for i, w in enumerate(normalised)
            if w and any(t in w for t in term_set)
        ]
    if not hits:
        return 0, []

    # Group nearby hits: one dense passage is a single result, not twelve.
    clusters: list[list[int]] = [[hits[0]]]
    for i in hits[1:]:
        if i - clusters[-1][-1] <= CLUSTER_GAP_WORDS:
            clusters[-1].append(i)
        else:
            clusters.append([i])

    # Rank by how many distinct query terms a cluster covers, then by density —
    # a passage mentioning every term beats one repeating a single common word.
    def score(cluster: list[int]) -> tuple[int, int]:
        covered = {normalised[i] for i in cluster}
        distinct = sum(1 for t in term_set if any(t in c for c in covered))
        return (distinct, len(cluster))

    clusters.sort(key=score, reverse=True)

    snippets = []
    for cluster in clusters[:max_snippets]:
        lo = max(0, cluster[0] - SNIPPET_RADIUS)
        hi = min(len(words), cluster[-1] + SNIPPET_RADIUS + 1)
        text = "".join(w.get("word", "") for w in words[lo:hi]).strip()
        if lo > 0:
            text = "… " + text
        if hi < len(words):
            text = text + " …"
        snippets.append({
            "start": float(words[cluster[0]].get("start", 0.0)),
            "text": text,
        })

    # Present in playback order; ranking decided which made the cut, not how
    # they are read.
    snippets.sort(key=lambda s: s["start"])
    return len(hits), snippets

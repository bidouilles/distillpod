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


# Prefix matching on a token this short matches most of the library, so a short
# token only survives when it is the whole query.
MIN_PREFIX_TOKEN = 3


def fts_any_query(q: str) -> str:
    """A MATCH expression that finds documents with ANY of these words.

    `fts_query` ANDs its tokens, which is right when someone types words into a
    search box: two words mean both. It is wrong for retrieval, where the words
    come from a model guessing at what a speaker might have said. "model evals"
    found nothing in an episode that says "models" and "evals" — the singular
    was absent and the AND failed on it.

    So this ORs the tokens and matches them as prefixes, letting bm25 rank the
    documents that cover more of them. Recall matters more than precision here,
    because what comes back is then ranked again by how many separate searches
    agree on the same passage.
    """
    tokens = [t.replace('"', '""') for t in re.split(r"\s+", q.strip()) if t]
    long_enough = [t for t in tokens if len(t) >= MIN_PREFIX_TOKEN]
    chosen = long_enough or tokens
    return " OR ".join(f'"{t}"*' for t in chosen)


def find_matches(words_json: str, q: str, max_snippets: int = 3,
                 radius: int = SNIPPET_RADIUS) -> tuple[int, list[dict]]:
    """Locate `q` inside one transcript.

    Returns (total hits, up to `max_snippets` snippets), each snippet carrying
    the timestamp of its first matching word so the player can seek there.

    `radius` is how many words of context surround a hit. The search screen
    wants a line someone can scan; a model answering a question needs enough
    either side to see what was actually being said, so it asks for more.
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
        lo = max(0, cluster[0] - radius)
        hi = min(len(words), cluster[-1] + radius + 1)
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

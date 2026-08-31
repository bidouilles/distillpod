"""Meaning-based search over transcripts.

Keyword search answers "where was this word said". The question people actually
ask a library is "where did someone talk about X", and those are different: a
passage about exhaustion, deadlines and resenting the work never says
"burnout". Prefix matching does not bridge that; a vector does.

This exists to feed `services/librarian.py`, which fuses these hits with the
keyword ones. It is entirely optional — with no embedding backend the index
stays empty and Ask falls back to keyword retrieval, which is how the feature
shipped and still works.

Windows, not episodes. An hour of speech in one vector says nothing useful; a
window of about a minute is a paragraph-sized idea, which is the unit a question
is asked about. They overlap, because the sentence that answers a question has
no reason to respect a window boundary.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

from database import get_db
from services import embeddings

log = logging.getLogger(__name__)

# A minute of speech is roughly a paragraph — long enough to carry an idea,
# short enough that a hit points somewhere worth listening to.
WINDOW_SECONDS = 60.0
# Overlap, so a thought spanning a boundary is whole in at least one window.
STRIDE_SECONDS = 45.0
# Below this, a window is a fragment: an intro sting, or the tail of an episode.
MIN_WINDOW_CHARS = 120

# Indexing walks the whole library, so it is paced and interruptible like the
# caption backfill. Embedding is cheap per call; the point is to stay polite to
# a hosted API and leave the box responsive.
EPISODE_SPACING_SECONDS = 0.4

_state: dict = {
    "running": False,
    "total": 0,
    "indexed": 0,
    "failed": 0,
    "current": None,
    "stopped_early": False,
    "finished_at": None,
    "stop_requested": False,
}


def status() -> dict:
    return {k: v for k, v in _state.items() if k != "stop_requested"}


def request_stop() -> None:
    _state["stop_requested"] = True


def windows(words: list[dict]) -> list[dict]:
    """Cut a word-level transcript into overlapping windows.

    Returns `[{start, end, text}]` in playback order. Timings come from the
    words themselves, so a hit lands on the second something was said rather
    than on an estimate.
    """
    if not words:
        return []
    end_of_episode = float(words[-1].get("end", 0.0))
    out: list[dict] = []
    start = float(words[0].get("start", 0.0))

    # Walked with two pointers rather than filtered per window: a five-hour
    # episode is 30,000 words and 400 windows, and re-scanning the list for each
    # one is twelve million comparisons for no reason.
    first = 0
    while start < end_of_episode:
        stop = start + WINDOW_SECONDS
        while first < len(words) and float(words[first].get("start", 0)) < start:
            first += 1
        last = first
        while last < len(words) and float(words[last].get("start", 0)) < stop:
            last += 1
        picked = words[first:last]
        if picked:
            text = " ".join(w.get("word", "").strip() for w in picked).strip()
            if len(text) >= MIN_WINDOW_CHARS or not out:
                out.append({
                    "start": round(float(picked[0].get("start", start)), 2),
                    "end": round(float(picked[-1].get("end", stop)), 2),
                    "text": text,
                })
        start += STRIDE_SECONDS
    return out


async def opted_in(db) -> bool:
    """Whether this library has agreed to be indexed.

    The automatic backend choice picks Mistral when a key is present, because
    one is usually there for Voxtral. But embedding sends transcript *text* to a
    hosted API, and an upgrade should not start doing that on its own — so the
    first index is always a deliberate press of the button. After that the
    nightly job keeps it level with the transcripts, which is what someone who
    pressed it wanted.

    An explicit `EMBED_BACKEND` is itself the decision, so it needs no press.
    """
    from config import settings
    if settings.embed_backend in ("mistral", "local"):
        return True
    row = await db.execute_fetchone("SELECT 1 FROM embeddings LIMIT 1")
    return bool(row)


async def index_episode(db, episode_id: str, words_json: str) -> int:
    """Embed and store one episode's windows. Returns how many were stored.

    Replaces whatever was there: re-transcribing an episode must not leave the
    old windows behind, pointing at times that have moved.
    """
    if not embeddings.available():
        return 0
    try:
        words = json.loads(words_json)
    except (TypeError, ValueError):
        return 0

    chunks = windows(words)
    if not chunks:
        return 0

    loop = asyncio.get_event_loop()
    vectors = await loop.run_in_executor(
        None, embeddings.embed, [c["text"] for c in chunks]
    )
    if not vectors or len(vectors) != len(chunks):
        # All or nothing: a half-indexed episode looks complete and answers
        # worse than one that was never indexed.
        return 0

    now = datetime.now(timezone.utc).isoformat()
    model = embeddings.model_name()
    await db.execute("DELETE FROM embeddings WHERE episode_id = ?", (episode_id,))
    for chunk, vector in zip(chunks, vectors):
        await db.execute(
            """INSERT INTO embeddings
               (episode_id, start_time, end_time, text, vector, model, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (episode_id, chunk["start"], chunk["end"], chunk["text"],
             embeddings.pack(vector), model, now),
        )
    await db.commit()
    return len(chunks)


async def pending(db) -> list[dict]:
    """Transcribed episodes with no windows stored, newest first."""
    rows = await db.execute_fetchall(
        """SELECT t.episode_id, e.title
             FROM transcripts t
             JOIN episodes e      ON e.id = t.episode_id
             JOIN subscriptions s ON s.podcast_id = e.podcast_id
            WHERE NOT EXISTS (
                    SELECT 1 FROM embeddings em WHERE em.episode_id = t.episode_id)
            ORDER BY e.published_at DESC"""
    )
    return [{"episode_id": r["episode_id"], "title": r["title"]} for r in rows]


async def coverage() -> dict:
    """How much of the library is searchable by meaning."""
    db = await get_db()
    try:
        transcribed = (await db.execute_fetchone(
            """SELECT COUNT(*) AS n FROM transcripts t
                 JOIN episodes e      ON e.id = t.episode_id
                 JOIN subscriptions s ON s.podcast_id = e.podcast_id"""
        ))["n"]
        indexed = (await db.execute_fetchone(
            "SELECT COUNT(DISTINCT episode_id) AS n FROM embeddings"
        ))["n"]
        vectors = (await db.execute_fetchone("SELECT COUNT(*) AS n FROM embeddings"))["n"]
        return {
            "engine": embeddings.engine(),
            "model": embeddings.model_name(),
            "transcribed": transcribed,
            "indexed": indexed,
            "pending": max(0, transcribed - indexed),
            "windows": vectors,
            # Nested rather than merged: the run's own counters use the same
            # words for different things — its `indexed` is "episodes done in
            # this run", not "episodes in the index" — and flattening them let
            # a finished run's tally overwrite the real coverage.
            "job": status(),
        }
    finally:
        await db.close()


async def run(require_opt_in: bool = False) -> dict:
    """Index everything transcribed but not yet indexed.

    `require_opt_in` is for the unattended caller: the nightly job must not
    start uploading transcript text to a hosted API on its own — see
    `opted_in`. The endpoint behind the button passes False, because pressing it
    is the opt-in.
    """
    if _state["running"]:
        return status()
    if not embeddings.available():
        return status()
    if require_opt_in:
        db = await get_db()
        try:
            if not await opted_in(db):
                return status()
        finally:
            await db.close()

    _state.update({"running": True, "total": 0, "indexed": 0, "failed": 0,
                   "current": None, "stopped_early": False, "finished_at": None,
                   "stop_requested": False})
    db = await get_db()
    try:
        todo = await pending(db)
        _state["total"] = len(todo)
        for item in todo:
            if _state["stop_requested"]:
                _state["stopped_early"] = True
                break
            _state["current"] = item["title"]
            row = await db.execute_fetchone(
                "SELECT words_json FROM transcripts WHERE episode_id = ?",
                (item["episode_id"],),
            )
            if not row:
                continue
            try:
                stored = await index_episode(db, item["episode_id"], row["words_json"])
                if stored:
                    _state["indexed"] += 1
                else:
                    _state["failed"] += 1
            except Exception as exc:
                log.warning("indexing %s failed: %s", item["episode_id"], exc)
                _state["failed"] += 1
            await asyncio.sleep(EPISODE_SPACING_SECONDS)
    finally:
        await db.close()
        _state.update({"running": False, "current": None,
                       "finished_at": datetime.now(timezone.utc).isoformat()})
    return status()


async def search(db, query: str, limit: int = 12) -> list[dict]:
    """Windows closest in meaning to `query`.

    One embedding call for the query, then a single pass over the stored
    vectors. That is a linear scan, and deliberately so: at a few thousand
    windows it costs milliseconds, and an approximate index would be a new
    dependency, a new failure mode and a worse answer.
    """
    if not embeddings.available() or not query.strip():
        return []

    loop = asyncio.get_event_loop()
    vectors = await loop.run_in_executor(None, embeddings.embed, [query])
    if not vectors:
        return []
    q = vectors[0]

    rows = await db.execute_fetchall(
        """SELECT em.episode_id, em.start_time, em.end_time, em.text, em.vector,
                  e.title AS episode_title, e.published_at, e.podcast_id,
                  s.title AS podcast_title, s.image_url AS podcast_image
             FROM embeddings em
             JOIN episodes e      ON e.id = em.episode_id
             JOIN subscriptions s ON s.podcast_id = e.podcast_id"""
    )
    if not rows:
        return []

    # A model change leaves vectors of a different width behind. They are
    # dropped before anything is compared: mixing widths is meaningless, and
    # numpy will not even build the matrix.
    width = len(q)
    usable: list[tuple[list[float], object]] = []
    for row in rows:
        vector = embeddings.unpack(row["vector"])
        if len(vector) == width:
            usable.append((vector, row))
    if not usable:
        return []

    scores = embeddings.similarity(q, [vector for vector, _ in usable])
    scored = sorted(
        ((score, row) for score, (_, row) in zip(scores, usable)),
        key=lambda pair: pair[0], reverse=True,
    )

    return [
        {
            "episode_id": r["episode_id"],
            "episode_title": r["episode_title"],
            "podcast_id": r["podcast_id"],
            "podcast_title": r["podcast_title"],
            "podcast_image": r["podcast_image"],
            "published_at": r["published_at"],
            "start": round(float(r["start_time"]), 2),
            "text": r["text"],
            "score": round(float(score), 4),
        }
        for score, r in scored[:max(1, limit)]
    ]

"""Search inside the transcripts of episodes you already have."""
from fastapi import APIRouter

from database import get_db
from services.transcript_search import find_matches, fts_query

router = APIRouter(prefix="/search", tags=["search"])

MAX_RESULTS = 30


@router.get("/transcripts")
async def search_transcripts(q: str = "", limit: int = 20) -> list[dict]:
    """Episodes whose transcript matches `q`, each with timestamped snippets.

    FTS5 picks the episodes and orders them by bm25; the per-episode word walk
    then finds where in the audio each hit is.
    """
    q = q.strip()
    if len(q) < 2:
        # A single character matches most of the library; not worth the scan.
        return []

    match = fts_query(q)
    if not match:
        return []

    limit = max(1, min(limit, MAX_RESULTS))
    db = await get_db()
    try:
        try:
            rows = await db.execute_fetchall(
                """
                SELECT f.episode_id,
                       e.title            AS episode_title,
                       e.podcast_id,
                       e.published_at,
                       e.duration_seconds,
                       s.title            AS podcast_title,
                       s.image_url        AS podcast_image,
                       t.words_json
                FROM transcripts_fts f
                JOIN transcripts   t ON t.episode_id = f.episode_id
                JOIN episodes      e ON e.id = f.episode_id
                JOIN subscriptions s ON s.podcast_id = e.podcast_id
                WHERE transcripts_fts MATCH ?
                ORDER BY bm25(transcripts_fts)
                LIMIT ?
                """,
                (match, limit),
            )
        except Exception:
            # A malformed MATCH expression is a bad query, not a server fault.
            return []

        results = []
        for r in rows:
            count, snippets = find_matches(r["words_json"], q)
            if not count:
                # FTS matched a stem our word walk could not place; skip rather
                # than show a result with no timestamp to jump to.
                continue
            results.append({
                "episode_id": r["episode_id"],
                "episode_title": r["episode_title"],
                "podcast_id": r["podcast_id"],
                "podcast_title": r["podcast_title"],
                "podcast_image": r["podcast_image"],
                "published_at": r["published_at"],
                "duration_seconds": r["duration_seconds"],
                "match_count": count,
                "matches": snippets,
            })
        return results
    finally:
        await db.close()

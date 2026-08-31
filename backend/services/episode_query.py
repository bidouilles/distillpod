"""One place that turns episode filters into SQL.

The home feed and smart playlists pick episodes by the same criteria — a
"quick, unplayed, #tech" rule means exactly what the same chips mean on Home —
and two implementations of that would drift into meaning different things
without anyone being able to see it. So both build their query here.

Counts are scalar subqueries rather than joins. Joining `gists` and
`bookmarks` in the same statement multiplies the rows before the aggregate, so
an episode with 3 distills and 2 bookmarks reports 6 of each.
"""
from typing import Optional

# Filters that map to a condition on the episode row.
STATUS_SQL = {
    "transcribed": "e.transcript_status = 'done'",
    "distilled":   "EXISTS (SELECT 1 FROM gists g2 WHERE g2.episode_id = e.id)",
    "bookmarked":  "EXISTS (SELECT 1 FROM bookmarks b2 WHERE b2.episode_id = e.id)",
    "adfree":      "e.adfree_path IS NOT NULL",
    "downloaded":  "e.downloaded = 1",
}

# `played` is written when an episode is opened, on whichever device opened it,
# so this reads as "not started anywhere". That is the whole point of doing it
# here rather than against this browser's localStorage, which knew nothing
# about the episode you finished on your phone.
UNPLAYED_SQL = (
    "NOT EXISTS (SELECT 1 FROM playback p2 "
    "WHERE p2.episode_id = e.id AND p2.played = 1)"
)

# Unknown durations sort last when asking for short episodes and last again
# when asking for long ones: "I have 20 minutes" should not be answered with an
# episode whose length nobody knows.
SORTS = {
    "newest":   "e.published_at DESC",
    "oldest":   "e.published_at ASC",
    "shortest": "e.duration_seconds IS NULL, e.duration_seconds ASC",
    "longest":  "e.duration_seconds IS NULL, e.duration_seconds DESC",
}

DEFAULT_SORT = "newest"

# `description` and `summary` are deliberately absent. Descriptions are large
# HTML blobs of show notes and sponsor links; at 200 rows they dominate the
# payload, and no list view renders them. An episode page fetches its own.
SELECT_COLUMNS = """
    e.id, e.podcast_id, e.title, e.audio_url, e.duration_seconds,
    e.published_at, e.created_at, e.image_url, e.downloaded, e.transcript_status,
    e.ads_detected,
    s.title     AS podcast_title,
    s.image_url AS podcast_image,
    (SELECT COUNT(*) FROM gists g     WHERE g.episode_id = e.id) AS distill_count,
    (SELECT COUNT(*) FROM bookmarks b WHERE b.episode_id = e.id) AS bookmark_count,
    EXISTS (SELECT 1 FROM queue q WHERE q.episode_id = e.id)     AS queued,
    COALESCE(pb.played, 0)  AS played,
    COALESCE(pb.position, 0) AS position
"""

MAX_LIMIT = 500


def build_where(
    q: str = "",
    tag_id: str = "",
    podcast_id: str = "",
    status: str = "",
    unplayed: bool = False,
    min_minutes: Optional[int] = None,
    max_minutes: Optional[int] = None,
) -> tuple[list[str], list]:
    """The WHERE fragments and their parameters, for combining with AND."""
    where: list[str] = []
    params: list = []

    if q and q.strip():
        # Title only. Descriptions are large HTML blobs (show notes, sponsor
        # links) and matching them buries real title hits in noise.
        like = f"%{q.strip()}%"
        where.append("(e.title LIKE ? OR s.title LIKE ?)")
        params += [like, like]

    if tag_id:
        where.append(
            "EXISTS (SELECT 1 FROM podcast_tags pt "
            "WHERE pt.podcast_id = e.podcast_id AND pt.tag_id = ?)"
        )
        params.append(tag_id)

    if podcast_id:
        where.append("e.podcast_id = ?")
        params.append(podcast_id)

    if status and status in STATUS_SQL:
        where.append(STATUS_SQL[status])

    if unplayed:
        where.append(UNPLAYED_SQL)

    # A duration bound only ever excludes episodes whose length is known: an
    # unknown one is not evidence of being outside the range.
    if min_minutes:
        where.append("e.duration_seconds >= ?")
        params.append(int(min_minutes) * 60)
    if max_minutes:
        where.append("e.duration_seconds IS NOT NULL AND e.duration_seconds <= ?")
        params.append(int(max_minutes) * 60)

    return where, params


def build(
    q: str = "",
    tag_id: str = "",
    podcast_id: str = "",
    status: str = "",
    unplayed: bool = False,
    min_minutes: Optional[int] = None,
    max_minutes: Optional[int] = None,
    sort: str = DEFAULT_SORT,
    limit: int = 50,
) -> tuple[str, tuple]:
    """A complete SELECT over episodes, with the podcast joined in."""
    where, params = build_where(
        q=q, tag_id=tag_id, podcast_id=podcast_id, status=status,
        unplayed=unplayed, min_minutes=min_minutes, max_minutes=max_minutes,
    )
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    order = SORTS.get(sort, SORTS[DEFAULT_SORT])
    params.append(max(1, min(int(limit or 50), MAX_LIMIT)))

    sql = f"""
        SELECT {SELECT_COLUMNS}
        FROM episodes e
        JOIN subscriptions s ON e.podcast_id = s.podcast_id
        LEFT JOIN playback pb ON pb.episode_id = e.id
        {clause}
        ORDER BY {order}
        LIMIT ?
    """
    return sql, tuple(params)

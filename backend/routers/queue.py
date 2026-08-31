"""Up Next, kept on the server.

The queue used to live only in this browser's localStorage, while playback
positions and opened-episode state were already server-side. So a queue built
on the laptop did not exist on the phone — the one device where a queue is the
point. This is the copy the devices agree on; the client keeps its local mirror
so the list still renders instantly and survives being offline.

Order is stored as a sparse integer rather than a linked list: reordering is a
whole-list intent (a drag ends somewhere), and rewriting positions in one
transaction cannot leave a cycle behind the way a move protocol can.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from database import get_db
from models import QueueOrder

router = APIRouter(prefix="/queue", tags=["queue"])

# A queue is a listening plan, not an archive. The cap exists so a runaway
# "add all" cannot turn into 4000 rows the client has to render.
MAX_QUEUE = 200

_SELECT = """
    SELECT q.episode_id, q.position, q.added_at,
           e.title, e.audio_url, e.duration_seconds, e.image_url,
           e.transcript_status, e.downloaded,
           s.podcast_id, s.title AS podcast_title, s.image_url AS podcast_image
      FROM queue q
      JOIN episodes e      ON e.id = q.episode_id
      LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id
     ORDER BY q.position ASC
"""


def _row(r) -> dict:
    return {
        "episode_id": r["episode_id"],
        "title": r["title"],
        "podcast_id": r["podcast_id"],
        "podcast_title": r["podcast_title"],
        "audio_url": r["audio_url"],
        "image_url": r["podcast_image"] or r["image_url"],
        "duration_seconds": r["duration_seconds"],
        "transcript_status": r["transcript_status"],
        "added_at": r["added_at"],
    }


async def _list(db) -> list[dict]:
    return [_row(r) for r in await db.execute_fetchall(_SELECT)]


async def _renumber(db) -> None:
    """Close gaps so positions stay small and predictable."""
    rows = await db.execute_fetchall("SELECT episode_id FROM queue ORDER BY position ASC")
    for i, r in enumerate(rows):
        await db.execute(
            "UPDATE queue SET position = ? WHERE episode_id = ?", (i, r["episode_id"])
        )


@router.get("")
async def get_queue() -> list[dict]:
    """The queue, in order, with enough episode data to play a row directly."""
    db = await get_db()
    try:
        return await _list(db)
    finally:
        await db.close()


@router.post("/{episode_id}")
async def enqueue(
    episode_id: str,
    position: str = Query("end", pattern="^(next|end)$"),
) -> list[dict]:
    """Add an episode, or move it if it is already queued.

    Re-adding is a move rather than a duplicate: a queue with the same episode
    twice is never what was meant, and "play this next" on something already
    further down the list is a perfectly ordinary thing to want.
    """
    db = await get_db()
    try:
        exists = await db.execute_fetchone("SELECT 1 FROM episodes WHERE id = ?", (episode_id,))
        if not exists:
            raise HTTPException(404, "Episode not found")

        count = (await db.execute_fetchone("SELECT COUNT(*) AS n FROM queue"))["n"]
        already = await db.execute_fetchone(
            "SELECT 1 FROM queue WHERE episode_id = ?", (episode_id,)
        )
        if not already and count >= MAX_QUEUE:
            raise HTTPException(409, f"The queue is full ({MAX_QUEUE} episodes)")

        now = datetime.now(timezone.utc).isoformat()
        await db.execute("DELETE FROM queue WHERE episode_id = ?", (episode_id,))
        if position == "next":
            await db.execute("UPDATE queue SET position = position + 1")
            await db.execute(
                "INSERT INTO queue (episode_id, position, added_at) VALUES (?, 0, ?)",
                (episode_id, now),
            )
        else:
            row = await db.execute_fetchone("SELECT MAX(position) AS m FROM queue")
            nxt = (row["m"] + 1) if row and row["m"] is not None else 0
            await db.execute(
                "INSERT INTO queue (episode_id, position, added_at) VALUES (?, ?, ?)",
                (episode_id, nxt, now),
            )
        await _renumber(db)
        await db.commit()
        return await _list(db)
    finally:
        await db.close()


@router.put("")
async def replace_queue(body: QueueOrder) -> list[dict]:
    """Replace the queue wholesale, in the order given.

    Ids that no longer exist are dropped rather than rejected: the client may
    be flushing a mirror written before an episode was unsubscribed, and
    failing the whole write would leave the two copies permanently apart.
    """
    db = await get_db()
    try:
        wanted = list(dict.fromkeys(body.episode_ids))[:MAX_QUEUE]
        known: set[str] = set()
        if wanted:
            placeholders = ",".join("?" * len(wanted))
            rows = await db.execute_fetchall(
                f"SELECT id FROM episodes WHERE id IN ({placeholders})", tuple(wanted)
            )
            known = {r["id"] for r in rows}

        now = datetime.now(timezone.utc).isoformat()
        await db.execute("DELETE FROM queue")
        for i, episode_id in enumerate(e for e in wanted if e in known):
            await db.execute(
                "INSERT INTO queue (episode_id, position, added_at) VALUES (?, ?, ?)",
                (episode_id, i, now),
            )
        await db.commit()
        return await _list(db)
    finally:
        await db.close()


@router.delete("/{episode_id}")
async def dequeue(episode_id: str) -> list[dict]:
    db = await get_db()
    try:
        await db.execute("DELETE FROM queue WHERE episode_id = ?", (episode_id,))
        await _renumber(db)
        await db.commit()
        return await _list(db)
    finally:
        await db.close()


@router.delete("")
async def clear_queue() -> list[dict]:
    db = await get_db()
    try:
        await db.execute("DELETE FROM queue")
        await db.commit()
        return []
    finally:
        await db.close()

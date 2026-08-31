"""Playlists, manual and smart.

Two kinds share one table because to everything that reads them — the card, the
detail page, "add all to queue" — they are the same thing: an ordered set of
episodes. A manual playlist stores its members; a smart one stores a rule and is
resolved by a query every time it is read, so it can never be stale.

The rules are the filter chips that already exist on Home, persisted. That is
deliberate: a "Quick listen" playlist should mean exactly what picking
`unplayed` and `under 25 minutes` means on the feed, and it does, because both
build their query in `services/episode_query.py`.
"""
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from database import get_db
from models import Playlist, PlaylistCreate, PlaylistRules, PlaylistUpdate, QueueOrder
from services import episode_query

router = APIRouter(prefix="/playlists", tags=["playlists"])

MAX_NAME = 60
MAX_PLAYLISTS = 100
MAX_ITEMS = 500


def _rules_of(row) -> PlaylistRules | None:
    if row["kind"] != "smart" or not row["rules_json"]:
        return None
    try:
        return PlaylistRules(**json.loads(row["rules_json"]))
    except (ValueError, TypeError):
        # A rule that no longer parses must not make the playlist unreadable;
        # it degrades to an empty smart playlist the user can edit.
        return PlaylistRules()


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise HTTPException(422, "A playlist needs a name")
    return name[:MAX_NAME]


async def _resolve(db, row, limit: int | None = None) -> list[dict]:
    """The episodes a playlist currently contains."""
    if row["kind"] == "smart":
        rules = _rules_of(row) or PlaylistRules()
        sql, params = episode_query.build(
            tag_id=rules.tag_id or "",
            podcast_id=rules.podcast_id or "",
            status=rules.status or "",
            unplayed=rules.unplayed,
            min_minutes=rules.min_minutes,
            max_minutes=rules.max_minutes,
            sort=rules.sort,
            limit=limit or rules.limit,
        )
        rows = await db.execute_fetchall(sql, params)
        return [dict(r) for r in rows]

    rows = await db.execute_fetchall(
        f"""SELECT {episode_query.SELECT_COLUMNS}, pi.position AS playlist_position
              FROM playlist_items pi
              JOIN episodes e ON e.id = pi.episode_id
              JOIN subscriptions s ON s.podcast_id = e.podcast_id
              LEFT JOIN playback pb ON pb.episode_id = e.id
             WHERE pi.playlist_id = ?
             ORDER BY pi.position ASC
             LIMIT ?""",
        (row["id"], limit or MAX_ITEMS),
    )
    return [dict(r) for r in rows]


@router.get("")
async def list_playlists() -> list[Playlist]:
    """Every playlist with its current size and a few covers for the card.

    A smart playlist's count is resolved here rather than stored, which is the
    point of it: "Quick listen (7)" has to be true now, not when it was made.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM playlists ORDER BY position ASC, created_at ASC"
        )
        out: list[Playlist] = []
        for row in rows:
            episodes = await _resolve(db, row, limit=12)
            if row["kind"] == "smart":
                count = len(episodes)
            else:
                count = (await db.execute_fetchone(
                    "SELECT COUNT(*) AS n FROM playlist_items WHERE playlist_id = ?",
                    (row["id"],),
                ))["n"]
            images: list[str] = []
            for e in episodes:
                art = e.get("podcast_image") or e.get("image_url")
                if art and art not in images:
                    images.append(art)
                if len(images) == 4:
                    break
            out.append(Playlist(
                id=row["id"], name=row["name"], kind=row["kind"],
                rules=_rules_of(row), created_at=row["created_at"],
                episode_count=count, images=images,
            ))
        return out
    finally:
        await db.close()


@router.post("")
async def create_playlist(body: PlaylistCreate) -> Playlist:
    kind = body.kind if body.kind in ("manual", "smart") else "manual"
    name = _clean_name(body.name)
    db = await get_db()
    try:
        count = (await db.execute_fetchone("SELECT COUNT(*) AS n FROM playlists"))["n"]
        if count >= MAX_PLAYLISTS:
            raise HTTPException(409, "Too many playlists")
        playlist_id = str(uuid.uuid4())
        rules = body.rules or (PlaylistRules() if kind == "smart" else None)
        await db.execute(
            """INSERT INTO playlists (id, name, kind, rules_json, position, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (playlist_id, name, kind,
             json.dumps(rules.model_dump()) if rules else None,
             count, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        row = await db.execute_fetchone("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
        return Playlist(
            id=row["id"], name=row["name"], kind=row["kind"], rules=_rules_of(row),
            created_at=row["created_at"], episode_count=0, images=[],
        )
    finally:
        await db.close()


@router.get("/{playlist_id}")
async def get_playlist(playlist_id: str) -> dict:
    """The playlist and its episodes, in one request — the detail page needs both."""
    db = await get_db()
    try:
        row = await db.execute_fetchone("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
        if not row:
            raise HTTPException(404, "Playlist not found")
        episodes = await _resolve(db, row)
        return {
            "playlist": Playlist(
                id=row["id"], name=row["name"], kind=row["kind"], rules=_rules_of(row),
                created_at=row["created_at"], episode_count=len(episodes),
                images=[],
            ).model_dump(mode="json"),
            "episodes": episodes,
        }
    finally:
        await db.close()


@router.patch("/{playlist_id}")
async def update_playlist(playlist_id: str, body: PlaylistUpdate) -> Playlist:
    """Rename a playlist, or change what a smart one selects."""
    db = await get_db()
    try:
        row = await db.execute_fetchone("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
        if not row:
            raise HTTPException(404, "Playlist not found")
        if body.name is not None:
            await db.execute(
                "UPDATE playlists SET name = ? WHERE id = ?",
                (_clean_name(body.name), playlist_id),
            )
        if body.rules is not None:
            if row["kind"] != "smart":
                raise HTTPException(409, "Only a smart playlist has rules")
            await db.execute(
                "UPDATE playlists SET rules_json = ? WHERE id = ?",
                (json.dumps(body.rules.model_dump()), playlist_id),
            )
        await db.commit()
        row = await db.execute_fetchone("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
        episodes = await _resolve(db, row, limit=12)
        return Playlist(
            id=row["id"], name=row["name"], kind=row["kind"], rules=_rules_of(row),
            created_at=row["created_at"], episode_count=len(episodes), images=[],
        )
    finally:
        await db.close()


@router.delete("/{playlist_id}")
async def delete_playlist(playlist_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM playlist_items WHERE playlist_id = ?", (playlist_id,))
        await db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
        await db.commit()
    finally:
        await db.close()
    return {"status": "deleted"}


@router.post("/{playlist_id}/episodes/{episode_id}")
async def add_episode(playlist_id: str, episode_id: str) -> dict:
    """Add an episode to a manual playlist. Adding twice is a no-op, not a duplicate."""
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT kind FROM playlists WHERE id = ?", (playlist_id,)
        )
        if not row:
            raise HTTPException(404, "Playlist not found")
        if row["kind"] != "manual":
            raise HTTPException(409, "A smart playlist chooses its own episodes")
        if not await db.execute_fetchone("SELECT 1 FROM episodes WHERE id = ?", (episode_id,)):
            raise HTTPException(404, "Episode not found")

        count = (await db.execute_fetchone(
            "SELECT COUNT(*) AS n FROM playlist_items WHERE playlist_id = ?", (playlist_id,)
        ))["n"]
        if count >= MAX_ITEMS:
            raise HTTPException(409, f"This playlist is full ({MAX_ITEMS} episodes)")

        row2 = await db.execute_fetchone(
            "SELECT MAX(position) AS m FROM playlist_items WHERE playlist_id = ?",
            (playlist_id,),
        )
        nxt = (row2["m"] + 1) if row2 and row2["m"] is not None else 0
        await db.execute(
            """INSERT OR IGNORE INTO playlist_items
               (playlist_id, episode_id, position, added_at) VALUES (?, ?, ?, ?)""",
            (playlist_id, episode_id, nxt, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return {"status": "added", "playlist_id": playlist_id, "episode_id": episode_id}
    finally:
        await db.close()


@router.delete("/{playlist_id}/episodes/{episode_id}")
async def remove_episode(playlist_id: str, episode_id: str):
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM playlist_items WHERE playlist_id = ? AND episode_id = ?",
            (playlist_id, episode_id),
        )
        await db.commit()
    finally:
        await db.close()
    return {"status": "removed"}


@router.put("/{playlist_id}/episodes")
async def reorder_episodes(playlist_id: str, body: QueueOrder) -> dict:
    """Replace a manual playlist's order wholesale, same as the queue."""
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            "SELECT kind FROM playlists WHERE id = ?", (playlist_id,)
        )
        if not row:
            raise HTTPException(404, "Playlist not found")
        if row["kind"] != "manual":
            raise HTTPException(409, "A smart playlist has no order to set")

        current = {
            r["episode_id"] for r in await db.execute_fetchall(
                "SELECT episode_id FROM playlist_items WHERE playlist_id = ?", (playlist_id,)
            )
        }
        now = datetime.now(timezone.utc).isoformat()
        wanted = [e for e in dict.fromkeys(body.episode_ids) if e in current]
        for i, episode_id in enumerate(wanted):
            await db.execute(
                "UPDATE playlist_items SET position = ? WHERE playlist_id = ? AND episode_id = ?",
                (i, playlist_id, episode_id),
            )
        # Anything the client did not mention keeps its relative place after
        # the ones it did, rather than being dropped.
        for j, episode_id in enumerate(sorted(current - set(wanted))):
            await db.execute(
                "UPDATE playlist_items SET position = ? WHERE playlist_id = ? AND episode_id = ?",
                (len(wanted) + j, playlist_id, episode_id),
            )
        await db.commit()
        return {"status": "reordered", "count": len(current)}
    finally:
        await db.close()


@router.post("/{playlist_id}/queue")
async def queue_playlist(playlist_id: str, replace: bool = False) -> list[dict]:
    """Send the whole playlist to Up Next — the "play all" this needs to be useful."""
    from routers.queue import MAX_QUEUE, _list, _renumber

    db = await get_db()
    try:
        row = await db.execute_fetchone("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
        if not row:
            raise HTTPException(404, "Playlist not found")
        episodes = await _resolve(db, row)

        now = datetime.now(timezone.utc).isoformat()
        if replace:
            await db.execute("DELETE FROM queue")
            start = 0
        else:
            top = await db.execute_fetchone("SELECT MAX(position) AS m FROM queue")
            start = (top["m"] + 1) if top and top["m"] is not None else 0

        for i, ep in enumerate(episodes):
            if start + i >= MAX_QUEUE:
                break
            await db.execute(
                """INSERT OR IGNORE INTO queue (episode_id, position, added_at)
                   VALUES (?, ?, ?)""",
                (ep["id"], start + i, now),
            )
        await _renumber(db)
        await db.commit()
        return await _list(db)
    finally:
        await db.close()

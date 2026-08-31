"""Tags on subscriptions — create, list, delete, and assign to a podcast."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from database import get_db
from models import Tag

router = APIRouter(prefix="/tags", tags=["tags"])

MAX_TAG_LENGTH = 32


class TagCreate(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def clean(cls, v: str) -> str:
        v = " ".join(v.split())          # collapse whitespace
        if not v:
            raise ValueError("tag name cannot be empty")
        if len(v) > MAX_TAG_LENGTH:
            raise ValueError(f"tag name cannot exceed {MAX_TAG_LENGTH} characters")
        return v


class TagAssignment(BaseModel):
    tag_ids: list[str]


@router.get("")
async def list_tags() -> list[Tag]:
    """All tags, with how many podcasts carry each — drives the filter chips."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT t.id, t.name, COUNT(pt.podcast_id) AS podcast_count
               FROM tags t
               LEFT JOIN podcast_tags pt ON pt.tag_id = t.id
               GROUP BY t.id
               ORDER BY t.name COLLATE NOCASE"""
        )
        return [Tag(**dict(r)) for r in rows]
    finally:
        await db.close()


@router.post("")
async def create_tag(body: TagCreate) -> Tag:
    """Create a tag, or return the existing one with that name.

    Idempotent so the UI can 'create or pick' in one call without racing itself
    when the same name is submitted twice.
    """
    db = await get_db()
    try:
        existing = await db.execute_fetchone(
            "SELECT id, name FROM tags WHERE name = ? COLLATE NOCASE", (body.name,)
        )
        if existing:
            return Tag(id=existing["id"], name=existing["name"])
        tag_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO tags (id, name, created_at) VALUES (?, ?, ?)",
            (tag_id, body.name, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        return Tag(id=tag_id, name=body.name)
    finally:
        await db.close()


@router.delete("/{tag_id}")
async def delete_tag(tag_id: str):
    """Remove a tag and detach it from every podcast."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM podcast_tags WHERE tag_id = ?", (tag_id,))
        await db.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        await db.commit()
    finally:
        await db.close()
    return {"status": "deleted"}


@router.put("/podcast/{podcast_id}")
async def set_podcast_tags(podcast_id: str, body: TagAssignment) -> list[Tag]:
    """Replace a podcast's tags wholesale. Returns the resulting set."""
    db = await get_db()
    try:
        sub = await db.execute_fetchone(
            "SELECT 1 FROM subscriptions WHERE podcast_id = ?", (podcast_id,)
        )
        if not sub:
            raise HTTPException(status_code=404, detail="Not subscribed to this podcast")

        # Ignore ids that do not exist rather than half-applying the assignment.
        valid = set()
        if body.tag_ids:
            placeholders = ",".join("?" * len(body.tag_ids))
            rows = await db.execute_fetchall(
                f"SELECT id FROM tags WHERE id IN ({placeholders})", tuple(body.tag_ids)
            )
            valid = {r["id"] for r in rows}

        await db.execute("DELETE FROM podcast_tags WHERE podcast_id = ?", (podcast_id,))
        for tag_id in valid:
            await db.execute(
                "INSERT INTO podcast_tags (podcast_id, tag_id) VALUES (?, ?)",
                (podcast_id, tag_id),
            )
        await db.commit()

        rows = await db.execute_fetchall(
            """SELECT t.id, t.name FROM tags t
               JOIN podcast_tags pt ON pt.tag_id = t.id
               WHERE pt.podcast_id = ?
               ORDER BY t.name COLLATE NOCASE""",
            (podcast_id,),
        )
        return [Tag(id=r["id"], name=r["name"]) for r in rows]
    finally:
        await db.close()

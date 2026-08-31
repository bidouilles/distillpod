"""Bookmarks — a quote kept from the transcript, with no model call behind it.

The app already had a way to keep a moment: tap ⚗️ and the agent CLI returns a
quote and an insight. That is worth ~30 seconds of waiting for the moment you
really want, and far too expensive for the six you want on a drive. So this is
the cheap half of the same gesture: the transcript is already on disk with
word-level timings, so keeping a quote costs a lookup and an INSERT.

Two ways in, because the caller knows different things:
  * the player knows only where playback is, so the server finds the sentence;
  * a long-press on a transcript line knows exactly which words it means.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from database import get_db
from models import Bookmark, BookmarkNote, BookmarkRequest
from services.bookmark_engine import extract

router = APIRouter(prefix="/bookmarks", tags=["bookmarks"])

MAX_TEXT = 2000
MAX_NOTE = 1000

_SELECT = """
    SELECT b.*, e.title AS episode_title, e.image_url AS episode_image,
           s.title AS podcast_title, s.image_url AS podcast_image
      FROM bookmarks b
      LEFT JOIN episodes e      ON e.id = b.episode_id
      LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id
"""


def _to_model(r) -> Bookmark:
    return Bookmark(
        id=r["id"],
        episode_id=r["episode_id"],
        start_seconds=r["start_seconds"],
        end_seconds=r["end_seconds"],
        text=r["text"],
        note=r["note"],
        created_at=r["created_at"],
        episode_title=r["episode_title"],
        podcast_title=r["podcast_title"],
        podcast_image=r["podcast_image"] or r["episode_image"],
    )


@router.get("")
async def list_bookmarks(episode_id: str = "", limit: int = 500) -> list[Bookmark]:
    """Bookmarks, newest first — or in playback order within one episode.

    Within an episode the useful order is the order they were said, because
    that is how they will be re-read; across the library it is the order they
    were saved.
    """
    db = await get_db()
    try:
        if episode_id:
            rows = await db.execute_fetchall(
                _SELECT + " WHERE b.episode_id = ? ORDER BY b.start_seconds ASC",
                (episode_id,),
            )
        else:
            rows = await db.execute_fetchall(
                _SELECT + " ORDER BY b.created_at DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            )
        return [_to_model(r) for r in rows]
    finally:
        await db.close()


@router.post("")
async def create_bookmark(body: BookmarkRequest) -> Bookmark:
    """Keep a quote. Returns the stored bookmark, quote included."""
    db = await get_db()
    try:
        episode = await db.execute_fetchone(
            "SELECT id, title FROM episodes WHERE id = ?", (body.episode_id,)
        )
        if not episode:
            raise HTTPException(404, "Episode not found")

        text = (body.text or "").strip()
        start = body.start_seconds
        end = body.end_seconds

        if not text:
            # No words supplied, so find them. The moment is whichever of the
            # two the caller gave: `seconds` from the player, `start_seconds`
            # from a transcript line.
            at = body.seconds if body.seconds is not None else start
            if at is None:
                raise HTTPException(422, "Give either text, seconds, or start_seconds")
            from services.transcriber import get_transcript_words
            words = await get_transcript_words(body.episode_id)
            found = extract(words, at)
            if not found:
                raise HTTPException(
                    409,
                    "Nothing transcribed around that moment yet"
                    if words else "This episode has no transcript yet",
                )
            text, start, end = found["text"], found["start_seconds"], found["end_seconds"]

        if start is None:
            start = body.seconds or 0.0
        if end is None:
            end = start

        bookmark_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO bookmarks
               (id, episode_id, start_seconds, end_seconds, text, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (bookmark_id, body.episode_id, max(0.0, float(start)), float(end),
             text[:MAX_TEXT], (body.note or None), now),
        )
        await db.commit()

        row = await db.execute_fetchone(_SELECT + " WHERE b.id = ?", (bookmark_id,))
        return _to_model(row)
    finally:
        await db.close()


@router.patch("/{bookmark_id}")
async def annotate_bookmark(bookmark_id: str, body: BookmarkNote) -> Bookmark:
    """Add or clear the note on a bookmark — why it was worth keeping."""
    db = await get_db()
    try:
        exists = await db.execute_fetchone(
            "SELECT 1 FROM bookmarks WHERE id = ?", (bookmark_id,)
        )
        if not exists:
            raise HTTPException(404, "Bookmark not found")
        note = (body.note or "").strip()[:MAX_NOTE] or None
        await db.execute("UPDATE bookmarks SET note = ? WHERE id = ?", (note, bookmark_id))
        await db.commit()
        row = await db.execute_fetchone(_SELECT + " WHERE b.id = ?", (bookmark_id,))
        return _to_model(row)
    finally:
        await db.close()


@router.delete("/{bookmark_id}")
async def delete_bookmark(bookmark_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM bookmarks WHERE id = ?", (bookmark_id,))
        await db.commit()
    finally:
        await db.close()
    return {"status": "deleted"}

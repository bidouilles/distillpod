import uuid
import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from database import get_db
from services import researcher

router = APIRouter(prefix="/research", tags=["research"])


@router.post("/{gist_id}")
async def trigger_research(gist_id: str):
    """Start researching a distilled moment.

    Refuses up front when there is no search API to research *with*: the agent
    CLI runs sandboxed with no network, so without a key the only possible
    output is a report explaining that it had no sources — which is what this
    used to produce, and announce as ready.
    """
    db = await get_db()
    try:
        # Check if gist exists
        gist = await (
            await db.execute(
                "SELECT id, episode_id, episode_title, text, summary FROM gists WHERE id = ?",
                (gist_id,),
            )
        ).fetchone()
        if not gist:
            raise HTTPException(status_code=404, detail="Gist not found")

        if not researcher.available():
            raise HTTPException(
                status_code=409,
                detail="Research needs a Tavily API key on the server (TAVILY_API_KEY).",
            )

        # Check if research already exists
        existing = await (
            await db.execute(
                "SELECT id, status, public_url FROM researches WHERE gist_id = ?",
                (gist_id,),
            )
        ).fetchone()
        if existing:
            # A failed attempt is worth retrying — the usual cause was a missing
            # key or a search that came back empty, both of which change.
            if existing["status"] != "error":
                return dict(existing)
            await db.execute("DELETE FROM researches WHERE gist_id = ?", (gist_id,))
            await db.commit()

        # Create research record
        research_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO researches (id, gist_id, episode_id, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (research_id, gist_id, gist["episode_id"], "pending", now),
        )
        await db.commit()

        # Runs in the background: two model calls and several web searches.
        asyncio.create_task(researcher.run_research(research_id, gist_id))

        return {"id": research_id, "status": "pending"}
    finally:
        await db.close()


@router.get("/{gist_id}")
async def get_research(gist_id: str):
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT id, status, public_url, error, created_at, finished_at "
                "FROM researches WHERE gist_id = ? ORDER BY created_at DESC LIMIT 1",
                (gist_id,),
            )
        ).fetchone()
        if not row:
            return {"status": "none"}
        return dict(row)
    finally:
        await db.close()

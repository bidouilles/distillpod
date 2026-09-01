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


@router.get("/{gist_id}/markdown")
async def research_markdown(gist_id: str):
    """The finished report as text, for pasting into a note or a message.

    Rendered from the structure the report was built from rather than scraped
    back out of the page, which is why that structure is stored. A report from
    before it was stored has only the page, and says so rather than returning
    something half-recovered.
    """
    import json as _json

    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT status, report_json, public_url FROM researches "
                "WHERE gist_id = ? ORDER BY created_at DESC LIMIT 1",
                (gist_id,),
            )
        ).fetchone()
    finally:
        await db.close()

    if not row or row["status"] != "done":
        raise HTTPException(404, "No finished report for this distillation")
    if not row["report_json"]:
        raise HTTPException(
            409,
            "This report predates text export — run it again to get a copyable version.",
        )
    try:
        data = _json.loads(row["report_json"])
    except ValueError:
        raise HTTPException(500, "The stored report could not be read")

    markdown = researcher.build_markdown(
        claim=data.get("claim", ""), report=data.get("report", {}),
        sources=data.get("sources", []), echoes=data.get("echoes", []),
        episode=data.get("episode", {}), gist={"text": data.get("quote", "")},
        queries=data.get("queries", []),
    )
    return {"gist_id": gist_id, "markdown": markdown, "url": row["public_url"]}


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

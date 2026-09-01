import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException

from fastapi.responses import FileResponse

from config import settings
from database import get_db
from services import researcher, typeset

log = logging.getLogger(__name__)

router = APIRouter(prefix="/research", tags=["research"])


@router.get("")
async def list_reports() -> list[dict]:
    """Every report, newest first.

    Until now a report could only be reached through the distillation that
    produced it — three screens deep, and unreachable at all if you had
    forgotten which moment it came from. It is the most substantial thing this
    app makes and it was the hardest thing to find.
    """
    import json as _json

    db = await get_db()
    try:
        rows = await (
            await db.execute(
                """SELECT r.id, r.gist_id, r.episode_id, r.status, r.public_url,
                          r.error, r.created_at, r.finished_at, r.report_json,
                          e.title AS episode_title, s.title AS podcast_title
                     FROM researches r
                     LEFT JOIN episodes e      ON e.id = r.episode_id
                     LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id
                    ORDER BY r.created_at DESC"""
            )
        ).fetchall()
    finally:
        await db.close()

    can_typeset = typeset.available()
    out = []
    for row in rows:
        record = dict(row)
        stored = record.pop("report_json", None)
        claim, verdict, sources = "", "", 0
        if stored:
            try:
                data = _json.loads(stored)
                claim = data.get("claim") or ""
                verdict = (data.get("report") or {}).get("verdict") or ""
                sources = len(data.get("sources") or [])
            except ValueError:
                pass
        out.append({
            **record,
            "claim": claim,
            "verdict": verdict,
            "sources": sources,
            "markdown": bool(stored),
            "pdf": bool(stored) and can_typeset,
        })
    return out


@router.delete("/{gist_id}")
async def delete_report(gist_id: str):
    """Remove a report, and the files it wrote.

    Deleting the row alone would leave the HTML and the PDF behind for good:
    nothing else knows they exist, and the media retention pass only looks at
    audio.
    """
    db = await get_db()
    try:
        rows = await (
            await db.execute(
                "SELECT id, file_path FROM researches WHERE gist_id = ?", (gist_id,)
            )
        ).fetchall()
        if not rows:
            raise HTTPException(404, "No report for this distillation")
        for row in rows:
            for path in (row["file_path"], str(Path(settings.reports_dir) / f"{row['id']}.pdf")):
                if not path:
                    continue
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("could not remove %s: %s", path, exc)
        await db.execute("DELETE FROM researches WHERE gist_id = ?", (gist_id,))
        await db.commit()
    finally:
        await db.close()
    return {"status": "deleted"}


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


@router.get("/{gist_id}/pdf")
async def research_pdf(gist_id: str):
    """The report typeset as a briefing note.

    Compiled once and kept beside the HTML: the same report asked for twice
    should not run the typesetter twice. Missing binary, or a report stored
    before the structure was kept, is a refusal with a reason rather than a
    blank page.
    """
    import json as _json

    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT id, status, report_json FROM researches "
                "WHERE gist_id = ? ORDER BY created_at DESC LIMIT 1",
                (gist_id,),
            )
        ).fetchone()
    finally:
        await db.close()

    if not row or row["status"] != "done":
        raise HTTPException(404, "No finished report for this distillation")
    if not typeset.available():
        raise HTTPException(409, "Typesetting needs the typst binary on the server.")
    if not row["report_json"]:
        raise HTTPException(
            409, "This report predates PDF export — run it again to typeset it.")

    path = Path(settings.reports_dir) / f"{row['id']}.pdf"
    if not path.exists():
        try:
            data = _json.loads(row["report_json"])
        except ValueError:
            raise HTTPException(500, "The stored report could not be read")
        # Typesetting is CPU work; it queues with the other media work rather
        # than competing with an encode or a download.
        from services import jobs
        async with jobs.lane("media", label=f"typeset: {row['id'][:8]}"):
            rendered = await asyncio.to_thread(typeset.render, data, path)
        if not rendered:
            raise HTTPException(502, "The report could not be typeset")

    stem = "".join(c if c.isalnum() or c in "-_" else "-"
                   for c in (gist_id[:8] + "-research"))
    return FileResponse(str(path), media_type="application/pdf",
                        filename=f"{stem}.pdf")


@router.get("/{gist_id}")
async def get_research(gist_id: str):
    db = await get_db()
    try:
        row = await (
            await db.execute(
                "SELECT id, status, public_url, error, created_at, finished_at, "
                "       report_json "
                "FROM researches WHERE gist_id = ? ORDER BY created_at DESC LIMIT 1",
                (gist_id,),
            )
        ).fetchone()
        if not row:
            return {"status": "none"}
        record = dict(row)
        # Whether the extra renderings can be offered at all, so the card does
        # not show a button that can only fail.
        record["markdown"] = bool(record.pop("report_json", None))
        record["pdf"] = record["markdown"] and typeset.available()
        return record
    finally:
        await db.close()

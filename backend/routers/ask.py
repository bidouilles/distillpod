"""Asking the whole library a question.

Episode chat answers about the episode in front of you; this answers across
everything transcribed, with a citation on every claim so it can be checked
against the audio. The retrieval and the prompting live in
`services/librarian.py` — this is the conversation around them.

One conversation rather than one per episode, because crossing episodes is the
entire point of it.
"""
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from database import get_db
from services import librarian, llm

router = APIRouter(prefix="/ask", tags=["ask"])

MAX_QUESTION = 1000
# Older exchanges are dropped rather than kept forever: only the last few are
# used as context, and an unbounded table would be read in full on every open.
MAX_HISTORY = 60


class Question(BaseModel):
    message: str

    @field_validator("message")
    @classmethod
    def clean(cls, v: str) -> str:
        v = " ".join((v or "").split())
        if not v:
            raise ValueError("ask something")
        return v[:MAX_QUESTION]


def _row(r) -> dict:
    citations = []
    if r["citations_json"]:
        try:
            citations = json.loads(r["citations_json"])
        except ValueError:
            citations = []
    return {
        "id": r["id"],
        "role": r["role"],
        "content": r["content"],
        "citations": citations,
        "created_at": r["created_at"],
    }


async def _history(db) -> list[dict]:
    rows = await db.execute_fetchall(
        "SELECT id, role, content, citations_json, created_at FROM library_chats "
        "ORDER BY created_at ASC, id ASC"
    )
    return [_row(r) for r in rows]


@router.get("")
async def get_history() -> list[dict]:
    """The conversation so far, oldest first."""
    db = await get_db()
    try:
        return await _history(db)
    finally:
        await db.close()


@router.post("")
async def ask_question(body: Question) -> dict:
    """Ask, and get the answer with the passages it was built from.

    Synchronous, like episode chat: two model calls, so tens of seconds. The
    answer is stored before it is returned, so a client that gives up on the
    request still finds it when the screen is reopened.
    """
    db = await get_db()
    try:
        history = await _history(db)
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO library_chats (id, role, content, citations_json, created_at)
               VALUES (?, 'user', ?, NULL, ?)""",
            (str(uuid.uuid4()), body.message, now),
        )
        await db.commit()

        try:
            result = await librarian.ask(db, body.message, history)
        except llm.LLMError as exc:
            raise HTTPException(502, f"Could not answer that: {exc}")

        answer_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO library_chats (id, role, content, citations_json, created_at)
               VALUES (?, 'assistant', ?, ?, ?)""",
            (answer_id, result["answer"], json.dumps(result["passages"]),
             datetime.now(timezone.utc).isoformat()),
        )

        # Keep the table to a readable length, oldest first.
        await db.execute(
            """DELETE FROM library_chats WHERE id IN (
                 SELECT id FROM library_chats
                 ORDER BY created_at DESC, id DESC LIMIT -1 OFFSET ?
               )""",
            (MAX_HISTORY,),
        )
        await db.commit()

        return {
            "id": answer_id,
            "role": "assistant",
            "content": result["answer"],
            "citations": result["passages"],
            "created_at": now,
            # What it searched for. Worth showing: when an answer is thin, the
            # searches say whether the question or the library was the problem.
            "queries": result.get("queries", []),
        }
    finally:
        await db.close()


@router.delete("")
async def clear_history():
    """Start again."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM library_chats")
        await db.commit()
    finally:
        await db.close()
    return {"status": "cleared"}

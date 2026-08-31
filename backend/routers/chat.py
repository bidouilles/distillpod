import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import shownotes
from database import get_db

router = APIRouter(prefix="/chat", tags=["chat"])

from services import llm


async def llm_call(prompt: str) -> str:
    """Free-text agent call, surfaced to the client as a 500 on failure."""
    try:
        return await llm.arun(prompt, timeout=120)
    except llm.LLMError as e:
        raise HTTPException(status_code=500, detail=str(e))


async def get_transcript(episode_id: str) -> str:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT words_json FROM transcripts WHERE episode_id = ?", (episode_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Transcript not found")
        words = json.loads(row["words_json"])
        return " ".join(w["word"] for w in words)
    finally:
        await db.close()


async def get_episode_context(episode_id: str) -> tuple[str, str]:
    """Title and flattened show notes.

    The notes carry guest names, handles, and links that are never spoken, so
    the transcript alone cannot answer "what was his blog?".
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT title, description FROM episodes WHERE id = ?", (episode_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return "this episode", ""
        return row["title"], shownotes.to_text(row["description"])
    finally:
        await db.close()


def _notes_block(notes: str) -> str:
    """Label the notes so the model does not confuse them with what was said."""
    if not notes:
        return ""
    return (
        "Show notes for this episode (written by the publisher, not spoken in "
        f"the audio — use them for names, links and references):\n{notes}\n\n"
    )


@router.get("/{episode_id}")
async def get_chat(episode_id: str):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, role, content, created_at FROM episode_chats "
            "WHERE episode_id = ? ORDER BY created_at ASC",
            (episode_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.post("/{episode_id}/init")
async def init_chat(episode_id: str):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, role, content, created_at FROM episode_chats "
            "WHERE episode_id = ? ORDER BY created_at ASC LIMIT 1",
            (episode_id,),
        )
        existing = await cursor.fetchone()
        if existing:
            return dict(existing)

        transcript = await get_transcript(episode_id)
        title, notes = await get_episode_context(episode_id)

        prompt = (
            f'You are a helpful podcast assistant for the episode "{title}". '
            f'Summarize the 3-4 key insights from this episode as bullet points, '
            f'then invite the user to ask questions. Be concise.\n\n'
            f'{_notes_block(notes)}'
            f'Full transcript:\n{transcript}'
        )
        content = await llm_call(prompt)

        now = datetime.now(timezone.utc).isoformat()
        msg_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO episode_chats (id, episode_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (msg_id, episode_id, "assistant", content, now),
        )
        await db.commit()
        return {"id": msg_id, "role": "assistant", "content": content, "created_at": now}
    finally:
        await db.close()


class MessageBody(BaseModel):
    message: str


@router.post("/{episode_id}/message")
async def send_message(episode_id: str, body: MessageBody):
    db = await get_db()
    try:
        transcript = await get_transcript(episode_id)
        title, notes = await get_episode_context(episode_id)
        cursor = await db.execute(
            "SELECT role, content FROM episode_chats "
            "WHERE episode_id = ? ORDER BY created_at ASC",
            (episode_id,),
        )
        history = await cursor.fetchall()
        turns = [{"role": r["role"], "content": r["content"]} for r in history][-10:]

        history_text = ""
        for t in turns:
            label = "User" if t["role"] == "user" else "Assistant"
            history_text += f"{label}: {t['content']}\n\n"

        prompt = (
            f'You are a helpful podcast assistant for the episode "{title}". '
            f"Answer using the show notes and transcript below. Be concise and "
            f"conversational. If something is covered in neither, say so.\n\n"
            f"{_notes_block(notes)}"
            f"Full transcript:\n{transcript}\n\n"
            f"Conversation so far:\n{history_text}"
            f"User: {body.message}\n\nAssistant:"
        )
        reply_content = await llm_call(prompt)

        now = datetime.now(timezone.utc).isoformat()
        user_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO episode_chats (id, episode_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, episode_id, "user", body.message, now),
        )
        asst_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO episode_chats (id, episode_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (asst_id, episode_id, "assistant", reply_content, now),
        )
        # Trim to 50 messages per episode (keep most recent)
        await db.execute(
            """DELETE FROM episode_chats WHERE episode_id = ? AND id NOT IN (
               SELECT id FROM episode_chats WHERE episode_id = ?
               ORDER BY created_at DESC LIMIT 50)""",
            (episode_id, episode_id),
        )
        await db.commit()
        return {"id": asst_id, "role": "assistant", "content": reply_content, "created_at": now}
    finally:
        await db.close()

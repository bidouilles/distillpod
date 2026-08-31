"""What the media directory is costing, and how to get it back.

The one piece of housekeeping a self-hosted podcast app cannot do without.
Every episode played leaves an MP3 behind and every one with ads leaves a
second, re-encoded copy beside it, so on a small VPS the disk filling up was
only ever a question of how long the app had been useful.

The policy is off by default — retention has to be asked for — and what it
throws away is chosen so that clearing an episode costs nothing you cannot get
back: the audio can be downloaded again, the transcript, chapters, distills and
bookmarks stay.
"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from database import get_db
from services import retention

router = APIRouter(prefix="/storage", tags=["storage"])


class RetentionPolicy(BaseModel):
    """`days = 0` disables retention entirely, which is the default."""
    days: int | None = Field(default=None, ge=0, le=3650)
    played_only: bool | None = None


@router.get("")
async def get_storage():
    """Disk used, broken down by show, plus the current policy."""
    return await retention.usage()


@router.put("/policy")
async def set_policy(body: RetentionPolicy):
    """Change how long audio is kept. Applied by the nightly job and by /prune."""
    db = await get_db()
    try:
        return await retention.set_state(db, days=body.days, played_only=body.played_only)
    finally:
        await db.close()


@router.post("/prune")
async def prune(dry_run: bool = False, days: int | None = None):
    """Free up space now.

    `dry_run` reports exactly what would go without touching anything, which is
    what the button in the UI asks first — deleting media unprompted is not the
    kind of surprise this app should hold.
    """
    return await retention.prune(days=days, dry_run=dry_run)

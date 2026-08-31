"""Keeping the media directory from eating the server.

Every episode played leaves an MP3 behind, and every one with ads leaves a
second, re-encoded copy next to it. Nothing ever removed either, so on a small
VPS the disk filling up was only a matter of how long the app had been useful.

What is thrown away and what is kept is the whole design: the audio can always
be downloaded again, and is 1000x the size of the transcript, which cannot. So
audio goes and the transcript, chapters, distills and bookmarks stay — an
episode you cleared is still searchable, still quotable, still exportable, and
plays again on demand.

Three things are never cleared, however old:
  * anything in the queue — you put it there to listen to it;
  * anything part-heard — a position with no finish is a bookmark of intent;
  * anything not played at all, unless asked for explicitly.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import settings
from database import get_db

log = logging.getLogger(__name__)

# Deliberately conservative. This runs unattended from the nightly job, and the
# failure that matters is deleting audio someone still wanted.
DEFAULT_DAYS = 30

STATE_DAYS = "retention_days"
STATE_PLAYED_ONLY = "retention_played_only"

# A file being written right now is not a stray. `downloader.py` writes
# `<name>.part` and renames on success, so sweeping those would break downloads
# in progress — and this runs from the nightly job while the app is still
# serving, so "in progress" is not hypothetical. The age window covers the other
# half of that race: a rename that has just happened but whose row has not been
# written yet.
IN_FLIGHT_SECONDS = 3600


def _in_flight(f: Path) -> bool:
    if f.suffix == ".part":
        return True
    try:
        return (time.time() - f.stat().st_mtime) < IN_FLIGHT_SECONDS
    except OSError:
        return True          # cannot tell how old it is, so do not touch it


async def get_state(db) -> dict:
    """The retention policy, as stored. `days = 0` means keep everything."""
    rows = await db.execute_fetchall(
        "SELECT key, value FROM app_state WHERE key IN (?, ?)",
        (STATE_DAYS, STATE_PLAYED_ONLY),
    )
    stored = {r["key"]: r["value"] for r in rows}
    try:
        days = int(stored.get(STATE_DAYS, 0))
    except (TypeError, ValueError):
        days = 0
    played_only = stored.get(STATE_PLAYED_ONLY, "1") != "0"
    return {"days": max(0, days), "played_only": played_only}


async def set_state(db, days: int | None = None, played_only: bool | None = None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    for key, value in ((STATE_DAYS, days), (STATE_PLAYED_ONLY, played_only)):
        if value is None:
            continue
        stored = str(int(value)) if isinstance(value, bool) else str(int(value))
        await db.execute(
            """INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              updated_at = excluded.updated_at""",
            (key, stored, now),
        )
    await db.commit()
    return await get_state(db)


def _size(path: str | None) -> int:
    if not path:
        return 0
    try:
        p = Path(path)
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0


async def usage() -> dict:
    """What is on disk, and which shows account for it.

    Read from the filesystem rather than from a stored size, because the
    interesting case is precisely the one where the two disagree: a file
    deleted by hand, or a download that half happened.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT e.id, e.podcast_id, e.local_path, e.adfree_path,
                      s.title AS podcast_title
               FROM episodes e
               LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id
               WHERE e.local_path IS NOT NULL OR e.adfree_path IS NOT NULL"""
        )
        state = await get_state(db)
    finally:
        await db.close()

    by_podcast: dict[str, dict] = {}
    tracked_files: set[str] = set()
    total = episodes = 0

    for r in rows:
        size = _size(r["local_path"]) + _size(r["adfree_path"])
        for p in (r["local_path"], r["adfree_path"]):
            if p:
                try:
                    tracked_files.add(str(Path(p).resolve()))
                except OSError:
                    pass
        if size <= 0:
            continue
        total += size
        episodes += 1
        entry = by_podcast.setdefault(
            r["podcast_id"],
            {"podcast_id": r["podcast_id"],
             "title": r["podcast_title"] or "Unknown",
             "bytes": 0, "episodes": 0},
        )
        entry["bytes"] += size
        entry["episodes"] += 1

    # Files in the media directory no episode claims: a renamed episode id, a
    # download interrupted before its row was updated, an unsubscribe.
    orphan_bytes = orphan_count = 0
    media_dir = Path(settings.media_dir)
    if media_dir.exists():
        for f in media_dir.iterdir():
            if not f.is_file() or _in_flight(f):
                continue
            try:
                if str(f.resolve()) in tracked_files:
                    continue
                orphan_bytes += f.stat().st_size
                orphan_count += 1
            except OSError:
                continue

    return {
        "total_bytes": total + orphan_bytes,
        "audio_bytes": total,
        "episodes": episodes,
        "orphan_bytes": orphan_bytes,
        "orphan_files": orphan_count,
        "by_podcast": sorted(by_podcast.values(), key=lambda p: -p["bytes"]),
        "policy": state,
    }


async def candidates(db, days: int, played_only: bool) -> list[dict]:
    """Episodes whose audio can go, oldest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, days))).isoformat()

    where = [
        "(e.local_path IS NOT NULL OR e.adfree_path IS NOT NULL)",
        # Never touch something queued up to listen to.
        "NOT EXISTS (SELECT 1 FROM queue q WHERE q.episode_id = e.id)",
        # Nor something half-heard: a position with no finish is intent.
        "NOT EXISTS (SELECT 1 FROM playback p WHERE p.episode_id = e.id "
        "            AND p.played = 0 AND p.position > 0)",
        # Old enough. `created_at` is when the row arrived here; published_at
        # covers rows that predate that column.
        "COALESCE(e.created_at, e.published_at, '') < ?",
    ]
    params: list = [cutoff]

    if played_only:
        where.append(
            "EXISTS (SELECT 1 FROM playback p2 "
            "WHERE p2.episode_id = e.id AND p2.played = 1)"
        )

    rows = await db.execute_fetchall(
        f"""SELECT e.id, e.title, e.local_path, e.adfree_path,
                   COALESCE(e.created_at, e.published_at) AS aged_at
            FROM episodes e
            WHERE {' AND '.join(where)}
            ORDER BY aged_at ASC""",
        tuple(params),
    )
    return [dict(r) for r in rows]


async def prune(days: int | None = None, played_only: bool | None = None,
                dry_run: bool = False, include_orphans: bool = True) -> dict:
    """Delete eligible audio. Returns what went, and how much that freed.

    Idempotent and safe to run from cron: with `days = 0` it does nothing at
    all, which is the default, so retention has to be turned on deliberately.
    """
    db = await get_db()
    try:
        state = await get_state(db)
        days = state["days"] if days is None else max(0, int(days))
        played_only = state["played_only"] if played_only is None else bool(played_only)

        if days <= 0:
            return {"status": "disabled", "freed_bytes": 0, "episodes": 0,
                    "orphans": 0, "cleared": []}

        rows = await candidates(db, days, played_only)
        freed = 0
        cleared: list[dict] = []

        for r in rows:
            episode_freed = 0
            for path in (r["local_path"], r["adfree_path"]):
                size = _size(path)
                if not path:
                    continue
                if not dry_run and size:
                    try:
                        Path(path).unlink()
                    except OSError as exc:
                        log.warning("could not delete %s: %s", path, exc)
                        continue
                episode_freed += size
            if not dry_run:
                await db.execute(
                    """UPDATE episodes
                          SET downloaded = 0, local_path = NULL, adfree_path = NULL
                        WHERE id = ?""",
                    (r["id"],),
                )
            freed += episode_freed
            cleared.append({"episode_id": r["id"], "title": r["title"],
                            "bytes": episode_freed})

        orphans = 0
        if include_orphans:
            orphan_freed, orphans = await _prune_orphans(db, dry_run)
            freed += orphan_freed

        if not dry_run:
            await db.commit()

        return {
            "status": "dry_run" if dry_run else "pruned",
            "days": days,
            "played_only": played_only,
            "freed_bytes": freed,
            "episodes": len(cleared),
            "orphans": orphans,
            "cleared": cleared[:50],
        }
    finally:
        await db.close()


async def _prune_orphans(db, dry_run: bool) -> tuple[int, int]:
    """Remove media files no episode row points at any more."""
    rows = await db.execute_fetchall(
        "SELECT local_path, adfree_path FROM episodes "
        "WHERE local_path IS NOT NULL OR adfree_path IS NOT NULL"
    )
    tracked: set[str] = set()
    for r in rows:
        for p in (r["local_path"], r["adfree_path"]):
            if p:
                try:
                    tracked.add(str(Path(p).resolve()))
                except OSError:
                    pass

    freed = count = 0
    media_dir = Path(settings.media_dir)
    if not media_dir.exists():
        return 0, 0
    for f in media_dir.iterdir():
        if not f.is_file() or _in_flight(f):
            continue
        try:
            if str(f.resolve()) in tracked:
                continue
            size = f.stat().st_size
            if not dry_run:
                f.unlink()
            freed += size
            count += 1
        except OSError as exc:
            log.warning("could not remove orphan %s: %s", f, exc)
    return freed, count

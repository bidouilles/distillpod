"""Downloads episode audio to local storage.

The single choke point for getting an episode's bytes onto disk, which is why
YouTube is handled here rather than at the call sites: `/player/play`, the
daily sync and the YouTube ingest task all go through this function, so they
all gain YouTube support without knowing anything about it.
"""
import asyncio
import hashlib
import httpx
from pathlib import Path
from config import settings
from services import jobs, youtube

# One lock per destination. A YouTube video is fetched in the background as
# soon as it is added, and the user may well hit Play while that is still
# running — without this both writers would interleave into the same file.
_locks: dict[str, asyncio.Lock] = {}


def episode_local_path(episode_id: str, audio_url: str) -> Path:
    ext = Path(audio_url.split("?")[0]).suffix or ".mp3"
    safe_id = hashlib.md5(episode_id.encode()).hexdigest()
    return settings.media_dir / f"{safe_id}{ext}"


def _lock_for(dest: Path) -> asyncio.Lock:
    return _locks.setdefault(str(dest), asyncio.Lock())


async def download_episode(episode_id: str, audio_url: str) -> Path:
    """Download audio to media_dir. Returns local path.

    Writes to a `.part` file and renames on success, so an interrupted download
    can never be mistaken for a complete one on the next call.
    """
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    dest = episode_local_path(episode_id, audio_url)

    async with _lock_for(dest):
        if dest.exists():
            return dest  # Already downloaded, or a concurrent call just finished it

        part = dest.with_name(dest.name + ".part")
        try:
            if youtube.is_youtube_url(audio_url):
                # yt-dlp takes its turn inside download_audio, in the youtube
                # lane — the constraint there is YouTube's rate limit, not this
                # box's bandwidth.
                await youtube.download_audio(audio_url, part)
            else:
                # One podcast download at a time. Three at once share the line
                # and all finish later; the person waiting to play wants the
                # first one done, not all three eventually.
                # Keyed, so a second browser pressing play on the same episode
                # waits for this download rather than queueing another.
                async with jobs.lane("media", label=f"download: {episode_id}",
                                     key=f"download:{episode_id}") as turn:
                    # A duplicate waited for the original and returns its file.
                    # If the original failed there is no file, and this turn
                    # does the work rather than leaving both callers with
                    # nothing — a shared job that failed must not be reported
                    # as a job that succeeded.
                    if turn.duplicate and dest.exists():
                        return dest
                    async with httpx.AsyncClient(follow_redirects=True, timeout=300) as client:
                        async with client.stream("GET", audio_url) as r:
                            r.raise_for_status()
                            with open(part, "wb") as f:
                                async for chunk in r.aiter_bytes(chunk_size=65536):
                                    f.write(chunk)
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)

    return dest


def delete_episode(episode_id: str, audio_url: str) -> None:
    path = episode_local_path(episode_id, audio_url)
    if path.exists():
        path.unlink()

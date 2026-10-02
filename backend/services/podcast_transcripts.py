"""Publisher transcripts. An unavailable link is an error, never proof of absence."""
import asyncio
import html
import ipaddress
import json
import math
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from database import get_db
from services import rss

MAX_BYTES = 8 * 1024 * 1024
TIMED_TYPES = {"text/vtt", "application/x-subrip", "application/srt", "application/json"}


async def _public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Invalid publisher transcript URL")
    addresses = await asyncio.get_running_loop().getaddrinfo(
        parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    if not addresses or any(not ipaddress.ip_address(addr[4][0]).is_global for addr in addresses):
        raise ValueError("Publisher transcript URL must resolve to a public address")


async def fetch_text(url: str) -> str:
    """Bound downloads and re-check each redirect before making its request."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
        for _ in range(6):
            await _public_url(url)
            async with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BYTES:
                        raise ValueError("Publisher transcript/feed exceeds 8 MiB")
                return content.decode("utf-8-sig")
    raise ValueError("Too many publisher transcript redirects")


def _seconds(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) not in (2, 3):
        raise ValueError("Invalid subtitle timestamp")
    result = 0.0
    for part in parts:
        result = result * 60 + float(part)
    return result


def parse_timed(text: str, mime: str) -> list[dict]:
    """Keep publisher cue text; distribute cue duration across its words.

    Cue-level sources provide approximate word timing, as human YouTube
    subtitles do. Never invent a position for a transcript with no timecodes.
    """
    cues = []
    if mime == "application/json":
        data = json.loads(text)
        segments = data.get("segments", []) if isinstance(data, dict) else data
        if not isinstance(segments, list):
            raise ValueError("Invalid JSON transcript segments")
        for segment in segments:
            cues.append((float(segment["startTime"]), float(segment["endTime"]), segment["body"]))
    else:
        for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
            lines = block.splitlines()
            if lines and lines[0].startswith(("NOTE", "STYLE", "REGION")):
                continue
            for index, line in enumerate(lines):
                if "-->" in line:
                    start, end = line.split("-->", 1)
                    cues.append((_seconds(start.strip()), _seconds(end.strip().split()[0]),
                                 " ".join(lines[index + 1:])))
                    break
    words = []
    for start, end, body in cues:
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
            raise ValueError("Invalid publisher transcript timing")
        tokens = html.unescape(re.sub(r"<[^>]*>", "", body)).split()
        for index, token in enumerate(tokens):
            words.append({"word": " " + token,
                          "start": start + (end - start) * index / len(tokens),
                          "end": start + (end - start) * (index + 1) / len(tokens)})
    if not words:
        raise ValueError("Publisher transcript contains no timed words")
    return sorted(words, key=lambda word: word["start"])


async def obtain(episode_id: str) -> tuple[list[dict], str]:
    db = await get_db()
    try:
        row = await db.execute_fetchone(
            """SELECT e.transcript_sources, s.feed_url FROM episodes e
               LEFT JOIN subscriptions s ON s.podcast_id = e.podcast_id WHERE e.id = ?""",
            (episode_id,),
        )
        if not row or not row["feed_url"]:
            raise ValueError("Cannot check publisher transcripts: episode/feed missing")
        if row["transcript_sources"] is None:
            sources = rss.transcript_links(await fetch_text(row["feed_url"]))
            if episode_id not in sources:
                raise ValueError("Episode is absent from RSS; cannot verify transcript availability")
            links = sources[episode_id]
            await db.execute("UPDATE episodes SET transcript_sources = ? WHERE id = ?",
                             (json.dumps(links), episode_id))
            await db.commit()
        else:
            links = json.loads(row["transcript_sources"])
    finally:
        await db.close()
    if not links:
        return [], ""
    errors = []
    for link in links:
        mime = link.get("type", "").split(";", 1)[0].strip().lower()
        if mime not in TIMED_TYPES:
            continue
        try:
            text = await fetch_text(link["url"])
            return parse_timed(text, mime), link.get("language", "")
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(str(exc))
    reason = "; ".join(errors) if errors else "only untimed or unsupported formats are linked"
    raise ValueError(f"Publisher transcript exists but cannot be imported: {reason}")

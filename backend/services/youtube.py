"""
YouTube videos as episodes — the yt-dlp adapter.

A video is turned into an ordinary `episodes` row so every existing feature
(player, transcript search, distills, chat, research) works on it unchanged.
Only two things are genuinely YouTube-specific and both live here:

  metadata   one `yt-dlp -J` call returns the title, channel, duration,
             thumbnail, the creator's own chapter marks, and the URLs of every
             caption track. That is a single round-trip for everything the
             episode row needs.

  captions   YouTube's `json3` caption format carries a timestamp per *word*
             (`tOffsetMs` inside each event), which is exactly the shape
             services/stt.py has to produce. So when a video is captioned the
             transcript is free and instant, and the STT backend is only needed
             for videos that have none.

The caption track URLs handed back by `-J` are plain HTTPS and fetch fine with
httpx, so reading captions costs no second yt-dlp invocation. yt-dlp is kept as
a fallback for the day that stops being true.

This module never touches the database — see routers/youtube.py for that.
"""
import asyncio
import json
import logging
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from config import settings

log = logging.getLogger(__name__)

# Long enough for a slow extractor round-trip, short enough that a wedged
# yt-dlp cannot pin a request forever.
METADATA_TIMEOUT = 120
CAPTION_TIMEOUT = 60
# Audio extraction re-encodes to mp3, so a multi-hour video takes a while.
DOWNLOAD_TIMEOUT = 1800

_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be",
}

# The 11-char id, wherever it hides: watch?v=, youtu.be/, /shorts/, /embed/, /live/.
_VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/|/embed/|/live/)([A-Za-z0-9_-]{11})")


class YouTubeError(RuntimeError):
    """yt-dlp failed, or the URL is not a video we can ingest."""


def is_youtube_url(url: str) -> bool:
    if not url:
        return False
    m = re.match(r"^https?://([^/:?#]+)", url.strip(), re.I)
    return bool(m) and m.group(1).lower() in _HOSTS


def video_id(url: str) -> Optional[str]:
    """The 11-char video id, or None if this URL does not carry one."""
    if not is_youtube_url(url):
        return None
    m = _VIDEO_ID.search(url.strip())
    return m.group(1) if m else None


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def _ytdlp() -> str:
    return settings.ytdlp_bin or shutil.which("yt-dlp") or "yt-dlp"


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Blocking yt-dlp call. Raises YouTubeError with the tail of stderr."""
    try:
        return subprocess.run(
            [_ytdlp(), *args], capture_output=True, text=True,
            check=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise YouTubeError(
            "yt-dlp is not installed or not on PATH (set YTDLP_BIN)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise YouTubeError(f"yt-dlp timed out after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "").strip().splitlines()[-3:]
        raise YouTubeError("yt-dlp failed: " + " / ".join(tail)) from exc


# /@handle, /channel/UC…, /c/name, /user/name — with or without a trailing
# tab like /videos, /shorts or /streams.
_CHANNEL_PATH = re.compile(r"^/(?:@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(?:/|$)", re.I)
_CHANNEL_ID = re.compile(r"/channel/(UC[A-Za-z0-9_-]{20,})", re.I)

# Anything shorter than this is a clip rather than something to listen to. The
# /videos tab already excludes Shorts structurally, so this is only a backstop.
MIN_VIDEO_SECONDS = 120

# Live and upcoming streams are not the thing being subscribed to, and a
# finished stream is usually hours of unedited talk.
_LIVE_STATUSES = {"is_live", "is_upcoming", "post_live", "was_live"}


def is_channel_url(url: str) -> bool:
    """A channel or handle URL, as opposed to a single video."""
    if not is_youtube_url(url) or video_id(url):
        return False
    m = re.match(r"^https?://[^/]+(/[^?#]*)", url.strip())
    return bool(m) and bool(_CHANNEL_PATH.match(m.group(1)))


def channel_videos_url(channel_id: str) -> str:
    """The channel's long-form uploads.

    Deliberately the /videos tab rather than the channel's Atom feed. The tab
    excludes Shorts and live streams by construction, and yt-dlp returns a
    duration and a live status for each entry, neither of which the feed
    carries. (The feed also 404s for any non-browser User-Agent, and the
    UULF/UUSH derived-playlist feeds 404 outright.)
    """
    return f"https://www.youtube.com/channel/{channel_id}/videos"


def _resolve_channel_blocking(url: str) -> dict:
    """Channel id, name and avatar for a channel or @handle URL.

    `--playlist-items 1` because only the channel's own metadata is wanted;
    enumerating a large channel here would be slow and is what the listing is for.
    """
    out = _run(
        ["-J", "--flat-playlist", "--playlist-items", "1", "--no-warnings", url],
        METADATA_TIMEOUT,
    )
    try:
        meta = json.loads(out.stdout)
    except ValueError as exc:
        raise YouTubeError("yt-dlp returned no usable channel metadata") from exc

    channel_id = meta.get("channel_id") or ""
    if not channel_id:
        m = _CHANNEL_ID.search(meta.get("channel_url") or meta.get("webpage_url") or "")
        channel_id = m.group(1) if m else ""
    if not channel_id:
        raise YouTubeError("Could not work out which channel that URL points to")

    thumbs = meta.get("thumbnails") or []
    return {
        "channel_id": channel_id,
        "title": meta.get("channel") or meta.get("uploader") or meta.get("title") or "YouTube channel",
        "thumbnail": (thumbs[-1].get("url") if thumbs else None),
    }


async def resolve_channel(url: str) -> dict:
    return await asyncio.to_thread(_resolve_channel_blocking, url)


def _channel_videos_blocking(channel_id: str, limit: int) -> list[dict]:
    out = _run(
        ["-J", "--flat-playlist", "--playlist-items", f"1-{limit}", "--no-warnings",
         # Without this the tab reports a null timestamp for many channels, and
         # an episode with no publish date sinks in the feed and falls outside
         # the nightly recency window. Day-granular is plenty for ordering.
         "--extractor-args", "youtubetab:approximate_date",
         channel_videos_url(channel_id)],
        METADATA_TIMEOUT,
    )
    try:
        data = json.loads(out.stdout)
    except ValueError as exc:
        raise YouTubeError("yt-dlp returned no usable channel listing") from exc

    videos = []
    for entry in data.get("entries") or []:
        vid = entry.get("id")
        if not vid:
            continue
        if (entry.get("live_status") or "") in _LIVE_STATUSES:
            continue
        duration = entry.get("duration")
        # No duration means yt-dlp could not tell — usually a stream placeholder.
        if not duration or duration < MIN_VIDEO_SECONDS:
            continue
        ts = entry.get("timestamp") or entry.get("release_timestamp")
        videos.append({
            "video_id": vid,
            "title": entry.get("title") or "Untitled video",
            "url": entry.get("url") or watch_url(vid),
            "duration_seconds": int(duration),
            "published_at": datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        })
    return videos


# The Atom feed 404s for a non-browser User-Agent — curl's default and httpx's
# both get an error page rather than the document. Verified against a live feed.
FEED_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def channel_feed_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


async def _feed_dates(channel_id: str) -> dict[str, datetime]:
    """Publish dates for the channel's recent uploads, keyed by video id.

    The /videos listing returns a null timestamp for some channels, and the
    obvious fix — asking yt-dlp for each video's metadata — is what tripped
    YouTube's bot check hard enough to get the address refused for a while.
    This is one cheap request for the whole channel instead.
    """
    import feedparser

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            r = await client.get(channel_feed_url(channel_id),
                                 headers={"User-Agent": FEED_USER_AGENT})
            r.raise_for_status()
            body = r.text
    except Exception as exc:
        log.info("channel feed unavailable for %s: %s", channel_id, exc)
        return {}

    dates: dict[str, datetime] = {}
    for entry in feedparser.parse(body).entries:
        vid = entry.get("yt_videoid")
        if vid and entry.get("published_parsed"):
            dates[vid] = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
    return dates


async def fetch_channel_videos(channel_id: str, limit: int = 15) -> list[dict]:
    """Recent long-form uploads, newest first. Shorts and streams excluded.

    Two requests for the whole channel and none per video: the /videos tab
    decides what counts and how long it runs, the Atom feed supplies the
    publish dates the tab omits.
    """
    videos = await asyncio.to_thread(_channel_videos_blocking, channel_id, limit)
    if not videos:
        return []
    dates = await _feed_dates(channel_id)
    for v in videos:
        v["published_at"] = dates.get(v["video_id"]) or v["published_at"]
    return videos


# ── Metadata ─────────────────────────────────────────────────────────────────

def _fetch_metadata_blocking(url: str) -> dict:
    out = _run(
        ["-J", "--no-playlist", "--skip-download", "--no-warnings", url],
        METADATA_TIMEOUT,
    )
    try:
        meta = json.loads(out.stdout)
    except ValueError as exc:
        raise YouTubeError("yt-dlp returned no usable metadata") from exc
    # A channel or playlist URL yields a container, not a video.
    if meta.get("_type") not in (None, "video"):
        raise YouTubeError("That URL is a playlist or channel, not a single video")
    return meta


async def fetch_metadata(url: str) -> dict:
    """Everything about one video in a single call. Off the event loop."""
    return await asyncio.to_thread(_fetch_metadata_blocking, url)


def published_at(meta: dict) -> Optional[datetime]:
    ts = meta.get("timestamp")
    if ts:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    raw = meta.get("upload_date")     # "YYYYMMDD"
    if raw and len(str(raw)) == 8:
        try:
            return datetime.strptime(str(raw), "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def chapters(meta: dict) -> list[dict]:
    """The uploader's own chapter marks, in the shape the chapters table wants.

    Free and human-written, so there is no reason to spend a model call
    generating them for a video that already ships with them.
    """
    out = []
    for ch in meta.get("chapters") or []:
        title = (ch.get("title") or "").strip()
        start = ch.get("start_time")
        if title and start is not None:
            out.append({"title": title, "start_time": float(start)})
    return out


# ── Captions ─────────────────────────────────────────────────────────────────

def original_language(meta: dict) -> str:
    """The language actually spoken in the video, as a bare code ("fr", "en").

    Three sources, in decreasing order of authority. yt-dlp's `language` is the
    video's declared audio language and settles it outright. Failing that,
    `automatic_captions` carries exactly one "<lang>-orig" key, and the language
    it names is the one YouTube ran speech recognition on — which is the
    language being spoken. Failing both, a video with a single hand-written
    subtitle track is almost certainly subtitled in its own language.
    """
    lang = (meta.get("language") or "").split("-")[0]
    if lang:
        return lang

    for key in meta.get("automatic_captions") or {}:
        if key.endswith("-orig"):
            return key[: -len("-orig")].split("-")[0]

    manual = [k for k in (meta.get("subtitles") or {}) if k != "live_chat"]
    if len(manual) == 1:
        return manual[0].split("-")[0]
    return ""


def caption_track(meta: dict) -> Optional[tuple[str, str]]:
    """(language, json3 URL) of the best caption track, or None.

    Always the video's *own* language: a French video is transcribed in French
    and an English one in English. `automatic_captions` lists all ~157 languages
    YouTube can machine-translate into, so taking anything else would store a
    transcript describing audio nobody can hear saying it — search would match
    words that were never spoken, and quotes in distills would be inventions.

    Within that language, human-written subtitles beat auto-captions: a person
    wrote them, so they are punctuated and spelled right. Among auto-captions
    the "-orig" track is the speech recognition output itself, while the bare
    language code is nominally the machine translation into that same language;
    YouTube usually serves identical bytes for both, but "-orig" is the one that
    says what it is.

    Returning None is a normal outcome, not a failure — the caller falls back
    to the STT backend, which transcribes the audio that is actually there.
    """
    lang = original_language(meta)
    if not lang:
        return None

    manual = meta.get("subtitles") or {}
    auto = meta.get("automatic_captions") or {}

    def json3(tracks) -> Optional[str]:
        for track in tracks or []:
            if track.get("ext") == "json3" and track.get("url"):
                return track["url"]
        return None

    for source, keys in ((manual, (lang, f"{lang}-orig")), (auto, (f"{lang}-orig", lang))):
        for key in keys:
            url = json3(source.get(key))
            if url:
                return key, url
        # Regional variants ("fr-CA-orig") still count as the original language.
        for key in source:
            if key.split("-")[0] == lang:
                url = json3(source.get(key))
                if url:
                    return key, url
    return None


def _words_from_json3(payload: dict, duration: Optional[float] = None) -> list[dict]:
    """json3 events -> [{word, start, end}] with the leading space kept.

    An event is a caption line and a seg is a timed slice of it. Auto-captions
    put one word per seg with its own `tOffsetMs`, which maps straight onto the
    word list. Human-written subtitles, though, put a whole *line* in a single
    seg — and a transcript of phrase-long "words" cannot be used: distill
    windows, the ad segmenter and chapter seeks all index into this array, so a
    two-second blob makes every seek land two seconds early. So any seg holding
    more than one word is split, and its span shared out across them by word
    length. Those timings are interpolated rather than measured, but they are
    monotonic and within a word of the truth, which is what the seeking needs.

    Roll-up captions repeat a line as it grows and mark the repeat with a lone
    "\n" seg. Those are dropped, and the space they stood for is put back by
    giving every word but the first its own leading space — the same convention
    services/stt.py follows, so "".join(words) reads as prose.
    """
    words: list[dict] = []

    def emit(text: str, start_ms: float, end_ms: float) -> None:
        tokens = text.split()
        if not tokens:
            return
        span = max(end_ms - start_ms, 0.0)
        total = sum(len(t) for t in tokens)
        cursor = start_ms
        for i, token in enumerate(tokens):
            share = span * (len(token) / total) if total else 0.0
            token_end = end_ms if i == len(tokens) - 1 else cursor + share
            words.append({
                "word": (" " if words else "") + token,
                "start": round(cursor / 1000, 3),
                "end": round(max(token_end, cursor) / 1000, 3),
            })
            cursor = token_end

    for event in payload.get("events") or []:
        segs = event.get("segs")
        if not segs:
            continue
        line_start = event.get("tStartMs") or 0
        line_end = line_start + (event.get("dDurationMs") or 0)

        timed: list[tuple[float, str]] = []
        for seg in segs:
            text = seg.get("utf8") or ""
            if not text.strip():        # newline / padding artifact
                continue
            timed.append((line_start + (seg.get("tOffsetMs") or 0), text))

        for i, (seg_start, text) in enumerate(timed):
            seg_end = timed[i + 1][0] if i + 1 < len(timed) else line_end
            emit(text, seg_start, max(seg_end, seg_start))

    # Roll-up lines overlap, so a line's last word can end after the next line
    # starts. Everything downstream assumes a well-formed, non-overlapping
    # interval.
    for i, w in enumerate(words[:-1]):
        w["end"] = max(w["start"], min(w["end"], words[i + 1]["start"]))
    if words and duration:
        words[-1]["end"] = max(words[-1]["start"], min(words[-1]["end"], float(duration)))
    return words


def _fetch_json3_via_ytdlp(url: str, lang: str) -> Optional[dict]:
    """Fallback for the day the caption URLs stop being directly fetchable."""
    with tempfile.TemporaryDirectory(prefix="distillpod-yt-") as tmp:
        _run([
            "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-langs", lang, "--sub-format", "json3", "--no-warnings",
            "--no-playlist", "-o", str(Path(tmp) / "cap.%(ext)s"), url,
        ], CAPTION_TIMEOUT)
        for path in Path(tmp).glob("cap.*.json3"):
            try:
                return json.loads(path.read_text())
            except ValueError:
                return None
    return None


async def fetch_caption_words(meta: dict) -> list[dict]:
    """Word-level transcript from YouTube's own captions. [] when there are none."""
    track = caption_track(meta)
    if not track:
        return []
    lang, url = track

    payload: Optional[dict] = None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=CAPTION_TIMEOUT) as client:
            r = await client.get(url)
            r.raise_for_status()
            payload = r.json()
    except Exception as exc:
        log.info("caption URL fetch failed (%s), retrying via yt-dlp", exc)
        try:
            payload = await asyncio.to_thread(
                _fetch_json3_via_ytdlp, meta.get("webpage_url") or "", lang
            )
        except YouTubeError as exc:
            log.info("yt-dlp caption fallback failed: %s", exc)
            return []

    if not payload:
        return []
    return _words_from_json3(payload, meta.get("duration"))


def caption_language(meta: dict) -> str:
    """The language stored against a caption transcript."""
    return original_language(meta) if caption_track(meta) else ""


# ── Audio ────────────────────────────────────────────────────────────────────

def _download_audio_blocking(url: str, dest: Path) -> Path:
    """Extract audio to mp3 at `dest` (whatever extension `dest` carries).

    yt-dlp insists on choosing the extension itself, so it writes into a scratch
    directory and the result is moved into place.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="distillpod-yt-") as tmp:
        _run([
            "-x", "--audio-format", "mp3", "--audio-quality", "5",
            "--no-playlist", "--no-warnings",
            "-o", str(Path(tmp) / "audio.%(ext)s"), url,
        ], DOWNLOAD_TIMEOUT)
        produced = next(iter(sorted(Path(tmp).glob("audio.*"))), None)
        if produced is None:
            raise YouTubeError("yt-dlp produced no audio file")
        shutil.move(str(produced), str(dest))
    return dest


async def download_audio(url: str, dest: Path) -> Path:
    return await asyncio.to_thread(_download_audio_blocking, url, dest)

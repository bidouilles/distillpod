from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class Podcast(BaseModel):
    id: str                        # Podcast Index feed ID
    title: str
    author: str
    description: str
    image_url: Optional[str] = None
    feed_url: str
    website_url: Optional[str] = None
    episode_count: Optional[int] = None


class Episode(BaseModel):
    id: str                        # guid from RSS
    podcast_id: str
    title: str
    description: Optional[str] = None
    audio_url: str
    duration_seconds: Optional[int] = None
    published_at: Optional[datetime] = None
    image_url: Optional[str] = None
    # Local state
    downloaded: bool = False
    local_path: Optional[str] = None
    transcript_status: str = "none"   # none | queued | processing | done | error
    adfree_path: Optional[str] = None
    ads_detected: Optional[int] = None
    summary: Optional[str] = None
    chapters_status: str = "none"  # none | processing | done | error


class Tag(BaseModel):
    id: str
    name: str
    podcast_count: int = 0


class PodcastSettings(BaseModel):
    """Per-podcast playback preferences.

    Every field is optional and `None` means "no opinion": the player keeps
    using whatever it was set to globally, rather than being reset to a stored
    default nobody chose. `auto_transcribe` is the exception worth naming — it
    is a load control, since transcription is the one stage that can cost money
    or pin a core for minutes.
    """
    playback_rate: Optional[float] = None      # 0.5 – 3.0
    skip_intro: Optional[int] = None           # seconds skipped at the start
    skip_outro: Optional[int] = None           # seconds treated as the end
    prefer_adfree: Optional[bool] = None       # open the ad-free cut by default
    auto_transcribe: Optional[bool] = None     # False = never transcribe this show


class Subscription(BaseModel):
    podcast_id: str
    feed_url: str
    title: str
    image_url: Optional[str] = None
    last_checked: Optional[datetime] = None
    subscribed_at: datetime
    # What this row actually is, so the library can show it:
    # "podcast" | "youtube_channel" | "youtube_video" (one video, not subscribed).
    source: str = "podcast"
    tags: list[Tag] = []
    settings: PodcastSettings = PodcastSettings()


class TranscriptWord(BaseModel):
    word: str
    start: float    # seconds
    end: float      # seconds


class Transcript(BaseModel):
    episode_id: str
    words: list[TranscriptWord]
    language: Optional[str] = None
    created_at: datetime


class Gist(BaseModel):
    id: str
    episode_id: str
    podcast_id: str
    episode_title: str
    podcast_title: str
    start_seconds: float
    end_seconds: float
    text: str                     # extracted from transcript
    summary: Optional[str] = None  # Claude summary (optional, via CLI subprocess)
    created_at: datetime
    # Picked by the nightly job rather than tapped while listening. Shown as
    # such, because a suggestion the user never made deserves to say so.
    auto: bool = False


# Request / Response schemas

class GistRequest(BaseModel):
    episode_id: str
    current_seconds: float        # playback position when user tapped Gist


class PlayRequest(BaseModel):
    episode_id: str
    audio_url: str                # original RSS audio URL


class ProgressUpdate(BaseModel):
    """Every field optional: callers update position, finishedness, or both.

    Saving a position and marking an episode finished are separate events —
    one fires every few seconds while listening, the other once at the end —
    so neither should have to restate the other and risk overwriting it.
    """
    position: Optional[float] = None
    duration: Optional[float] = None
    played: Optional[bool] = None


class Bookmark(BaseModel):
    """A quote kept from the transcript, with no model call behind it."""
    id: str
    episode_id: str
    start_seconds: float
    end_seconds: float
    text: str
    note: Optional[str] = None
    created_at: datetime
    # Filled in when listing across episodes, so the library view can render a
    # bookmark without a second request per row.
    episode_title: Optional[str] = None
    podcast_title: Optional[str] = None
    podcast_image: Optional[str] = None


class BookmarkRequest(BaseModel):
    """Either a moment (`seconds`) or an explicit span from the transcript.

    Tapping "bookmark" in the player knows only where playback is, so the
    server looks up the sentence around it. Long-pressing a transcript line
    already knows exactly which words it means, and says so.
    """
    episode_id: str
    seconds: Optional[float] = None
    start_seconds: Optional[float] = None
    end_seconds: Optional[float] = None
    text: Optional[str] = None
    note: Optional[str] = None


class BookmarkNote(BaseModel):
    note: Optional[str] = None


class QueueItemRef(BaseModel):
    episode_id: str


class QueueOrder(BaseModel):
    """The whole queue, in order. Replacing wholesale rather than patching:
    a drag-and-drop reorder is one intent, and sending the result avoids
    inventing a move protocol that two devices could interleave."""
    episode_ids: list[str]


class PlaylistRules(BaseModel):
    """What a smart playlist selects. Every field is optional; those set are
    ANDed together, which is the only combination a rule row can express and
    the only one anyone tried to build."""
    unplayed: bool = False
    status: Optional[str] = None          # transcribed | distilled | adfree | downloaded
    tag_id: Optional[str] = None
    podcast_id: Optional[str] = None
    min_minutes: Optional[int] = None
    max_minutes: Optional[int] = None
    sort: str = "newest"                  # newest | oldest | shortest | longest
    limit: int = 50


class PlaylistCreate(BaseModel):
    name: str
    kind: str = "manual"                  # manual | smart
    rules: Optional[PlaylistRules] = None


class PlaylistUpdate(BaseModel):
    name: Optional[str] = None
    rules: Optional[PlaylistRules] = None


class Playlist(BaseModel):
    id: str
    name: str
    kind: str
    rules: Optional[PlaylistRules] = None
    created_at: datetime
    episode_count: int = 0
    # Enough artwork to draw a stack on the card without fetching the episodes.
    images: list[str] = []


class TranscriptStatus(BaseModel):
    episode_id: str
    status: str                   # none | queued | processing | done | error
    progress_percent: Optional[float] = None

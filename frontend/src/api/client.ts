const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8124";

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    method,
    credentials: "include",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) {
    // Session expired or missing — navigate to root to trigger auth check in App.tsx
    window.location.href = "/";
    throw new Error("Unauthorized");
  }
  if (!r.ok) throw new Error(`${method} ${path} → ${r.status}`);
  return r.json();
}

// --- Podcasts ---
export const searchPodcasts = (q: string) =>
  req<Podcast[]>("GET", `/podcasts/search?q=${encodeURIComponent(q)}`);

export const getSubscriptions = () =>
  req<Subscription[]>("GET", "/podcasts/subscriptions");

export const subscribe = (podcastId: string, feedUrl: string, title: string, imageUrl?: string) =>
  req("POST", `/podcasts/subscriptions/${podcastId}?feed_url=${encodeURIComponent(feedUrl)}&title=${encodeURIComponent(title)}${imageUrl ? `&image_url=${encodeURIComponent(imageUrl)}` : ""}`);

export const unsubscribe = (podcastId: string) =>
  req("DELETE", `/podcasts/subscriptions/${podcastId}`);

export interface FeedFilters {
  q?: string;
  tag_id?: string;
  podcast_id?: string;
  status?: string;
  /** Never opened, on any device — read from server-side playback state. */
  unplayed?: boolean;
  min_minutes?: number;
  max_minutes?: number;
  sort?: FeedSort;
  limit?: number;
}

export type FeedSort = "newest" | "oldest" | "shortest" | "longest";

export const getFeed = (filters: FeedFilters = {}) => {
  // Filtering runs server-side: the feed is capped, so filtering an
  // already-truncated page would hide matches older than the cap.
  const qs = new URLSearchParams();
  Object.entries(filters).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  });
  const suffix = qs.toString() ? `?${qs}` : "";
  return req<FeedEpisode[]>("GET", `/podcasts/feed${suffix}`);
};

// --- Transcript search ---
export interface TranscriptMatch { start: number; text: string; }
export interface TranscriptHit {
  episode_id: string;
  episode_title: string;
  podcast_id: string;
  podcast_title: string;
  podcast_image?: string;
  published_at?: string;
  duration_seconds?: number;
  match_count: number;
  matches: TranscriptMatch[];
}

export const searchTranscripts = (q: string) =>
  req<TranscriptHit[]>("GET", `/search/transcripts?q=${encodeURIComponent(q)}`);

// --- Tags ---
export const getTags = () => req<Tag[]>("GET", "/tags");

export const createTag = (name: string) =>
  req<Tag>("POST", "/tags", { name });

export const deleteTag = (id: string) => req("DELETE", `/tags/${id}`);

export const setPodcastTags = (podcastId: string, tagIds: string[]) =>
  req<Tag[]>("PUT", `/tags/podcast/${podcastId}`, { tag_ids: tagIds });

export const getSuggestions = () =>
  req<Suggestion[]>("GET", "/podcasts/suggestions");

export const dismissSuggestion = (id: string) =>
  req("POST", `/podcasts/suggestions/${id}/dismiss`);

export const getEpisodes = (podcastId: string, refresh = false) =>
  req<Episode[]>("GET", `/podcasts/${podcastId}/episodes?refresh=${refresh}`);

// --- Player ---
export const startPlay = (episodeId: string, audioUrl: string) =>
  req<{
    audio_url: string;
    transcript_status: string;
    status: "ready" | "downloading";
    /** The podcast's preferences, returned here so the player has them at the
     *  moment it needs them rather than a round trip later. */
    settings?: PodcastSettings;
  }>("POST", "/player/play", { episode_id: episodeId, audio_url: audioUrl });

/** Downloads run server-side, so several can be fetched at once and none of
 *  them depend on a screen staying open. */
export const getDownloadStatus = (episodeId: string) =>
  req<{ downloaded: boolean; downloading: boolean; error: string | null }>(
    "GET", `/player/download-status/${episodeId}`
  );

export const getTranscriptStatus = (episodeId: string) =>
  req<{ status: string }>("GET", `/player/transcript-status/${episodeId}`);

export const getEpisode = (episodeId: string) =>
  req<Episode>("GET", `/player/episode/${episodeId}`);

export const audioStreamUrl = (episodeId: string) => `${BASE}/player/audio/${episodeId}`;

// --- Shots ---
export const createGist = (episodeId: string, currentSeconds: number) =>
  req<Gist>("POST", `/gists/`, {
    episode_id: episodeId,
    current_seconds: currentSeconds,
  });

export const listGists = (episodeId?: string) =>
  req<Gist[]>("GET", episodeId ? `/gists/?episode_id=${episodeId}` : "/gists/");

export const deleteGist = (snipId: string) =>
  req("DELETE", `/gists/${snipId}`);

// --- Types ---
export interface Podcast {
  id: string; title: string; author: string; description: string;
  image_url?: string; feed_url: string; episode_count?: number;
}
export interface Tag {
  id: string;
  name: string;
  podcast_count?: number;
}
export interface Subscription {
  podcast_id: string; feed_url: string; title: string; image_url?: string;
  subscribed_at: string; tags?: Tag[];
  /** "podcast" | "youtube_channel" | "youtube_video" (one video, not subscribed). */
  source?: string;
  settings?: PodcastSettings;
}
export interface Episode {
  id: string; podcast_id: string; title: string; description?: string;
  audio_url: string; duration_seconds?: number; published_at?: string;
  image_url?: string; downloaded: boolean; transcript_status: string;
  ads_detected?: number;
}
export interface FeedEpisode extends Episode {
  podcast_title: string;
  podcast_image?: string;
  distill_count: number;
  bookmark_count: number;
  /** 0/1 from SQLite — already in Up Next. */
  queued: number;
  /** 0/1 — opened on some device. Server-side, so it does not lie across devices. */
  played: number;
  position: number;
  created_at?: string;
}
export interface Suggestion {
  id: string; title: string; author?: string; description?: string;
  image_url?: string; feed_url: string; podcast_index_id?: string;
  reason?: string; suggested_at: string;
}
export interface Gist {
  id: string; episode_id: string; podcast_id: string;
  episode_title: string; podcast_title: string;
  start_seconds: number; end_seconds: number;
  text: string; summary?: string; created_at: string;
  /** Picked by the nightly job rather than tapped while listening. */
  auto?: boolean;
}

// --- Chat ---
export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
}

export const getChat = (episodeId: string) =>
  req<ChatMessage[]>("GET", `/chat/${episodeId}`);

export const initChat = (episodeId: string) =>
  req<ChatMessage>("POST", `/chat/${episodeId}/init`);

export const sendChatMessage = (episodeId: string, message: string) =>
  req<ChatMessage>("POST", `/chat/${episodeId}/message`, { message });

// --- Research ---
export interface Research {
  id?: string;
  status: "none" | "pending" | "running" | "done" | "error";
  public_url?: string;
  error?: string;
}

export const triggerResearch = (gistId: string) =>
  req<Research>("POST", `/research/${gistId}`);

export const getResearch = (gistId: string) =>
  req<Research>("GET", `/research/${gistId}`);

// --- Ad-free ---
export interface AdFreeStatus {
  has_adfree: boolean;
  ads_count: number;
}

export async function getAdFreeStatus(episodeId: string): Promise<AdFreeStatus> {
  return req<AdFreeStatus>('GET', `/player/adfree-status/${episodeId}`);
}

export const adFreeAudioUrl = (episodeId: string) =>
  `${BASE}/player/audio-adfree/${episodeId}`;

// --- Chapters ---
export interface Chapter {
  title: string;
  start_time: number; // seconds
}

export interface ChaptersResult {
  episode_id: string;
  chapters_status: "none" | "processing" | "done" | "error";
  summary: string | null;
  chapters: Chapter[];
}

export const getChapters = (episodeId: string) =>
  req<ChaptersResult>('GET', `/player/chapters/${episodeId}`);

// --- YouTube ---
// A video is ingested as an ordinary episode, so nothing else in the client
// needs to know it came from YouTube.
export interface YouTubeAddResult {
  /** "video" ingests one episode; "channel" subscribes and imports in the background. */
  kind: "video" | "channel";
  episode_id?: string;
  podcast_id: string;
  channel_id?: string;
  title: string;
  channel?: string;
  image_url?: string;
  duration_seconds?: number;
  chapters?: number;
  has_captions?: boolean;
  already_added: boolean;
}

export const addYoutubeVideo = (url: string) =>
  req<YouTubeAddResult>("POST", "/youtube/add", { url });

// --- Read-along transcript ---
// [start, end, text] triples rather than objects: an hour of speech is ~10k
// words, and repeating three JSON keys on each roughly doubles the payload.
export type TranscriptWord = [number, number, string];

export interface TranscriptResult {
  episode_id: string;
  language?: string;
  words: TranscriptWord[];
}

export const getTranscript = (episodeId: string) =>
  req<TranscriptResult>("GET", `/player/transcript/${episodeId}`);

// --- Playback progress (cross-device resume) ---
// Server-side so an episode started on the phone resumes on the laptop. The
// client keeps a localStorage copy for instant, offline reads; this is the
// copy the devices agree on.
export interface ProgressRecord {
  episode_id: string;
  position: number;
  duration?: number;
  played: boolean;
  updated_at: string;
  title?: string | null;
  podcast_title?: string | null;
  podcast_image?: string | null;
}

export const getProgress = () =>
  req<ProgressRecord[]>("GET", "/player/progress");

export const putProgress = (
  episodeId: string,
  body: { position?: number; duration?: number; played?: boolean },
) => req<{ episode_id: string; updated_at: string }>("PUT", `/player/progress/${episodeId}`, body);

export const deleteProgress = (episodeId: string) =>
  req("DELETE", `/player/progress/${episodeId}`);

/** Ask the server to pick highlights from an already-transcribed episode.
 *  Returns immediately; poll listGists to see them arrive. */
export const autoSnipEpisode = (episodeId: string) =>
  req<{ episode_id: string; status: string }>("POST", `/gists/auto/${episodeId}`);

// --- Obsidian export ---
export interface ExportedNote {
  episode_id: string;
  title: string;
  /** false when the model half was skipped or unavailable — the note is still usable. */
  enriched: boolean;
  markdown: string;
}

export const exportNote = (episodeId: string, enrich = true) =>
  req<ExportedNote>("GET", `/player/export/${episodeId}?enrich=${enrich}`);

/** What an episode is about, generated on first open and stored after. */
export const getBrief = (episodeId: string) =>
  req<{ episode_id: string; summary: string | null; generated: boolean }>(
    "GET", `/player/brief/${episodeId}`
  );

/** Check every subscription for new episodes, now, rather than waiting for the
 *  nightly job. Fetches feeds and channel listings; transcribes nothing. */
export interface RefreshState {
  status?: string;
  running: boolean;
  new: number;
  checked: number;
  failed: number;
  finished_at: string | null;
}

export const refreshSubscriptions = () =>
  req<RefreshState>("POST", "/podcasts/refresh");

/** A refresh runs server-side, so it survives leaving the screen. */
export const getRefreshStatus = () =>
  req<RefreshState>("GET", "/podcasts/refresh/status");

// --- Transcript backfill (captions only, never speech-to-text) ---
export interface BackfillState {
  running: boolean;
  total: number;
  transcribed: number;
  /** Videos with no captions — left alone rather than sent to a paid backend. */
  no_captions: number;
  failed: number;
  current: string | null;
  stopped_early: boolean;
  finished_at: string | null;
  pending: number;
}

export const getBackfillStatus = () =>
  req<BackfillState>("GET", "/player/backfill/status");

export const startBackfill = () =>
  req<{ status: string; pending?: number }>("POST", "/player/backfill");

export const stopBackfill = () =>
  req<{ status: string }>("POST", "/player/backfill/stop");

// --- Up Next (server-side) ---
// The queue lives on the server for the same reason playback positions do: a
// queue built on the laptop is only useful if the phone has it too. The store
// in `stores/queueStore.ts` keeps a local mirror for instant, offline reads.
export interface QueueRow {
  episode_id: string;
  title: string;
  podcast_id?: string;
  podcast_title?: string;
  audio_url: string;
  image_url?: string;
  duration_seconds?: number;
  transcript_status?: string;
  added_at: string;
}

export const getQueue = () => req<QueueRow[]>("GET", "/queue");

export const enqueueEpisode = (episodeId: string, position: "next" | "end" = "end") =>
  req<QueueRow[]>("POST", `/queue/${episodeId}?position=${position}`);

export const dequeueEpisode = (episodeId: string) =>
  req<QueueRow[]>("DELETE", `/queue/${episodeId}`);

export const replaceQueue = (episodeIds: string[]) =>
  req<QueueRow[]>("PUT", "/queue", { episode_ids: episodeIds });

export const clearQueue = () => req<QueueRow[]>("DELETE", "/queue");

// --- Bookmarks ---
// The cheap half of ⚗️: a distill costs a CLI round trip and ~30s, a bookmark
// costs an INSERT. That difference is the whole point — it can be tapped as
// often as it is useful.
export interface Bookmark {
  id: string;
  episode_id: string;
  start_seconds: number;
  end_seconds: number;
  text: string;
  note?: string | null;
  created_at: string;
  episode_title?: string | null;
  podcast_title?: string | null;
  podcast_image?: string | null;
}

export const listBookmarks = (episodeId?: string) =>
  req<Bookmark[]>("GET", episodeId ? `/bookmarks?episode_id=${episodeId}` : "/bookmarks");

/** From the player: the server finds the sentence around `seconds`. */
export const bookmarkMoment = (episodeId: string, seconds: number) =>
  req<Bookmark>("POST", "/bookmarks", { episode_id: episodeId, seconds });

/** From a transcript line, which already knows exactly which words it means. */
export const bookmarkLine = (
  episodeId: string, startSeconds: number, endSeconds: number, text: string,
) => req<Bookmark>("POST", "/bookmarks", {
  episode_id: episodeId, start_seconds: startSeconds, end_seconds: endSeconds, text,
});

export const annotateBookmark = (id: string, note: string) =>
  req<Bookmark>("PATCH", `/bookmarks/${id}`, { note });

export const deleteBookmark = (id: string) => req("DELETE", `/bookmarks/${id}`);

// --- Playlists ---
export interface PlaylistRules {
  unplayed: boolean;
  status?: string | null;
  tag_id?: string | null;
  podcast_id?: string | null;
  min_minutes?: number | null;
  max_minutes?: number | null;
  sort: FeedSort;
  limit: number;
}

export const EMPTY_RULES: PlaylistRules = {
  unplayed: false, status: null, tag_id: null, podcast_id: null,
  min_minutes: null, max_minutes: null, sort: "newest", limit: 50,
};

export interface Playlist {
  id: string;
  name: string;
  kind: "manual" | "smart";
  rules?: PlaylistRules | null;
  created_at: string;
  episode_count: number;
  images: string[];
}

export const listPlaylists = () => req<Playlist[]>("GET", "/playlists");

export const createPlaylist = (name: string, kind: "manual" | "smart", rules?: PlaylistRules) =>
  req<Playlist>("POST", "/playlists", { name, kind, rules });

export const getPlaylist = (id: string) =>
  req<{ playlist: Playlist; episodes: FeedEpisode[] }>("GET", `/playlists/${id}`);

export const updatePlaylist = (id: string, patch: { name?: string; rules?: PlaylistRules }) =>
  req<Playlist>("PATCH", `/playlists/${id}`, patch);

export const deletePlaylist = (id: string) => req("DELETE", `/playlists/${id}`);

export const addToPlaylist = (id: string, episodeId: string) =>
  req<{ status: string }>("POST", `/playlists/${id}/episodes/${episodeId}`);

export const removeFromPlaylist = (id: string, episodeId: string) =>
  req("DELETE", `/playlists/${id}/episodes/${episodeId}`);

export const reorderPlaylist = (id: string, episodeIds: string[]) =>
  req<{ status: string }>("PUT", `/playlists/${id}/episodes`, { episode_ids: episodeIds });

export const queuePlaylist = (id: string, replace = false) =>
  req<QueueRow[]>("POST", `/playlists/${id}/queue?replace=${replace}`);

// --- Inbox ---
// "Like email but for podcasts" needs something tracking the read line, and it
// has to be server-side or "new" means new to this browser.
export const getInbox = () =>
  req<{ new: number; since: string | null }>("GET", "/podcasts/inbox");

export const markInboxSeen = () =>
  req<{ new: number; since: string }>("POST", "/podcasts/inbox/seen");

// --- Per-podcast playback settings ---
// Every field nullable, and null means "no opinion" rather than a stored
// default: the player keeps whatever it was last set to.
export interface PodcastSettings {
  playback_rate?: number | null;
  skip_intro?: number | null;
  skip_outro?: number | null;
  prefer_adfree?: boolean | null;
  auto_transcribe?: boolean | null;
}

export const setPodcastSettings = (podcastId: string, settings: PodcastSettings) =>
  req<PodcastSettings>("PUT", `/podcasts/${podcastId}/settings`, settings);

// --- OPML ---
/** A download, because the consumer is another podcast app, not this one. */
export const opmlExportUrl = () => `${BASE}/podcasts/opml`;

export const importOpml = (xml: string) =>
  req<{ added: number; skipped: number; found: number; titles: string[] }>(
    "POST", "/podcasts/opml", { xml },
  );

// --- Storage ---
export interface StorageUsage {
  total_bytes: number;
  audio_bytes: number;
  episodes: number;
  orphan_bytes: number;
  orphan_files: number;
  by_podcast: { podcast_id: string; title: string; bytes: number; episodes: number }[];
  policy: { days: number; played_only: boolean };
}

export interface PruneResult {
  status: "disabled" | "dry_run" | "pruned";
  freed_bytes: number;
  episodes: number;
  orphans: number;
  cleared: { episode_id: string; title: string; bytes: number }[];
}

export const getStorage = () => req<StorageUsage>("GET", "/storage");

export const setRetentionPolicy = (days: number, playedOnly: boolean) =>
  req<{ days: number; played_only: boolean }>("PUT", "/storage/policy",
    { days, played_only: playedOnly });

export const pruneStorage = (dryRun: boolean, days?: number) =>
  req<PruneResult>("POST",
    `/storage/prune?dry_run=${dryRun}${days != null ? `&days=${days}` : ""}`);

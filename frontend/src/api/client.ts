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
  limit?: number;
}

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
  req<{ audio_url: string; transcript_status: string }>(
    "POST", "/player/play", { episode_id: episodeId, audio_url: audioUrl }
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
  episode_id: string;
  podcast_id: string;
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

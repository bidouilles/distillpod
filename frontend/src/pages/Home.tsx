import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { getFeed, getTags, getProgress, refreshSubscriptions, getRefreshStatus, FeedEpisode, Tag, ProgressRecord } from "../api/client";
import ContinueListening from "../components/ContinueListening";
import { getCached, setCached } from "../cache";
import FeedFilterBar, { FeedFilterState, EMPTY_FILTERS, hasActiveFilters } from "../components/FeedFilterBar";

const FEED_CACHE_KEY = "home:feed";
const SHOTS_CACHE_KEY = "home:shotCounts";

// ─── Listened state (localStorage) ───────────────────────────────────────────
const STORAGE_KEY = "distillpod:played";

function getPlayed(): Set<string> {
  try { return new Set(JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]")); }
  catch { return new Set(); }
}

function togglePlayed(id: string): Set<string> {
  const played = getPlayed();
  played.has(id) ? played.delete(id) : played.add(id);
  localStorage.setItem(STORAGE_KEY, JSON.stringify([...played]));
  return played;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function fmtDate(iso?: string) {
  if (!iso) return null;
  const d = new Date(iso);
  const now = new Date();
  const diffDays = Math.floor((now.getTime() - d.getTime()) / 86400000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays}d ago`;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function fmtDuration(secs?: number | null) {
  if (!secs) return null;
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// ─── Skeleton card ────────────────────────────────────────────────────────────
function SkeletonCard() {
  return (
    <div className="bg-gray-900 rounded-xl p-4 flex gap-3 animate-pulse">
      <div className="w-12 h-12 rounded-lg bg-gray-800 flex-shrink-0" />
      <div className="flex-1 space-y-2">
        <div className="h-3 bg-gray-800 rounded w-1/3" />
        <div className="h-4 bg-gray-800 rounded w-full" />
        <div className="h-4 bg-gray-800 rounded w-3/4" />
        <div className="h-3 bg-gray-800 rounded w-1/4" />
      </div>
    </div>
  );
}

// ─── Episode card ─────────────────────────────────────────────────────────────
function EpisodeCard({
  ep, shotCount, played, onTogglePlayed,
}: {
  ep: FeedEpisode;
  shotCount: number;
  played: boolean;
  onTogglePlayed: () => void;
}) {
  const nav = useNavigate();

  return (
    <div
      className={`bg-gray-900 rounded-xl p-4 flex gap-3 transition-opacity relative ${played ? "opacity-60" : ""}`}
    >
      {ep.ads_detected != null && ep.ads_detected > 0 && (
        <span title='Ad-free version available' className='absolute top-1 right-1 text-xs bg-gray-800 rounded px-1'>✂️</span>
      )}
      {/* Podcast art */}
      <div className="flex-shrink-0">
        {ep.podcast_image
          ? <img src={ep.podcast_image} className="w-12 h-12 rounded-lg object-cover" alt="" />
          : <div className="w-12 h-12 rounded-lg bg-gray-800 flex items-center justify-center text-xl">🎙</div>
        }
      </div>

      {/* Content */}
      <div
        className="flex-1 min-w-0 cursor-pointer"
        onClick={() => nav(`/player/${ep.id}`, { state: ep })}
      >
        <div className="text-xs text-gray-500 mb-0.5 truncate">{ep.podcast_title}</div>
        <div className="text-sm font-medium leading-snug line-clamp-2">{ep.title}</div>

        {/* Meta row */}
        <div className="flex items-center gap-2 mt-1.5 flex-wrap">
          {fmtDate(ep.published_at) && (
            <span className="text-xs text-gray-500">{fmtDate(ep.published_at)}</span>
          )}
          {fmtDuration(ep.duration_seconds) && (
            <span className="text-xs text-gray-600">· {fmtDuration(ep.duration_seconds)}</span>
          )}
          {shotCount > 0 && (
            <span className="text-xs bg-indigo-900 text-indigo-300 px-1.5 py-0.5 rounded-full font-medium">
              ⚗️ {shotCount}
            </span>
          )}
        </div>
      </div>

      {/* Listened toggle */}
      <button
        onClick={e => { e.stopPropagation(); onTogglePlayed(); }}
        className="flex-shrink-0 self-center ml-1"
        title={played ? "Mark as unplayed" : "Mark as played"}
      >
        {played ? (
          <div className="w-6 h-6 rounded-full bg-indigo-500 flex items-center justify-center">
            <svg viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" className="w-3.5 h-3.5">
              <polyline points="20 6 9 17 4 12" />
            </svg>
          </div>
        ) : (
          <div className="w-6 h-6 rounded-full border-2 border-gray-600 hover:border-gray-400 transition-colors" />
        )}
      </button>
    </div>
  );
}

// ─── Home page ────────────────────────────────────────────────────────────────
export default function Home() {
  const nav = useNavigate();
  const [feed, setFeed] = useState<FeedEpisode[]>(() => getCached<FeedEpisode[]>(FEED_CACHE_KEY) || []);
  const [shotCounts, setShotCounts] = useState<Record<string, number>>(() => getCached<Record<string, number>>(SHOTS_CACHE_KEY) || {});
  const [played, setPlayed] = useState<Set<string>>(getPlayed());
  const [loading, setLoading] = useState(() => !getCached(FEED_CACHE_KEY)); // skip spinner if cache hit
  const [refreshing, setRefreshing] = useState(false);
  const [noSubs, setNoSubs] = useState(false);
  const [filters, setFilters] = useState<FeedFilterState>(EMPTY_FILTERS);
  const [tags, setTags] = useState<Tag[]>([]);
  const [progress, setProgress] = useState<ProgressRecord[]>([]);
  const [toast, setToast] = useState("");
  const refreshPoll = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

  const filtering = hasActiveFilters(filters);

  const fetchFeed = async (showRefreshing = false, f: FeedFilterState = filters) => {
    if (showRefreshing) setRefreshing(true);
    try {
      const episodes = await getFeed({
        q: f.q.trim(),
        tag_id: f.tagId,
        status: f.status,
        // A filtered view should search the whole library, not just the most
        // recent page, or a match from last year would silently not exist.
        limit: hasActiveFilters(f) ? 200 : 50,
      });

      const counts: Record<string, number> = {};
      episodes.forEach(ep => { counts[ep.id] = ep.distill_count; });
      setFeed(episodes);
      setShotCounts(counts);

      // Only the unfiltered feed is worth caching, and an empty result there
      // genuinely means "no subscriptions" — under a filter it means "no match".
      if (!hasActiveFilters(f)) {
        setNoSubs(episodes.length === 0);
        setCached(FEED_CACHE_KEY, episodes);
        setCached(SHOTS_CACHE_KEY, counts);
      }
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  // The feed itself is a local query, so re-reading it looked instant and
  // changed nothing — new episodes only appeared after the nightly job. This
  // asks the subscriptions, which is what the control implies.
  //
  // The work runs server-side and is polled rather than awaited, so leaving
  // Home does not abandon it: coming back picks the run up again and still
  // reports what it found.
  const watchRefresh = () => {
    if (refreshPoll.current) clearInterval(refreshPoll.current);
    refreshPoll.current = setInterval(async () => {
      try {
        const s = await getRefreshStatus();
        if (s.running) return;
        if (refreshPoll.current) clearInterval(refreshPoll.current);
        setRefreshing(false);
        await fetchFeed();
        setToast(
          s.new > 0
            ? `${s.new} new episode${s.new === 1 ? "" : "s"}`
            : s.failed && !s.checked ? "Could not reach your subscriptions"
            : "You're up to date",
        );
        setTimeout(() => setToast(""), 3000);
      } catch { /* transient — keep watching */ }
    }, 2000);
  };

  const refreshNow = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setToast("");
    try {
      await refreshSubscriptions();
      watchRefresh();
    } catch {
      setRefreshing(false);
      setToast("Refresh failed");
      setTimeout(() => setToast(""), 3000);
    }
  };

  // Rejoin a refresh that was started before this screen was opened.
  useEffect(() => {
    getRefreshStatus()
      .then(s => { if (s.running) { setRefreshing(true); watchRefresh(); } })
      .catch(() => {});
    return () => { if (refreshPoll.current) clearInterval(refreshPoll.current); };
  }, []);

  useEffect(() => { fetchFeed(); }, []);

  useEffect(() => { getTags().then(setTags).catch(() => {}); }, []);

  // Playback state lives on the server, so read it from there rather than
  // trusting this device's localStorage: an episode finished on the phone has
  // to count as played here too, or the "unplayed" filter quietly lies.
  useEffect(() => {
    getProgress()
      .then(records => {
        setProgress(records);
        setPlayed(prev => {
          const merged = new Set(prev);
          records.forEach(r => { if (r.played) merged.add(r.episode_id); });
          return merged;
        });
      })
      .catch(() => {});
  }, []);

  const applyFilters = (f: FeedFilterState) => {
    setFilters(f);
    setLoading(true);
    fetchFeed(false, f);
  };

  // Played state is local-only, so this last hop cannot be done server-side.
  const visible = filters.unplayedOnly ? feed.filter(ep => !played.has(ep.id)) : feed;

  const handleTogglePlayed = (id: string) => {
    setPlayed(togglePlayed(id));
  };

  // ── Empty: no subscriptions ──
  if (!loading && noSubs) return (
    <div className="text-center py-16 space-y-4">
      <div className="text-5xl">🎙</div>
      <p className="text-gray-300 font-medium">No subscriptions yet</p>
      <p className="text-gray-500 text-sm">Search for podcasts to fill your feed.</p>
      <button
        onClick={() => nav("/search")}
        className="mt-2 bg-indigo-600 hover:bg-indigo-500 text-white px-5 py-2.5 rounded-xl font-medium text-sm"
      >
        Find podcasts
      </button>
    </div>
  );

  return (
    <div className="space-y-3">
      {toast && (
        <div className="fixed top-4 left-1/2 -translate-x-1/2 bg-gray-800 text-white px-4 py-2 rounded-full shadow-lg z-50 text-sm border border-gray-700">
          {toast}
        </div>
      )}

      <ContinueListening records={progress} />

      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">Latest Episodes</h1>
        <button
          onClick={refreshNow}
          disabled={refreshing}
          className="text-gray-400 hover:text-white disabled:opacity-40 transition-colors p-1"
          title="Refresh"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"
            className={`w-5 h-5 ${refreshing ? "animate-spin" : ""}`}>
            <polyline points="23 4 23 10 17 10" />
            <polyline points="1 20 1 14 7 14" />
            <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" />
          </svg>
        </button>
      </div>

      <FeedFilterBar
        filters={filters}
        onChange={applyFilters}
        tags={tags}
        resultCount={visible.length}
        loading={loading}
      />

      {loading && [...Array(5)].map((_, i) => <SkeletonCard key={i} />)}

      {!loading && visible.length === 0 && filtering && (
        <div className="text-center py-12 space-y-3">
          <div className="text-4xl">🔍</div>
          <p className="text-gray-400 text-sm">No episodes match these filters.</p>
          <button
            onClick={() => applyFilters(EMPTY_FILTERS)}
            className="text-indigo-400 hover:text-indigo-300 text-sm min-h-[44px] px-3"
          >
            Clear filters
          </button>
        </div>
      )}

      {!loading && visible.map(ep => (
        <EpisodeCard
          key={ep.id}
          ep={ep}
          shotCount={shotCounts[ep.id] || 0}
          played={played.has(ep.id)}
          onTogglePlayed={() => handleTogglePlayed(ep.id)}
        />
      ))}
    </div>
  );
}

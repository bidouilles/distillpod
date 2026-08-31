import { useState, useEffect } from "react";
import { getCached, setCached } from "./cache";
import { getInbox, getSubscriptions, getEpisodes } from "./api/client";
import { useQueue } from "./stores/queueStore";
import { BrowserRouter, Routes, Route, Navigate, useLocation, useNavigate } from "react-router-dom";
import { AudioProvider, useAudio } from "./context/AudioContext";
import MiniPlayer from "./components/MiniPlayer";
import FullscreenPlayer from "./components/FullscreenPlayer";
import Home from "./pages/Home";
import Search from "./pages/Search";
import Queue from './pages/Queue';
import Library from "./pages/Library";
import { PodcastEpisodes } from "./pages/Subscriptions";
import PlaylistDetail from "./pages/PlaylistDetail";
import Player from "./pages/Player";
import Saved from "./pages/Saved";
import Chat from "./pages/Chat";
import Login from "./pages/Login";
import Unauthorized from "./pages/Unauthorized";

// ─── Icons ────────────────────────────────────────────────────────────────────
const HomeIcon = ({ active }: { active: boolean }) => (
  <svg viewBox="0 0 24 24" fill={active ? "currentColor" : "none"} stroke="currentColor" strokeWidth={active ? 0 : 2} strokeLinecap="round" strokeLinejoin="round" className="w-6 h-6">
    <path d="M3 9.75L12 3l9 6.75V21a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V9.75z" />
    <rect x="9" y="12" width="6" height="10" fill={active ? "white" : "none"} opacity={active ? 0.3 : 0} />
  </svg>
);

const SearchIcon = ({ active }: { active: boolean }) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={active ? 2.5 : 2} strokeLinecap="round" strokeLinejoin="round" className="w-6 h-6">
    <circle cx="11" cy="11" r="7" />
    <line x1="21" y1="21" x2="16.65" y2="16.65" />
  </svg>
);

const LibraryIcon = ({ active }: { active: boolean }) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={active ? 2.5 : 2} strokeLinecap="round" strokeLinejoin="round" className="w-6 h-6">
    <path d="M3 18v-6a9 9 0 0 1 18 0v6" />
    <path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z" />
  </svg>
);

const SavedIcon = ({ active }: { active: boolean }) => (
  <svg viewBox="0 0 24 24" fill={active ? "currentColor" : "none"} stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className="w-6 h-6">
    <path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z" />
  </svg>
);

const QueueIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className="w-5 h-5">
    <line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" />
    <line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" />
  </svg>
);

// ─── Bottom nav ───────────────────────────────────────────────────────────────
// Four tabs rather than five. Up Next moved to the header, where it is visible
// from every screen instead of only when the tab bar is looked at — and where
// it can carry a count. That buys the remaining four bigger touch targets, and
// makes room for the Library to become a place with sections rather than one
// list of shows.
const tabs = [
  { to: "/",        label: "Home",    Icon: HomeIcon    },
  { to: "/search",  label: "Search",  Icon: SearchIcon  },
  { to: "/library", label: "Library", Icon: LibraryIcon },
  { to: "/saved",   label: "Saved",   Icon: SavedIcon   },
];

function BottomNav() {
  const location = useLocation();
  const navigate = useNavigate();
  const isActive = (to: string) => {
    if (to === "/") return location.pathname === "/";
    if (to === "/library") {
      // A podcast's own page and a playlist are both places inside the library.
      return ["/library", "/subscriptions"].some(p => location.pathname.startsWith(p));
    }
    return location.pathname.startsWith(to);
  };

  return (
    <nav
      className="fixed bottom-0 left-0 right-0 bg-gray-900 border-t border-gray-800 flex z-50"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      {tabs.map(({ to, label, Icon }) => {
        const active = isActive(to);
        return (
          <button
            key={to}
            onClick={() => navigate(to)}
            aria-current={active ? "page" : undefined}
            className={`relative flex-1 flex flex-col items-center justify-center gap-1 py-2.5 transition-colors ${
              active ? "text-indigo-400" : "text-gray-500 hover:text-gray-300"
            }`}
          >
            <Icon active={active} />
            <span className={`text-xs font-medium ${active ? "text-indigo-400" : "text-gray-500"}`}>
              {label}
            </span>
            {active && (
              <span className="absolute top-0 left-1/2 -translate-x-1/2 w-8 h-0.5 bg-indigo-400 rounded-b-full" />
            )}
          </button>
        );
      })}
    </nav>
  );
}

// ─── Header ───────────────────────────────────────────────────────────────────
function Header() {
  const navigate = useNavigate();
  const location = useLocation();
  const { queue } = useQueue();
  const [inbox, setInbox] = useState(0);

  // Read once per app load, and again whenever the feed is returned to: a
  // refresh started on Home is the usual reason this number changes.
  useEffect(() => {
    getInbox().then(r => setInbox(r.new)).catch(() => {});
  }, [location.pathname === "/"]);

  const onQueue = location.pathname.startsWith("/up-next");

  return (
    <header
      className="sticky top-0 z-40 bg-gray-900 border-b border-gray-800 px-4 py-3 flex items-center gap-2"
      style={{ paddingTop: "max(0.75rem, env(safe-area-inset-top))" }}
    >
      <button onClick={() => navigate("/")} className="font-bold text-indigo-400 text-lg tracking-tight">
        ⚗️ DistillPod
      </button>

      <div className="flex-1" />

      {inbox > 0 && !onQueue && (
        <button
          onClick={() => navigate("/")}
          className="text-xs font-semibold bg-indigo-600/20 text-indigo-300 hover:bg-indigo-600/30 rounded-full px-2.5 min-h-[32px] transition-colors"
        >
          {inbox} new
        </button>
      )}

      <button
        onClick={() => navigate(onQueue ? "/" : "/up-next")}
        aria-label={`Up Next (${queue.length})`}
        className={`relative w-10 h-10 flex items-center justify-center rounded-full transition-colors ${
          onQueue ? "text-indigo-400 bg-indigo-600/15" : "text-gray-400 hover:text-white hover:bg-gray-800"
        }`}
      >
        <QueueIcon />
        {queue.length > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[18px] h-[18px] px-1 rounded-full bg-indigo-500 text-white text-[10px] font-bold flex items-center justify-center">
            {queue.length > 99 ? "99+" : queue.length}
          </span>
        )}
      </button>
    </header>
  );
}

// ─── App shell (needs useAudio → must be inside AudioProvider) ────────────────
function AppShell() {
  const { audioReady } = useAudio();
  const hydrateQueue = useQueue(s => s.hydrate);

  // Reconcile the queue with the server once, at startup — same contract as
  // playback positions. Until it lands, the local mirror is what renders.
  useEffect(() => { hydrateQueue(); }, [hydrateQueue]);

  // Prefetch all subscribed podcast episodes on mount (warm the cache silently)
  useEffect(() => {
    getSubscriptions().then(subs => {
      subs.forEach(sub => {
        const key = `episodes:${sub.podcast_id}`;
        if (!getCached(key)) {
          getEpisodes(sub.podcast_id).then(eps => setCached(key, eps)).catch(() => {});
        }
      });
    }).catch(() => {});
  }, []);

  return (
    <div className="min-h-screen bg-gray-950 text-white flex flex-col">
      <Header />

      {/* Extra bottom padding when mini player is visible */}
      <main className={`flex-1 p-4 max-w-3xl mx-auto w-full transition-[padding] ${
        audioReady ? "pb-36" : "pb-24"
      }`}>
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/search" element={<Search />} />
          <Route path="/library" element={<Library />} />
          {/* Kept so links, bookmarks and back buttons from before the Library
              gained sections still land somewhere sensible. */}
          <Route path="/subscriptions" element={<Navigate to="/library" replace />} />
          <Route path="/subscriptions/:podcastId" element={<PodcastEpisodes />} />
          <Route path="/library/playlists/:playlistId" element={<PlaylistDetail />} />
          <Route path="/player/:episodeId" element={<Player />} />
          <Route path="/player/:episodeId/chat" element={<Chat />} />
          {/* "/up-next", not "/queue": the API owns /queue, and a hard reload
              of an SPA path that an API route also matches serves JSON. Same
              reason playlists live under /library. */}
          <Route path="/up-next" element={<Queue />} />
          <Route path="/queue" element={<Navigate to="/up-next" replace />} />
          <Route path="/saved" element={<Saved />} />
          <Route path="/gists" element={<Navigate to="/saved" replace />} />
          <Route path="*" element={<Home />} />
        </Routes>
      </main>

      <MiniPlayer />
      <FullscreenPlayer />
      <BottomNav />
    </div>
  );
}

// ─── Root ─────────────────────────────────────────────────────────────────────
interface User { email: string; name: string; picture: string; }

export default function App() {
  const [user, setUser] = useState<User | null | "loading">("loading");

  useEffect(() => {
    fetch("/auth/me", { credentials: "include" })
      .then(r => (r.ok ? r.json() : null))
      .then(u => setUser(u))
      .catch(() => setUser(null));
  }, []);

  if (user === "loading") {
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center">
        <span className="text-4xl animate-pulse select-none">⚗️</span>
      </div>
    );
  }

  if (!user) {
    if (window.location.pathname === "/unauthorized") return <Unauthorized />;
    return <Login />;
  }

  return (
    <BrowserRouter>
      <AudioProvider>
        <AppShell />
      </AudioProvider>
    </BrowserRouter>
  );
}

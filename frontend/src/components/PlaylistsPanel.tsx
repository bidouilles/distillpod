import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  createPlaylist, listPlaylists, EMPTY_RULES,
  type Playlist, type PlaylistRules,
} from "../api/client";
import PlaylistRuleEditor from "./PlaylistRuleEditor";

/**
 * Playlists, both kinds, in one list.
 *
 * A manual playlist is a queue you keep; a smart one is a saved question. The
 * card says which, and a smart one's count is resolved when the list is read
 * rather than stored — "Quick listen (7)" has to be true now, or the whole
 * feature is decoration.
 */
export default function PlaylistsPanel() {
  const nav = useNavigate();
  const [playlists, setPlaylists] = useState<Playlist[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState<null | "manual" | "smart">(null);
  const [name, setName] = useState("");
  const [rules, setRules] = useState<PlaylistRules>(EMPTY_RULES);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = () => {
    listPlaylists().then(setPlaylists).catch(() => {}).finally(() => setLoading(false));
  };
  useEffect(load, []);

  const startCreating = (kind: "manual" | "smart") => {
    setCreating(kind);
    setName(kind === "smart" ? "Quick listen" : "Listen later");
    setRules(kind === "smart" ? { ...EMPTY_RULES, unplayed: true, max_minutes: 25 } : EMPTY_RULES);
    setError("");
  };

  const submit = async () => {
    if (!creating || !name.trim() || busy) return;
    setBusy(true);
    try {
      const created = await createPlaylist(
        name.trim(), creating, creating === "smart" ? rules : undefined);
      setCreating(null);
      load();
      nav(`/library/playlists/${created.id}`);
    } catch {
      setError("Could not create that playlist");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      {loading && [...Array(2)].map((_, i) => (
        <div key={i} className="bg-gray-900 rounded-2xl h-20 animate-pulse" />
      ))}

      {!loading && playlists.length === 0 && !creating && (
        <div className="text-center py-10 text-gray-500">
          <div className="text-4xl mb-3">🗂</div>
          <p className="text-gray-300 font-medium">No playlists yet</p>
          <p className="text-sm mt-1 max-w-xs mx-auto">
            Keep a list by hand, or set a rule and let it fill itself — “unplayed,
            under 25 minutes” is the useful one.
          </p>
        </div>
      )}

      {playlists.map(p => (
        <button
          key={p.id}
          onClick={() => nav(`/library/playlists/${p.id}`)}
          className="w-full bg-gray-900 hover:bg-gray-800 active:bg-gray-700 rounded-2xl p-3 flex gap-3 items-center text-left transition-colors"
        >
          <Covers images={p.images} smart={p.kind === "smart"} />
          <div className="flex-1 min-w-0">
            <div className="font-medium leading-snug truncate">{p.name}</div>
            <div className="text-xs text-gray-500 mt-0.5 flex items-center gap-1.5">
              <span className={`px-1.5 rounded-full text-[10px] font-semibold ${
                p.kind === "smart"
                  ? "bg-indigo-600/20 text-indigo-300"
                  : "bg-gray-800 text-gray-400"
              }`}>
                {p.kind === "smart" ? "SMART" : "MANUAL"}
              </span>
              <span>
                {p.episode_count} episode{p.episode_count === 1 ? "" : "s"}
              </span>
            </div>
          </div>
          <span className="text-gray-600 text-lg">›</span>
        </button>
      ))}

      {/* Create */}
      {!creating ? (
        <div className="flex gap-2">
          <button
            onClick={() => startCreating("manual")}
            className="flex-1 min-h-[44px] rounded-xl bg-gray-800 hover:bg-gray-700 text-sm text-gray-200 font-medium transition-colors"
          >
            + Playlist
          </button>
          <button
            onClick={() => startCreating("smart")}
            className="flex-1 min-h-[44px] rounded-xl bg-indigo-600/20 hover:bg-indigo-600/30 text-sm text-indigo-300 font-medium transition-colors"
          >
            + Smart playlist
          </button>
        </div>
      ) : (
        <div className="bg-gray-900 rounded-2xl px-4 py-4 space-y-4">
          <div>
            <div className="text-xs text-gray-500 uppercase tracking-wider font-semibold mb-1.5">
              {creating === "smart" ? "New smart playlist" : "New playlist"}
            </div>
            <input
              value={name}
              onChange={e => setName(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter") submit(); }}
              autoFocus
              maxLength={60}
              placeholder="Name"
              className="w-full bg-gray-800 text-white placeholder-gray-500 rounded-xl px-3 min-h-[44px] text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>

          {creating === "smart" && (
            <PlaylistRuleEditor rules={rules} onChange={setRules} />
          )}

          {error && <p className="text-xs text-red-400">{error}</p>}

          <div className="flex gap-2">
            <button
              onClick={submit}
              disabled={busy || !name.trim()}
              className="flex-1 min-h-[44px] rounded-xl bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white text-sm font-semibold"
            >
              {busy ? "Creating…" : "Create"}
            </button>
            <button
              onClick={() => setCreating(null)}
              className="min-h-[44px] px-4 rounded-xl bg-gray-800 hover:bg-gray-700 text-sm text-gray-300"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** A stack of covers, so a playlist is recognisable before it is read. */
function Covers({ images, smart }: { images: string[]; smart: boolean }) {
  if (images.length === 0) {
    return (
      <div className={`w-14 h-14 rounded-lg flex items-center justify-center text-xl flex-shrink-0 ${
        smart ? "bg-indigo-600/15" : "bg-gray-800"
      }`}>
        {smart ? "✨" : "🗂"}
      </div>
    );
  }
  if (images.length === 1) {
    return <img src={images[0]} className="w-14 h-14 rounded-lg object-cover flex-shrink-0" alt="" />;
  }
  return (
    <div className="w-14 h-14 rounded-lg overflow-hidden grid grid-cols-2 grid-rows-2 flex-shrink-0 bg-gray-800">
      {images.slice(0, 4).map((src, i) => (
        <img key={i} src={src} className="w-full h-full object-cover" alt="" />
      ))}
    </div>
  );
}

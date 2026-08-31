import { useEffect, useState } from "react";
import { addToPlaylist, createPlaylist, listPlaylists, type Playlist } from "../api/client";

/**
 * Put this episode in a playlist.
 *
 * Only manual playlists are offered: a smart playlist chooses its own
 * episodes, and letting one be added to by hand would make its rule a lie the
 * next time it was read.
 */
export default function AddToPlaylist({ episodeId }: { episodeId: string }) {
  const [open, setOpen] = useState(false);
  const [playlists, setPlaylists] = useState<Playlist[]>([]);
  const [added, setAdded] = useState<Set<string>>(new Set());
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    listPlaylists()
      .then(all => setPlaylists(all.filter(p => p.kind === "manual")))
      .catch(() => setError("Could not load your playlists"));
  }, [open]);

  const add = async (playlist: Playlist) => {
    setAdded(prev => new Set(prev).add(playlist.id));
    try { await addToPlaylist(playlist.id, episodeId); }
    catch {
      setAdded(prev => { const n = new Set(prev); n.delete(playlist.id); return n; });
      setError("Could not add it");
    }
  };

  const createAndAdd = async () => {
    if (!name.trim()) return;
    try {
      const created = await createPlaylist(name.trim(), "manual");
      setPlaylists(prev => [...prev, created]);
      setName("");
      setCreating(false);
      await add(created);
    } catch {
      setError("Could not create that playlist");
    }
  };

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="w-full min-h-[44px] rounded-xl bg-gray-800 hover:bg-gray-700 text-sm text-gray-300 font-medium transition-colors"
      >
        🗂 Add to playlist
      </button>
    );
  }

  return (
    <div className="bg-gray-900 rounded-2xl px-4 py-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
          Add to playlist
        </span>
        <button onClick={() => setOpen(false)} className="text-gray-500 hover:text-white w-8 h-8">✕</button>
      </div>

      {playlists.length === 0 && !creating && (
        <p className="text-xs text-gray-500 py-1">No playlists yet.</p>
      )}

      {playlists.map(p => (
        <button
          key={p.id}
          onClick={() => add(p)}
          disabled={added.has(p.id)}
          className="w-full flex items-center justify-between gap-2 min-h-[40px] px-3 rounded-xl bg-gray-800 hover:bg-gray-700 disabled:opacity-60 text-sm text-gray-200 transition-colors"
        >
          <span className="truncate">{p.name}</span>
          <span className="text-xs flex-shrink-0">
            {added.has(p.id) ? "✓ added" : "+"}
          </span>
        </button>
      ))}

      {creating ? (
        <div className="flex gap-2">
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            onKeyDown={e => { if (e.key === "Enter") createAndAdd(); }}
            autoFocus
            maxLength={60}
            placeholder="Playlist name"
            className="flex-1 bg-gray-800 text-white placeholder-gray-500 rounded-xl px-3 min-h-[40px] text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
          <button
            onClick={createAndAdd}
            className="min-h-[40px] px-3 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-semibold"
          >
            Add
          </button>
        </div>
      ) : (
        <button
          onClick={() => setCreating(true)}
          className="w-full min-h-[40px] rounded-xl bg-gray-800/60 hover:bg-gray-700 text-xs text-gray-400 transition-colors"
        >
          + New playlist
        </button>
      )}

      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  );
}

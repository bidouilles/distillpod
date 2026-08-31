import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  deletePlaylist, getPlaylist, queuePlaylist, removeFromPlaylist,
  reorderPlaylist, updatePlaylist,
  type FeedEpisode, type Playlist, type PlaylistRules,
} from "../api/client";
import { useQueue } from "../stores/queueStore";
import EpisodeRow from "../components/EpisodeRow";
import PlaylistRuleEditor from "../components/PlaylistRuleEditor";

/**
 * One playlist, and the two things anyone came here to do: see what is in it,
 * and play it.
 *
 * A smart playlist shows its rule and can be re-tuned in place, because the
 * rule *is* its content — editing it is the equivalent of adding an episode to
 * a manual one.
 */
export default function PlaylistDetail() {
  const { playlistId } = useParams<{ playlistId: string }>();
  const nav = useNavigate();
  const hydrateQueue = useQueue(s => s.hydrate);

  const [playlist, setPlaylist] = useState<Playlist | null>(null);
  const [episodes, setEpisodes] = useState<FeedEpisode[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");
  const [rules, setRules] = useState<PlaylistRules | null>(null);
  const [toast, setToast] = useState("");

  const load = () => {
    if (!playlistId) return;
    getPlaylist(playlistId)
      .then(r => {
        setPlaylist(r.playlist);
        setEpisodes(r.episodes);
        setName(r.playlist.name);
        setRules(r.playlist.rules ?? null);
      })
      .catch(() => setPlaylist(null))
      .finally(() => setLoading(false));
  };

  useEffect(load, [playlistId]);

  const flash = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast(""), 2200);
  };

  const play = async (replace: boolean) => {
    if (!playlistId) return;
    try {
      await queuePlaylist(playlistId, replace);
      // The store owns the queue everywhere else, so let it re-read rather than
      // holding a second copy of the same list.
      await hydrateQueue();
      flash(replace ? "Queue replaced" : `Added ${episodes.length} to Up Next`);
    } catch {
      flash("Could not queue that");
    }
  };

  const saveEdits = async () => {
    if (!playlistId || !playlist) return;
    try {
      await updatePlaylist(playlistId, {
        name: name.trim() || playlist.name,
        ...(playlist.kind === "smart" && rules ? { rules } : {}),
      });
      setEditing(false);
      load();
    } catch {
      flash("Could not save that");
    }
  };

  const drop = async (episodeId: string) => {
    if (!playlistId) return;
    setEpisodes(prev => prev.filter(e => e.id !== episodeId));
    try { await removeFromPlaylist(playlistId, episodeId); }
    catch { load(); }
  };

  const move = async (from: number, to: number) => {
    if (!playlistId || to < 0 || to >= episodes.length) return;
    const next = [...episodes];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    setEpisodes(next);
    try { await reorderPlaylist(playlistId, next.map(e => e.id)); }
    catch { load(); }
  };

  const destroy = async () => {
    if (!playlistId || !playlist) return;
    if (!confirm(`Delete “${playlist.name}”? The episodes themselves are not touched.`)) return;
    await deletePlaylist(playlistId);
    nav("/library?tab=playlists");
  };

  if (loading) {
    return <div className="space-y-3">
      {[...Array(4)].map((_, i) => <div key={i} className="bg-gray-900 rounded-xl h-16 animate-pulse" />)}
    </div>;
  }

  if (!playlist) {
    return (
      <div className="text-center py-16 space-y-3">
        <p className="text-gray-400">That playlist is gone.</p>
        <button onClick={() => nav("/library?tab=playlists")}
                className="text-indigo-400 hover:text-indigo-300 text-sm min-h-[44px] px-3">
          Back to playlists
        </button>
      </div>
    );
  }

  const smart = playlist.kind === "smart";

  return (
    <div className="space-y-4">
      {toast && (
        <div className="fixed top-4 left-1/2 -translate-x-1/2 bg-gray-800 text-white px-4 py-2 rounded-full shadow-lg z-50 text-sm border border-gray-700">
          {toast}
        </div>
      )}

      {/* Header */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => nav("/library?tab=playlists")}
          className="flex items-center gap-1.5 text-indigo-400 hover:text-indigo-300 py-2 pr-2 -ml-1"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5}
               strokeLinecap="round" strokeLinejoin="round" className="w-5 h-5">
            <polyline points="15 18 9 12 15 6" />
          </svg>
          <span className="text-sm font-medium">Playlists</span>
        </button>
        <div className="flex-1" />
        <button
          onClick={() => setEditing(e => !e)}
          className="text-xs text-gray-400 hover:text-white min-h-[36px] px-2"
        >
          {editing ? "Done" : "Edit"}
        </button>
        <button
          onClick={destroy}
          className="text-xs text-gray-500 hover:text-red-400 min-h-[36px] px-2"
        >
          Delete
        </button>
      </div>

      {editing ? (
        <div className="bg-gray-900 rounded-2xl px-4 py-4 space-y-4">
          <input
            value={name}
            onChange={e => setName(e.target.value)}
            maxLength={60}
            className="w-full bg-gray-800 text-white rounded-xl px-3 min-h-[44px] text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
          {smart && rules && <PlaylistRuleEditor rules={rules} onChange={setRules} />}
          <button
            onClick={saveEdits}
            className="w-full min-h-[44px] rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-semibold"
          >
            Save
          </button>
        </div>
      ) : (
        <div>
          <h1 className="text-xl font-bold leading-tight">{playlist.name}</h1>
          <div className="text-xs text-gray-500 mt-1 flex items-center gap-1.5">
            <span className={`px-1.5 rounded-full text-[10px] font-semibold ${
              smart ? "bg-indigo-600/20 text-indigo-300" : "bg-gray-800 text-gray-400"
            }`}>
              {smart ? "SMART" : "MANUAL"}
            </span>
            <span>{episodes.length} episode{episodes.length === 1 ? "" : "s"}</span>
          </div>
          {smart && rules && <RuleSummary rules={rules} />}
        </div>
      )}

      {/* Play */}
      {episodes.length > 0 && (
        <div className="flex gap-2">
          <button
            onClick={() => play(true)}
            className="flex-1 min-h-[44px] rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-semibold"
          >
            ▶ Play all
          </button>
          <button
            onClick={() => play(false)}
            className="flex-1 min-h-[44px] rounded-xl bg-gray-800 hover:bg-gray-700 text-sm text-gray-200 font-medium"
          >
            + Add to Up Next
          </button>
        </div>
      )}

      {/* Episodes */}
      {episodes.length === 0 ? (
        <div className="text-center py-12 text-gray-500 text-sm">
          {smart
            ? "Nothing matches this rule right now."
            : "Empty. Add episodes from an episode page."}
        </div>
      ) : (
        <div className="space-y-2">
          {episodes.map((ep, i) => (
            <EpisodeRow
              key={ep.id}
              ep={ep}
              index={smart ? undefined : i}
              right={!smart ? (
                <div className="flex flex-col gap-0.5 flex-shrink-0">
                  <button
                    onClick={() => move(i, i - 1)}
                    disabled={i === 0}
                    aria-label="Move up"
                    className="w-8 h-6 text-gray-500 hover:text-white disabled:opacity-25 leading-none"
                  >▲</button>
                  <button
                    onClick={() => move(i, i + 1)}
                    disabled={i === episodes.length - 1}
                    aria-label="Move down"
                    className="w-8 h-6 text-gray-500 hover:text-white disabled:opacity-25 leading-none"
                  >▼</button>
                  <button
                    onClick={() => drop(ep.id)}
                    aria-label="Remove from playlist"
                    className="w-8 h-6 text-gray-600 hover:text-red-400 leading-none"
                  >✕</button>
                </div>
              ) : undefined}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** The rule in words, so the card is not the only place it is legible. */
function RuleSummary({ rules }: { rules: PlaylistRules }) {
  const parts: string[] = [];
  if (rules.unplayed) parts.push("unplayed");
  if (rules.status) parts.push(rules.status);
  if (rules.min_minutes && rules.max_minutes) parts.push(`${rules.min_minutes}–${rules.max_minutes} min`);
  else if (rules.max_minutes) parts.push(`under ${rules.max_minutes} min`);
  else if (rules.min_minutes) parts.push(`over ${rules.min_minutes} min`);
  if (rules.sort !== "newest") parts.push(rules.sort);
  if (!parts.length) return null;
  return (
    <p className="text-xs text-gray-500 mt-1.5">
      Fills itself with: {parts.join(" · ")}
    </p>
  );
}

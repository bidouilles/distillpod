import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  annotateBookmark, deleteBookmark, listBookmarks, type Bookmark,
} from "../api/client";
import { CopyButton, ShareButton } from "./ActionButtons";

/**
 * Bookmarks, across the library or within one episode.
 *
 * A bookmark is what the listener reached for; a distillation is what the model
 * made of a moment. They are kept apart deliberately — six months later, which
 * of the two said a thing is the difference between a quote you can stand
 * behind and one you have to go and check.
 */

function fmtTime(secs: number) {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = Math.floor(secs % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

export default function BookmarkList({
  episodeId, compact = false, refreshKey = 0, heading,
}: {
  /** Omit for the whole library. */
  episodeId?: string;
  /** Inside an episode page: drop the show name, which is already the heading. */
  compact?: boolean;
  /** Bump to re-read after saving one elsewhere on the screen. */
  refreshKey?: number;
  /** Rendered only when there is something under it, so an episode with no
   *  bookmarks shows no empty section. */
  heading?: string;
}) {
  const [bookmarks, setBookmarks] = useState<Bookmark[]>([]);
  const [loading, setLoading] = useState(true);

  const load = () => {
    listBookmarks(episodeId)
      .then(setBookmarks)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(load, [episodeId, refreshKey]);

  if (loading) {
    return <div className="space-y-2">
      {[...Array(compact ? 1 : 3)].map((_, i) =>
        <div key={i} className="bg-gray-900 rounded-xl h-20 animate-pulse" />)}
    </div>;
  }

  if (bookmarks.length === 0) {
    if (compact) return null;
    return (
      <div className="text-center py-12 text-gray-500">
        <div className="text-4xl mb-3">🔖</div>
        <p className="text-gray-300 font-medium">No bookmarks yet</p>
        <p className="text-sm mt-1 max-w-xs mx-auto">
          While an episode plays, tap 🔖 to keep the sentence being spoken — or
          hold any line of the transcript.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {heading && (
        <h2 className="text-gray-500 font-semibold text-xs uppercase tracking-wider px-1">
          {heading}
        </h2>
      )}
      {bookmarks.map(b => (
        <BookmarkCard
          key={b.id}
          bookmark={b}
          compact={compact}
          onDeleted={() => setBookmarks(prev => prev.filter(x => x.id !== b.id))}
        />
      ))}
    </div>
  );
}

function BookmarkCard({ bookmark, compact, onDeleted }: {
  bookmark: Bookmark; compact: boolean; onDeleted: () => void;
}) {
  const nav = useNavigate();
  const [note, setNote] = useState(bookmark.note ?? "");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const text = () => `"${bookmark.text}"${note ? `\n\n${note}` : ""}` +
    `\n\n— ${bookmark.episode_title ?? ""} (${fmtTime(bookmark.start_seconds)})`;

  const saveNote = (value: string) => {
    setNote(value);
    clearTimeout(timer.current);
    timer.current = setTimeout(async () => {
      setSaving(true);
      try { await annotateBookmark(bookmark.id, value); } catch {}
      finally { setSaving(false); }
    }, 600);
  };

  const jump = () => nav(`/player/${bookmark.episode_id}`, {
    state: {
      seekTo: bookmark.start_seconds,
      podcast_image: bookmark.podcast_image,
      podcast_title: bookmark.podcast_title,
    },
  });

  return (
    <div className="bg-gray-900 rounded-xl px-4 py-3">
      {!compact && (
        <div className="flex items-center gap-2 mb-2 min-w-0">
          {bookmark.podcast_image && (
            <img src={bookmark.podcast_image} className="w-6 h-6 rounded object-cover flex-shrink-0" alt="" />
          )}
          <div className="min-w-0 flex-1">
            <div className="text-[11px] text-gray-500 truncate">{bookmark.podcast_title}</div>
            <div className="text-xs text-gray-300 truncate">{bookmark.episode_title}</div>
          </div>
        </div>
      )}

      <blockquote className="text-sm text-gray-100 leading-relaxed border-l-2 border-yellow-600/50 pl-3 selectable">
        {bookmark.text}
      </blockquote>

      {editing ? (
        <textarea
          value={note}
          onChange={e => saveNote(e.target.value)}
          onBlur={() => setEditing(false)}
          autoFocus
          rows={2}
          maxLength={1000}
          placeholder="Why keep this?"
          className="mt-2 w-full bg-gray-800 text-sm text-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
      ) : note ? (
        <button onClick={() => setEditing(true)}
                className="mt-2 text-left text-xs text-gray-400 hover:text-gray-200 w-full">
          {note}
        </button>
      ) : null}

      <div className="flex items-center gap-1 mt-2 -mr-2">
        <button
          onClick={jump}
          className="text-xs font-mono text-indigo-400 hover:text-indigo-300 min-h-[32px] px-1"
        >
          ▶ {fmtTime(bookmark.start_seconds)}
        </button>
        <div className="flex-1" />
        {!note && !editing && (
          <button
            onClick={() => setEditing(true)}
            className="text-xs text-gray-500 hover:text-gray-300 min-h-[32px] px-2"
          >
            + note
          </button>
        )}
        {saving && <span className="text-[10px] text-gray-600">saving…</span>}
        <CopyButton getText={text} label="Copy quote" />
        <ShareButton getText={text} getTitle={() => bookmark.episode_title || "Bookmark"} label="Share quote" />
        <button
          onClick={async () => { await deleteBookmark(bookmark.id); onDeleted(); }}
          aria-label="Delete bookmark"
          className="w-8 h-8 flex items-center justify-center text-gray-600 hover:text-red-400 rounded-full hover:bg-gray-800 transition-colors"
        >
          ✕
        </button>
      </div>
    </div>
  );
}

import { useNavigate } from "react-router-dom";
import { type FeedEpisode } from "../api/client";

/** Shared row for any list of episodes that is not the home feed. */
export function fmtDuration(secs?: number | null) {
  if (!secs) return null;
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

export function fmtDate(iso?: string) {
  if (!iso) return null;
  const d = new Date(iso);
  const diffDays = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays}d ago`;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export default function EpisodeRow({
  ep, index, right, onClick,
}: {
  ep: FeedEpisode;
  /** Position in a playlist, shown instead of artwork where order is the point. */
  index?: number;
  right?: React.ReactNode;
  onClick?: () => void;
}) {
  const nav = useNavigate();
  const open = onClick ?? (() => nav(`/player/${ep.id}`, { state: ep }));
  const art = ep.podcast_image || ep.image_url;

  return (
    <div className={`bg-gray-900 rounded-xl p-3 flex gap-3 items-center ${
      ep.played ? "opacity-60" : ""
    }`}>
      {index != null ? (
        <span className="w-6 text-center text-xs font-mono text-gray-600 flex-shrink-0">
          {index + 1}
        </span>
      ) : null}

      {art
        ? <img src={art} className="w-10 h-10 rounded-lg object-cover flex-shrink-0" alt="" />
        : <div className="w-10 h-10 rounded-lg bg-gray-800 flex-shrink-0 flex items-center justify-center">🎙</div>}

      <button onClick={open} className="flex-1 min-w-0 text-left">
        <div className="text-xs text-gray-500 truncate">{ep.podcast_title}</div>
        <div className="text-sm font-medium leading-snug line-clamp-2">{ep.title}</div>
        <div className="flex items-center gap-2 mt-1 text-xs text-gray-600">
          {fmtDate(ep.published_at) && <span>{fmtDate(ep.published_at)}</span>}
          {fmtDuration(ep.duration_seconds) && <span>· {fmtDuration(ep.duration_seconds)}</span>}
          {ep.transcript_status === "done" && <span title="Transcribed">· 📝</span>}
          {ep.distill_count > 0 && <span className="text-indigo-400">· ⚗️ {ep.distill_count}</span>}
          {ep.bookmark_count > 0 && <span className="text-yellow-500/80">· 🔖 {ep.bookmark_count}</span>}
        </div>
      </button>

      {right}
    </div>
  );
}

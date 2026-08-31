import { useNavigate } from "react-router-dom";
import { ProgressRecord } from "../api/client";

const fmtLeft = (secs: number) => {
  if (!isFinite(secs) || secs <= 0) return "";
  const h = Math.floor(secs / 3600), m = Math.round((secs % 3600) / 60);
  return h > 0 ? `${h}h ${m}m left` : `${Math.max(1, m)} min left`;
};

/**
 * The episodes you are part-way through, newest first.
 *
 * Reads the server's copy rather than this device's, which is the whole point:
 * something started on the phone has to show up here on the laptop. Rendered
 * from the records Home already fetched, so it costs no extra request.
 */
export default function ContinueListening({
  records,
  onDismiss,
}: {
  records: ProgressRecord[];
  onDismiss?: (episodeId: string) => void;
}) {
  const nav = useNavigate();

  // Filtered on position, not on `played`. In this app `played` is set when an
  // episode is *opened*, not when it is finished — it drives the "unplayed"
  // feed filter — so gating on it would leave this rail permanently empty.
  // Finishing an episode zeroes the position, which is what drops it from here.
  const items = records
    .filter(r => r.position > 0 && r.title)
    .sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at))
    .slice(0, 12);

  if (!items.length) return null;

  return (
    <div className="space-y-2">
      <h2 className="text-xs font-semibold uppercase tracking-widest text-gray-500">
        Continue listening
      </h2>
      {/* Horizontal rail: it is a shortcut, not the main feed, so it should
          cost one row of height on a phone rather than a screenful. */}
      <div className="flex gap-3 overflow-x-auto -mx-4 px-4 py-1 snap-x snap-mandatory scrollbar-none">
        {items.map(r => {
          const pct = r.duration && r.duration > 0
            ? Math.min(100, (r.position / r.duration) * 100)
            : 0;
          const left = r.duration && r.duration > 0 ? r.duration - r.position : 0;
          return (
            // The dismiss control is a sibling of the card rather than a child:
            // the card is itself a button, and a button inside a button is not
            // valid markup and behaves inconsistently.
            <div key={r.episode_id} className="relative flex-shrink-0 w-36 snap-start">
            {onDismiss && (
              <button
                onClick={() => onDismiss(r.episode_id)}
                aria-label={`Remove ${r.title ?? "episode"} from Continue listening`}
                title="Remove from Continue listening"
                className="absolute top-1 right-1 z-10 w-9 h-9 flex items-center justify-center text-white/70 hover:text-white"
              >
                <span className="w-5 h-5 flex items-center justify-center rounded-full bg-black/60 backdrop-blur-sm text-[11px] leading-none">
                  ✕
                </span>
              </button>
            )}
            <button
              onClick={() => nav(`/player/${r.episode_id}`)}
              className="w-full text-left bg-gray-900 rounded-xl overflow-hidden active:scale-[0.98] transition-transform"
            >
              {r.podcast_image
                ? <img
                    src={`/proxy/image?url=${encodeURIComponent(r.podcast_image)}`}
                    alt=""
                    className="w-36 h-24 object-cover"
                    loading="lazy"
                  />
                : <div className="w-36 h-24 bg-gray-800 flex items-center justify-center text-2xl">🎙</div>}
              <div className="p-2.5 space-y-1.5">
                <div className="text-xs font-medium leading-snug line-clamp-2 text-white">
                  {r.title}
                </div>
                <div className="text-[10px] text-gray-500 truncate">{fmtLeft(left)}</div>
                <div className="h-0.5 bg-gray-800 rounded-full overflow-hidden">
                  <div className="h-full bg-indigo-500" style={{ width: `${pct}%` }} />
                </div>
              </div>
            </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

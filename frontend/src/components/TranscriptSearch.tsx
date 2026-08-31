import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { searchTranscripts, TranscriptHit } from "../api/client";

const fmtTime = (s: number) => {
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.floor(s % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
};

/** Search what was actually said, and jump to the moment it was said. */
export default function TranscriptSearch() {
  const nav = useNavigate();
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<TranscriptHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  // Guards against an earlier, slower request overwriting a newer one.
  const latest = useRef(0);

  useEffect(() => {
    clearTimeout(timer.current);
    const term = q.trim();
    if (term.length < 2) {
      setHits([]); setSearched(false); setLoading(false);
      return;
    }
    setLoading(true);
    timer.current = setTimeout(async () => {
      const seq = ++latest.current;
      try {
        const r = await searchTranscripts(term);
        if (seq === latest.current) { setHits(r); setSearched(true); }
      } catch {
        if (seq === latest.current) { setHits([]); setSearched(true); }
      } finally {
        if (seq === latest.current) setLoading(false);
      }
    }, 300);
    return () => clearTimeout(timer.current);
  }, [q]);

  const jumpTo = (hit: TranscriptHit, start: number) =>
    nav(`/player/${hit.episode_id}`, {
      state: { seekTo: start, podcast_title: hit.podcast_title, podcast_image: hit.podcast_image },
    });

  return (
    <div className="space-y-3">
      <div className="relative">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round"
          className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none">
          <circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          value={q}
          onChange={e => setQ(e.target.value)}
          placeholder="Search what was said…"
          aria-label="Search transcripts"
          className="w-full bg-gray-800 text-white placeholder-gray-500 rounded-xl pl-9 pr-9 min-h-[44px] text-sm focus:outline-none focus:ring-2 ring-indigo-500"
        />
        {q && (
          <button onClick={() => setQ("")} aria-label="Clear"
            className="absolute right-1 top-1/2 -translate-y-1/2 w-10 h-10 flex items-center justify-center text-gray-500 hover:text-white">
            ✕
          </button>
        )}
      </div>

      {q.trim().length < 2 && (
        <p className="text-gray-500 text-xs px-1">
          Searches the transcripts of episodes you have transcribed. Accents are
          ignored, so <span className="text-gray-400">retro</span> finds{" "}
          <span className="text-gray-400">rétro</span>.
        </p>
      )}

      {loading && <div className="text-gray-500 text-sm px-1">Searching…</div>}

      {!loading && searched && hits.length === 0 && (
        <div className="text-center py-10 space-y-2">
          <div className="text-4xl">🔍</div>
          <p className="text-gray-400 text-sm">Nothing said about “{q.trim()}”.</p>
          <p className="text-gray-600 text-xs">Only transcribed episodes are searchable.</p>
        </div>
      )}

      {!loading && hits.map(hit => (
        <div key={hit.episode_id} className="bg-gray-900 rounded-xl overflow-hidden">
          <button
            onClick={() => nav(`/player/${hit.episode_id}`)}
            className="w-full text-left p-3 flex gap-3 items-start hover:bg-gray-800 transition-colors"
          >
            {hit.podcast_image && (
              <img src={hit.podcast_image} alt="" className="w-10 h-10 rounded object-cover flex-shrink-0" />
            )}
            <div className="min-w-0 flex-1">
              <div className="text-xs text-gray-500 truncate">{hit.podcast_title}</div>
              <div className="text-sm font-medium leading-snug line-clamp-2">{hit.episode_title}</div>
            </div>
            <span className="flex-shrink-0 text-[11px] text-indigo-300 bg-indigo-950 px-2 py-0.5 rounded-full">
              {hit.match_count}
            </span>
          </button>

          <div className="border-t border-gray-800 divide-y divide-gray-800">
            {hit.matches.map((m, i) => (
              <button
                key={i}
                onClick={() => jumpTo(hit, m.start)}
                className="w-full text-left px-3 py-2.5 min-h-[44px] hover:bg-gray-800 transition-colors flex gap-2"
              >
                <span className="flex-shrink-0 text-[11px] font-mono text-indigo-400 pt-0.5">
                  {fmtTime(m.start)}
                </span>
                <span className="text-xs text-gray-400 leading-relaxed line-clamp-3">{m.text}</span>
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

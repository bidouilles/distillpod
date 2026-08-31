import { useState } from "react";
import { Episode } from "../api/client";
import { Chip } from "./FeedFilterBar";

/** Predicates over an already-loaded episode. */
const STATUSES: { key: string; label: string; match: (e: Episode) => boolean }[] = [
  { key: "transcribed", label: "Transcribed", match: e => e.transcript_status === "done" },
  { key: "downloaded",  label: "Downloaded",  match: e => Boolean(e.downloaded) },
  { key: "adfree",      label: "Ad-free",     match: e => Boolean(e.ads_detected) },
];

export function filterEpisodes(episodes: Episode[], q: string, status: string): Episode[] {
  const needle = q.trim().toLowerCase();
  const pred = STATUSES.find(s => s.key === status)?.match;
  return episodes.filter(e => {
    if (needle && !e.title.toLowerCase().includes(needle)) return false;
    if (pred && !pred(e)) return false;
    return true;
  });
}

/**
 * Filter one podcast's episode list.
 *
 * Unlike the home feed this filters in the browser, because the whole list for
 * this podcast is already loaded — a round-trip would only add latency, and
 * there is no cap here for a filter to hide results behind.
 */
export default function EpisodeListFilter({
  q, status, onChange, shown, total,
}: {
  q: string;
  status: string;
  onChange: (q: string, status: string) => void;
  shown: number;
  total: number;
}) {
  const [focused, setFocused] = useState(false);
  const active = Boolean(q.trim() || status);

  return (
    <div className="space-y-2">
      <div className="relative">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round"
          className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none">
          <circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          value={q}
          onChange={e => onChange(e.target.value, status)}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          placeholder="Filter these episodes…"
          aria-label="Filter episodes in this podcast"
          className="w-full bg-gray-900 text-white placeholder-gray-500 rounded-xl pl-9 pr-9 min-h-[44px] text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
        {q && (
          <button
            onClick={() => onChange("", status)}
            aria-label="Clear search"
            className="absolute right-1 top-1/2 -translate-y-1/2 w-10 h-10 flex items-center justify-center text-gray-500 hover:text-white"
          >
            ✕
          </button>
        )}
      </div>

      <div className="flex gap-1.5 overflow-x-auto pb-1 -mx-4 px-4 scrollbar-none">
        {STATUSES.map(s => (
          <Chip key={s.key} active={status === s.key}
            onClick={() => onChange(q, status === s.key ? "" : s.key)}>
            {s.label}
          </Chip>
        ))}
      </div>

      {(active || focused) && (
        <div className="flex items-center justify-between text-xs text-gray-500">
          <span>{shown} of {total} episodes</span>
          {active && (
            <button onClick={() => onChange("", "")}
              className="text-indigo-400 hover:text-indigo-300 min-h-[32px] px-1">
              Clear
            </button>
          )}
        </div>
      )}
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import { Tag } from "../api/client";

export interface FeedFilterState {
  q: string;
  tagId: string;
  status: string;
  unplayedOnly: boolean;
}

export const EMPTY_FILTERS: FeedFilterState = { q: "", tagId: "", status: "", unplayedOnly: false };

export const hasActiveFilters = (f: FeedFilterState) =>
  Boolean(f.q.trim() || f.tagId || f.status || f.unplayedOnly);

/** Server-side statuses. `unplayedOnly` is separate — played state lives in
 *  localStorage, so it can only be applied on the client. */
const STATUSES = [
  { key: "transcribed", label: "Transcribed" },
  { key: "distilled",   label: "Distilled" },
  { key: "adfree",      label: "Ad-free" },
  { key: "downloaded",  label: "Downloaded" },
];

export function Chip({ active, onClick, children }: {
  active: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`flex-shrink-0 min-h-[36px] px-3 rounded-full text-xs font-medium whitespace-nowrap transition-colors ${
        active
          ? "bg-indigo-600 text-white"
          : "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200"
      }`}
    >
      {children}
    </button>
  );
}

export default function FeedFilterBar({
  filters, onChange, tags, resultCount, loading,
}: {
  filters: FeedFilterState;
  onChange: (f: FeedFilterState) => void;
  tags: Tag[];
  resultCount: number;
  loading: boolean;
}) {
  // Local mirror so typing stays responsive; the parent is told on a debounce.
  const [draftQ, setDraftQ] = useState(filters.q);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  // Keep in step when the parent clears filters from elsewhere.
  useEffect(() => { setDraftQ(filters.q); }, [filters.q]);

  useEffect(() => {
    if (draftQ === filters.q) return;
    clearTimeout(timer.current);
    timer.current = setTimeout(() => onChange({ ...filters, q: draftQ }), 250);
    return () => clearTimeout(timer.current);
  }, [draftQ]);

  const set = (patch: Partial<FeedFilterState>) => onChange({ ...filters, ...patch });
  const active = hasActiveFilters(filters);

  return (
    <div className="space-y-2">
      {/* Search */}
      <div className="relative">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round"
          className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-500 pointer-events-none">
          <circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
        </svg>
        <input
          value={draftQ}
          onChange={e => setDraftQ(e.target.value)}
          placeholder="Filter episodes…"
          aria-label="Filter episodes by title"
          className="w-full bg-gray-900 text-white placeholder-gray-500 rounded-xl pl-9 pr-9 min-h-[44px] text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
        {draftQ && (
          <button
            onClick={() => setDraftQ("")}
            aria-label="Clear search"
            className="absolute right-1 top-1/2 -translate-y-1/2 w-10 h-10 flex items-center justify-center text-gray-500 hover:text-white"
          >
            ✕
          </button>
        )}
      </div>

      {/* Chips — one scroll row keeps the feed above the fold on a phone. */}
      <div className="flex gap-1.5 overflow-x-auto pb-1 -mx-4 px-4 scrollbar-none">
        <Chip active={filters.unplayedOnly} onClick={() => set({ unplayedOnly: !filters.unplayedOnly })}>
          Unplayed
        </Chip>
        {/* Your own tags come before the built-in statuses: this row scrolls on
            a phone, and the tags you created are the reason you opened it. */}
        {tags.map(t => (
          <Chip key={t.id} active={filters.tagId === t.id}
            onClick={() => set({ tagId: filters.tagId === t.id ? "" : t.id })}>
            #{t.name}
          </Chip>
        ))}
        {tags.length > 0 && <div className="flex-shrink-0 w-px bg-gray-700 my-1.5 mx-1" />}
        {STATUSES.map(s => (
          <Chip key={s.key} active={filters.status === s.key}
            onClick={() => set({ status: filters.status === s.key ? "" : s.key })}>
            {s.label}
          </Chip>
        ))}
      </div>

      {active && (
        <div className="flex items-center justify-between text-xs text-gray-500">
          <span>
            {loading ? "Filtering…" : `${resultCount} episode${resultCount === 1 ? "" : "s"}`}
          </span>
          <button
            onClick={() => onChange(EMPTY_FILTERS)}
            className="text-indigo-400 hover:text-indigo-300 min-h-[32px] px-1"
          >
            Clear filters
          </button>
        </div>
      )}
    </div>
  );
}

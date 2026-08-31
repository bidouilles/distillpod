import { useEffect, useRef, useState } from "react";
import { type FeedSort, type Tag } from "../api/client";

export type LengthBand = "" | "short" | "medium" | "long";

export interface FeedFilterState {
  q: string;
  tagId: string;
  status: string;
  unplayedOnly: boolean;
  /** "I have twenty minutes" — the most common real query. */
  length: LengthBand;
  sort: FeedSort;
}

export const EMPTY_FILTERS: FeedFilterState = {
  q: "", tagId: "", status: "", unplayedOnly: false, length: "", sort: "newest",
};

export const hasActiveFilters = (f: FeedFilterState) =>
  Boolean(f.q.trim() || f.tagId || f.status || f.unplayedOnly || f.length || f.sort !== "newest");

/** Minutes each band maps to, sent to the server as bounds. */
export const LENGTH_BOUNDS: Record<Exclude<LengthBand, "">, { min?: number; max?: number }> = {
  short:  { max: 20 },
  medium: { min: 20, max: 60 },
  long:   { min: 60 },
};

const LENGTHS: { key: Exclude<LengthBand, "">; label: string }[] = [
  { key: "short",  label: "< 20 min" },
  { key: "medium", label: "20–60 min" },
  { key: "long",   label: "> 1 h" },
];

/** Every one of these is resolved server-side, including `unplayed`. */
const STATUSES = [
  { key: "transcribed", label: "Transcribed" },
  { key: "distilled",   label: "Distilled" },
  { key: "bookmarked",  label: "Bookmarked" },
  { key: "adfree",      label: "Ad-free" },
  { key: "downloaded",  label: "Downloaded" },
];

const SORTS: { key: FeedSort; label: string }[] = [
  { key: "newest",   label: "Newest" },
  { key: "oldest",   label: "Oldest" },
  { key: "shortest", label: "Shortest" },
  { key: "longest",  label: "Longest" },
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
  const [more, setMore] = useState(false);
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

      {/* First row: what you reach for most — how much time you have, and
          whether you have heard it. One scroll row keeps the feed above the
          fold on a phone; everything rarer is behind "More". */}
      <div className="flex gap-1.5 overflow-x-auto py-1 -mx-4 px-4 scrollbar-none">
        <Chip active={filters.unplayedOnly} onClick={() => set({ unplayedOnly: !filters.unplayedOnly })}>
          Unplayed
        </Chip>
        {LENGTHS.map(l => (
          <Chip
            key={l.key}
            active={filters.length === l.key}
            onClick={() => set({ length: filters.length === l.key ? "" : l.key })}
          >
            {l.label}
          </Chip>
        ))}
        {tags.map(t => (
          <Chip key={t.id} active={filters.tagId === t.id}
            onClick={() => set({ tagId: filters.tagId === t.id ? "" : t.id })}>
            #{t.name}
          </Chip>
        ))}
        <Chip active={more} onClick={() => setMore(m => !m)}>
          {more ? "Less ▲" : "More ▾"}
        </Chip>
      </div>

      {more && (
        <div className="space-y-2 pt-0.5">
          <div className="flex gap-1.5 overflow-x-auto py-1 -mx-4 px-4 scrollbar-none">
            {STATUSES.map(s => (
              <Chip key={s.key} active={filters.status === s.key}
                onClick={() => set({ status: filters.status === s.key ? "" : s.key })}>
                {s.label}
              </Chip>
            ))}
          </div>
          <div className="flex gap-1.5 overflow-x-auto py-1 -mx-4 px-4 scrollbar-none items-center">
            <span className="text-[11px] text-gray-500 uppercase tracking-wider font-semibold flex-shrink-0 pr-1">
              Order
            </span>
            {SORTS.map(s => (
              <Chip key={s.key} active={filters.sort === s.key} onClick={() => set({ sort: s.key })}>
                {s.label}
              </Chip>
            ))}
          </div>
        </div>
      )}

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

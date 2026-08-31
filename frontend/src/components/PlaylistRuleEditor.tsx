import { getTags, type FeedSort, type PlaylistRules, type Tag } from "../api/client";
import { useEffect, useState } from "react";
import { Chip } from "./FeedFilterBar";

/**
 * What a smart playlist selects.
 *
 * Deliberately the same vocabulary as the filter chips on the feed, because it
 * is the same query underneath — a rule here means exactly what picking those
 * chips means on Home. Anything else would be a difference nobody could see
 * until a playlist disagreed with the feed.
 */

const STATUSES: { key: string; label: string }[] = [
  { key: "transcribed", label: "Transcribed" },
  { key: "distilled",   label: "Distilled" },
  { key: "bookmarked",  label: "Bookmarked" },
  { key: "adfree",      label: "Ad-free" },
  { key: "downloaded",  label: "Downloaded" },
];

const LENGTHS: { label: string; min: number | null; max: number | null }[] = [
  { label: "Under 20 min", min: null, max: 20 },
  { label: "20–60 min",    min: 20,   max: 60 },
  { label: "Over an hour", min: 60,   max: null },
];

const SORTS: { key: FeedSort; label: string }[] = [
  { key: "newest",   label: "Newest" },
  { key: "oldest",   label: "Oldest" },
  { key: "shortest", label: "Shortest" },
  { key: "longest",  label: "Longest" },
];

export default function PlaylistRuleEditor({
  rules, onChange,
}: {
  rules: PlaylistRules;
  onChange: (r: PlaylistRules) => void;
}) {
  const [tags, setTags] = useState<Tag[]>([]);
  useEffect(() => { getTags().then(setTags).catch(() => {}); }, []);

  const set = (patch: Partial<PlaylistRules>) => onChange({ ...rules, ...patch });

  const lengthActive = (l: typeof LENGTHS[number]) =>
    (rules.min_minutes ?? null) === l.min && (rules.max_minutes ?? null) === l.max;

  return (
    <div className="space-y-3">
      <Row label="Only">
        <Chip active={rules.unplayed} onClick={() => set({ unplayed: !rules.unplayed })}>
          Unplayed
        </Chip>
        {STATUSES.map(s => (
          <Chip
            key={s.key}
            active={rules.status === s.key}
            onClick={() => set({ status: rules.status === s.key ? null : s.key })}
          >
            {s.label}
          </Chip>
        ))}
      </Row>

      <Row label="Length">
        {LENGTHS.map(l => (
          <Chip
            key={l.label}
            active={lengthActive(l)}
            onClick={() => set(lengthActive(l)
              ? { min_minutes: null, max_minutes: null }
              : { min_minutes: l.min, max_minutes: l.max })}
          >
            {l.label}
          </Chip>
        ))}
      </Row>

      {tags.length > 0 && (
        <Row label="Tagged">
          {tags.map(t => (
            <Chip
              key={t.id}
              active={rules.tag_id === t.id}
              onClick={() => set({ tag_id: rules.tag_id === t.id ? null : t.id })}
            >
              #{t.name}
            </Chip>
          ))}
        </Row>
      )}

      <Row label="Order">
        {SORTS.map(s => (
          <Chip key={s.key} active={rules.sort === s.key} onClick={() => set({ sort: s.key })}>
            {s.label}
          </Chip>
        ))}
      </Row>

      <Row label="At most">
        {[10, 25, 50, 100].map(n => (
          <Chip key={n} active={rules.limit === n} onClick={() => set({ limit: n })}>
            {n}
          </Chip>
        ))}
      </Row>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[11px] text-gray-500 uppercase tracking-wider font-semibold mb-1.5">
        {label}
      </div>
      <div className="flex gap-1.5 flex-wrap">{children}</div>
    </div>
  );
}

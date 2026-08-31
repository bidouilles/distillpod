/**
 * One control for switching between sections of a screen.
 *
 * The Library and Saved both grew from a single list into a place with two or
 * three, and a phone has no room for either a sidebar or more bottom tabs. A
 * segmented control keeps the whole screen for content and says, in one line,
 * everything the section contains.
 */
export interface Segment<T extends string> {
  key: T;
  label: string;
  /** Rendered after the label — a count, usually. Hidden when zero. */
  badge?: number;
}

export default function SegmentedTabs<T extends string>({
  segments, value, onChange, className = "",
}: {
  segments: Segment<T>[];
  value: T;
  onChange: (key: T) => void;
  className?: string;
}) {
  return (
    <div
      role="tablist"
      className={`flex gap-1 bg-gray-900 rounded-xl p-1 ${className}`}
    >
      {segments.map(s => {
        const active = s.key === value;
        return (
          <button
            key={s.key}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(s.key)}
            className={`flex-1 min-h-[40px] rounded-lg text-sm font-medium transition-colors flex items-center justify-center gap-1.5 ${
              active
                ? "bg-gray-800 text-white shadow-sm"
                : "text-gray-500 hover:text-gray-300"
            }`}
          >
            {s.label}
            {s.badge ? (
              <span className={`text-[11px] font-semibold rounded-full px-1.5 ${
                active ? "bg-indigo-600/30 text-indigo-300" : "bg-gray-800 text-gray-500"
              }`}>
                {s.badge}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

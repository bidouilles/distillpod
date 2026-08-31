/**
 * What a library row actually is.
 *
 * Everything in the library looks alike once it has a name and a picture, but
 * the rows are not the same thing: some are podcasts pulled from RSS, some are
 * YouTube channels that get polled, and some exist only because a single video
 * was added and brought its channel along with it. That last kind will never
 * gain new episodes on its own, which is worth being able to see.
 */
const KINDS = {
  youtube_channel: {
    label: "YouTube",
    className: "bg-red-500/15 text-red-300",
    icon: (
      <svg viewBox="0 0 24 24" fill="currentColor" className="w-3 h-3">
        <path d="M21.6 7.2a2.5 2.5 0 0 0-1.8-1.8C18.2 5 12 5 12 5s-6.2 0-7.8.4A2.5 2.5 0 0 0 2.4 7.2 26 26 0 0 0 2 12a26 26 0 0 0 .4 4.8 2.5 2.5 0 0 0 1.8 1.8C5.8 19 12 19 12 19s6.2 0 7.8-.4a2.5 2.5 0 0 0 1.8-1.8A26 26 0 0 0 22 12a26 26 0 0 0-.4-4.8z" />
        <polygon points="10,15 15,12 10,9" fill="#0b1020" />
      </svg>
    ),
  },
  youtube_video: {
    label: "Single video",
    className: "bg-gray-500/15 text-gray-400",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
           strokeLinecap="round" strokeLinejoin="round" className="w-3 h-3">
        <rect x="2" y="4" width="20" height="16" rx="3" />
        <polygon points="10,9 15,12 10,15" fill="currentColor" stroke="none" />
      </svg>
    ),
  },
  podcast: {
    label: "Podcast",
    className: "bg-indigo-500/15 text-indigo-300",
    icon: (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
           strokeLinecap="round" strokeLinejoin="round" className="w-3 h-3">
        <rect x="9" y="2" width="6" height="11" rx="3" />
        <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
        <line x1="12" y1="18" x2="12" y2="22" />
      </svg>
    ),
  },
} as const;

export default function SourceBadge({ source }: { source?: string }) {
  const kind = KINDS[(source || "podcast") as keyof typeof KINDS] ?? KINDS.podcast;
  return (
    <span
      title={kind.label}
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-semibold leading-none flex-shrink-0 ${kind.className}`}
    >
      {kind.icon}
      {kind.label}
    </span>
  );
}

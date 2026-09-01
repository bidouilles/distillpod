import { useEffect, useRef, useState } from "react";
import { getBackgroundJobs, type LaneStatus } from "../api/client";

/**
 * What the server is working on right now.
 *
 * Background work takes turns per resource rather than all at once, which is
 * what stops two yt-dlp processes racing the same rate limit or two
 * transcriptions fighting over two cores. The cost of that is a wait nobody can
 * see, so this makes it visible: what is running, for how long, and what is
 * queued behind it.
 */

const LANES: Record<string, { label: string; why: string }> = {
  youtube:  { label: "YouTube", why: "spaced apart — YouTube refuses an address that asks too fast" },
  media:    { label: "Downloads", why: "one at a time, so the episode you want finishes first" },
  stt:      { label: "Transcription", why: "one at a time — it bills, or it pins a core" },
  llm:      { label: "AI", why: "one agent process at a time on two cores" },
  web:      { label: "Search & embeddings", why: "paced" },
};

const PRIORITY: Record<string, { label: string; className: string }> = {
  user:        { label: "you're waiting", className: "bg-indigo-600/25 text-indigo-300" },
  interactive: { label: "you asked", className: "bg-indigo-600/15 text-indigo-300/80" },
  background:  { label: "housekeeping", className: "bg-gray-800 text-gray-500" },
};

function fmtElapsed(seconds: number) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  return `${m}m ${Math.round(seconds % 60)}s`;
}

export default function ActivityCard() {
  const [lanes, setLanes] = useState<Record<string, LaneStatus> | null>(null);
  const poll = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

  const read = () => getBackgroundJobs().then(setLanes).catch(() => {});

  useEffect(() => {
    read();
    // Fast enough to feel live while something runs, slow enough to be free
    // when nothing does — the endpoint reads in-memory state either way.
    poll.current = setInterval(read, 3000);
    return () => { if (poll.current) clearInterval(poll.current); };
  }, []);

  if (!lanes) return null;

  const entries = Object.entries(lanes);
  const active = entries.filter(([, l]) => l.running || l.waiting > 0);
  const done = entries.reduce((n, [, l]) => n + l.completed, 0);

  return (
    <div className="bg-gray-900 rounded-2xl px-4 py-4 space-y-3">
      <div className="flex items-baseline justify-between gap-3">
        <div className="text-xs text-gray-500 uppercase tracking-wider font-semibold">
          Server activity
        </div>
        {done > 0 && (
          <div className="text-[11px] text-gray-600">{done} finished since restart</div>
        )}
      </div>

      {active.length === 0 ? (
        <p className="text-sm text-gray-400">
          Idle — nothing running or queued.
        </p>
      ) : (
        <div className="space-y-3">
          {active.map(([name, lane]) => {
            const meta = LANES[name] ?? { label: name, why: "" };
            const priority = PRIORITY[lane.priority] ?? PRIORITY.background;
            return (
              <div key={name} className="space-y-1">
                <div className="flex items-center gap-2">
                  <span className="text-sm text-gray-200">{meta.label}</span>
                  {lane.running && (
                    <span className={`text-[10px] font-semibold rounded-full px-1.5 ${priority.className}`}>
                      {priority.label}
                    </span>
                  )}
                  <span className="flex-1" />
                  {lane.running && (
                    <span className="text-[11px] text-gray-500 font-mono flex-shrink-0">
                      {fmtElapsed(lane.running_for)}
                    </span>
                  )}
                </div>

                {lane.running ? (
                  <div className="flex items-center gap-2 text-xs text-gray-400">
                    <span className="w-2.5 h-2.5 border-2 border-gray-700 border-t-indigo-400 rounded-full animate-spin inline-block flex-shrink-0" />
                    <span className="truncate">{lane.running}</span>
                  </div>
                ) : (
                  <div className="text-xs text-gray-500">Waiting for its turn</div>
                )}

                {lane.waiting > 0 && (
                  <div className="text-[11px] text-gray-600 leading-relaxed">
                    {lane.waiting} queued
                    {lane.queue.length > 0 && <> — next: {lane.queue[0]}</>}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      <p className="text-[11px] text-gray-600 leading-relaxed">
        Work takes turns per resource so nothing competes with itself: YouTube
        requests are {LANES.youtube.why}, transcription runs {LANES.stt.why}.
        Anything you are waiting on goes ahead of housekeeping.
      </p>
    </div>
  );
}

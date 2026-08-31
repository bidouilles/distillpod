import { useEffect, useRef, useState } from "react";
import { getBackfillStatus, startBackfill, stopBackfill, BackfillState } from "../api/client";

/**
 * Fill in transcripts for episodes that arrived without one.
 *
 * A channel import creates episode rows without fetching anything per video and
 * the nightly caption pass only looks back 48 hours, so most of a library is
 * unsearchable — and search, chat, distills and the export all need a
 * transcript. This works through the backlog from YouTube captions.
 *
 * Captions only: it never runs speech-to-text, because a whole back catalogue
 * through a paid backend is a bill nobody asked for. Videos without captions
 * are counted and left for first play.
 */
export default function BackfillCard() {
  const [state, setState] = useState<BackfillState | null>(null);
  const poll = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

  const refresh = async () => {
    try { setState(await getBackfillStatus()); } catch { /* transient */ }
  };

  useEffect(() => {
    refresh();
    return () => { if (poll.current) clearInterval(poll.current); };
  }, []);

  // Watch while it runs. The work is server-side, so this rejoins a run that
  // was started before this screen was opened.
  useEffect(() => {
    if (!state?.running) { if (poll.current) clearInterval(poll.current); return; }
    poll.current = setInterval(refresh, 2000);
    return () => { if (poll.current) clearInterval(poll.current); };
  }, [state?.running]);

  if (!state) return null;
  const { running, pending, total, transcribed, no_captions, failed } = state;
  if (!running && pending === 0) return null;   // nothing to offer

  const done = transcribed + no_captions + failed;

  return (
    <div className="bg-gray-900 rounded-xl p-4 space-y-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-medium">
            {running ? "Filling in transcripts…" : `${pending} episode${pending === 1 ? "" : "s"} without a transcript`}
          </div>
          <div className="text-xs text-gray-500 mt-0.5">
            {running
              ? `${done} of ${total} · ${transcribed} transcribed`
              : "From YouTube captions — no audio downloaded, nothing transcribed at cost."}
          </div>
        </div>
        <button
          onClick={async () => {
            if (running) { await stopBackfill().catch(() => {}); }
            else { await startBackfill().catch(() => {}); }
            refresh();
          }}
          className={`flex-shrink-0 min-h-[44px] px-4 rounded-full text-xs font-semibold transition-colors ${
            running ? "bg-gray-800 hover:bg-gray-700 text-gray-300"
                    : "bg-indigo-600 hover:bg-indigo-500 text-white"
          }`}
        >
          {running ? "Stop" : "Fill in"}
        </button>
      </div>

      {running && (
        <>
          <div className="h-1 bg-gray-800 rounded-full overflow-hidden">
            <div className="h-full bg-indigo-500 transition-[width] duration-500"
                 style={{ width: `${total ? (done / total) * 100 : 0}%` }} />
          </div>
          {state.current && (
            <div className="text-[11px] text-gray-600 truncate">{state.current}</div>
          )}
        </>
      )}

      {!running && (no_captions > 0 || failed > 0) && state.finished_at && (
        <div className="text-[11px] text-gray-600">
          Last run: {transcribed} transcribed
          {no_captions > 0 && `, ${no_captions} had no captions`}
          {failed > 0 && `, ${failed} failed`}
          {state.stopped_early && " · stopped early"}
        </div>
      )}
    </div>
  );
}

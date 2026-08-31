import { useEffect, useRef, useState } from "react";
import {
  buildSemanticIndex, getSemanticIndex, stopSemanticIndex,
  type SemanticIndexState,
} from "../api/client";

/**
 * The state of meaning-based search, and the button that builds it.
 *
 * Sits above Ask because that is where its absence is felt: without it, a
 * question only finds passages containing the words it was asked with, so
 * "where did someone talk about burning out" finds nothing unless somebody said
 * "burnout".
 *
 * The card says what building it costs, because it is the second thing in this
 * app that sends anything off the box — and then disappears once the index is
 * complete, since a control with nothing to do is noise.
 */
export default function SemanticIndexCard({ onBuilt }: { onBuilt?: () => void }) {
  const [state, setState] = useState<SemanticIndexState | null>(null);
  const [busy, setBusy] = useState(false);
  const poll = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

  const read = () => getSemanticIndex().then(setState).catch(() => {});

  useEffect(() => {
    read();
    return () => { if (poll.current) clearInterval(poll.current); };
  }, []);

  useEffect(() => {
    if (!state?.job.running) {
      if (poll.current) clearInterval(poll.current);
      return;
    }
    poll.current = setInterval(read, 2000);
    return () => { if (poll.current) clearInterval(poll.current); };
  }, [state?.job.running]);

  useEffect(() => {
    // Tell the parent once a run finishes, so a pending question can be retried
    // against the fuller index.
    if (state && !state.job.running && state.job.finished_at) onBuilt?.();
  }, [state?.job.finished_at, state?.job.running]);

  if (!state) return null;

  const { engine, indexed, transcribed, pending, windows, job } = state;

  // Nothing to offer: no backend, or everything already indexed.
  if (engine === "off") {
    return (
      <div className="bg-gray-900 rounded-xl px-4 py-3">
        <div className="text-xs text-gray-400">
          Answers use keyword search only.
        </div>
        <div className="text-[11px] text-gray-600 mt-1 leading-relaxed">
          Meaning-based search needs an embedding backend. Set
          <code className="text-gray-400 mx-1">EMBED_BACKEND=mistral</code>
          to use the Mistral key this server already has, or install
          sentence-transformers for a local model.
        </div>
      </div>
    );
  }

  if (!job.running && pending === 0 && indexed > 0) return null;

  const done = job.total ? job.indexed + job.failed : 0;
  const percent = job.total ? Math.round((done / job.total) * 100) : 0;

  return (
    <div className="bg-gray-900 rounded-xl px-4 py-3 space-y-2.5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-medium">
            {job.running ? "Indexing your episodes…" : "Search by meaning, not just words"}
          </div>
          <div className="text-[11px] text-gray-500 mt-0.5 leading-relaxed">
            {job.running
              ? `${done} of ${job.total}`
              : indexed > 0
                ? `${indexed} of ${transcribed} episodes indexed · ${windows} passages`
                : `${pending} transcribed episode${pending === 1 ? "" : "s"} to index. ` +
                  (engine === "mistral"
                    ? "Their text is sent to Mistral to be embedded, once."
                    : "Embedded locally on this server.")}
          </div>
        </div>
        {job.running ? (
          <button
            onClick={() => stopSemanticIndex().then(read)}
            className="text-xs text-gray-400 hover:text-white flex-shrink-0 min-h-[36px] px-2"
          >
            Stop
          </button>
        ) : (
          <button
            onClick={async () => {
              setBusy(true);
              try { await buildSemanticIndex(); await read(); }
              finally { setBusy(false); }
            }}
            disabled={busy}
            className="text-xs font-medium bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-full px-3 min-h-[36px] flex-shrink-0"
          >
            {busy ? "…" : indexed > 0 ? "Index the rest" : "Build index"}
          </button>
        )}
      </div>

      {job.running && (
        <>
          <div className="h-1 bg-gray-800 rounded-full overflow-hidden">
            <div className="h-full bg-indigo-500 transition-[width] duration-500"
                 style={{ width: `${percent}%` }} />
          </div>
          {job.current && (
            <div className="text-[11px] text-gray-600 truncate">{job.current}</div>
          )}
        </>
      )}

      {!job.running && job.failed > 0 && (
        <div className="text-[11px] text-yellow-600/80">
          {job.failed} episode{job.failed === 1 ? "" : "s"} could not be embedded.
        </div>
      )}
    </div>
  );
}

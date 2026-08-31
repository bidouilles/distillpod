import { useEffect, useState } from "react";
import {
  getStorage, pruneStorage, setRetentionPolicy,
  type PruneResult, type StorageUsage,
} from "../api/client";

/**
 * What the media directory is costing, and how to get it back.
 *
 * This is the screen a self-hosted app needs and a hosted one never shows you.
 * Every episode played leaves an MP3 behind and every one with ads leaves a
 * second, re-encoded copy beside it, so without this the disk filling up was
 * only a question of how long the app had been useful.
 *
 * "Free up space" asks first: it runs a dry run, shows exactly what would go,
 * and only deletes when that is confirmed.
 */

function fmtBytes(bytes: number): string {
  if (!bytes) return "0 MB";
  const mb = bytes / (1024 * 1024);
  if (mb < 1) return "<1 MB";
  if (mb < 1024) return `${Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

const RETENTION_CHOICES = [
  { days: 0,   label: "Keep everything" },
  { days: 7,   label: "7 days" },
  { days: 30,  label: "30 days" },
  { days: 90,  label: "90 days" },
];

export default function StoragePanel() {
  const [usage, setUsage] = useState<StorageUsage | null>(null);
  const [loading, setLoading] = useState(true);
  const [pending, setPending] = useState<PruneResult | null>(null);
  const [done, setDone] = useState<PruneResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const load = () => {
    setLoading(true);
    getStorage().then(setUsage).catch(() => setError("Could not read storage"))
      .finally(() => setLoading(false));
  };

  useEffect(load, []);

  const choosePolicy = async (days: number) => {
    if (!usage) return;
    setBusy(true);
    try {
      await setRetentionPolicy(days, usage.policy.played_only);
      load();
    } catch { setError("Could not save that"); }
    finally { setBusy(false); }
  };

  const togglePlayedOnly = async () => {
    if (!usage) return;
    setBusy(true);
    try {
      await setRetentionPolicy(usage.policy.days, !usage.policy.played_only);
      load();
    } catch { setError("Could not save that"); }
    finally { setBusy(false); }
  };

  const preview = async () => {
    setBusy(true); setError(""); setDone(null);
    try {
      // Deliberately independent of the stored policy: this button means "what
      // could go", and answering "nothing, retention is off" would be useless.
      const days = usage?.policy.days || 30;
      setPending(await pruneStorage(true, days));
    } catch { setError("Could not work out what to clear"); }
    finally { setBusy(false); }
  };

  const confirm = async () => {
    setBusy(true);
    try {
      const days = usage?.policy.days || 30;
      const result = await pruneStorage(false, days);
      setDone(result);
      setPending(null);
      load();
    } catch { setError("Could not free up space"); }
    finally { setBusy(false); }
  };

  if (loading && !usage) {
    return <div className="bg-gray-900 rounded-2xl h-40 animate-pulse" />;
  }
  if (!usage) {
    return <p className="text-sm text-gray-500 text-center py-8">{error || "No storage data."}</p>;
  }

  const biggest = usage.by_podcast.slice(0, 8);
  const max = biggest[0]?.bytes || 1;

  return (
    <div className="space-y-4">
      {/* Total */}
      <div className="bg-gray-900 rounded-2xl px-4 py-4">
        <div className="flex items-end justify-between">
          <div>
            <div className="text-xs text-gray-500 uppercase tracking-wider font-semibold">
              Audio on disk
            </div>
            <div className="text-2xl font-bold mt-1">{fmtBytes(usage.total_bytes)}</div>
          </div>
          <div className="text-right text-xs text-gray-500">
            <div>{usage.episodes} episode{usage.episodes === 1 ? "" : "s"}</div>
            {usage.orphan_files > 0 && (
              <div className="text-yellow-500/80 mt-0.5">
                +{usage.orphan_files} stray file{usage.orphan_files === 1 ? "" : "s"}
              </div>
            )}
          </div>
        </div>
        <p className="text-[11px] text-gray-600 mt-3 leading-relaxed">
          Clearing an episode removes only its audio. The transcript, chapters,
          distills and bookmarks stay, and it downloads again when you play it.
        </p>
      </div>

      {/* Per-podcast breakdown */}
      {biggest.length > 0 && (
        <div className="bg-gray-900 rounded-2xl px-4 py-4 space-y-2.5">
          <div className="text-xs text-gray-500 uppercase tracking-wider font-semibold mb-1">
            Where it goes
          </div>
          {biggest.map(p => (
            <div key={p.podcast_id}>
              <div className="flex justify-between text-xs mb-1 gap-2">
                <span className="truncate text-gray-300">{p.title}</span>
                <span className="text-gray-500 flex-shrink-0 font-mono">{fmtBytes(p.bytes)}</span>
              </div>
              <div className="h-1 rounded-full bg-gray-800 overflow-hidden">
                <div className="h-full bg-indigo-500/70 rounded-full"
                     style={{ width: `${Math.max(2, (p.bytes / max) * 100)}%` }} />
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Policy */}
      <div className="bg-gray-900 rounded-2xl px-4 py-4 space-y-3">
        <div>
          <div className="text-xs text-gray-500 uppercase tracking-wider font-semibold">
            Keep audio for
          </div>
          <p className="text-[11px] text-gray-600 mt-1">
            Applied by the nightly job. Nothing queued or half-heard is ever cleared.
          </p>
        </div>
        <div className="flex gap-1.5 flex-wrap">
          {RETENTION_CHOICES.map(c => (
            <button
              key={c.days}
              disabled={busy}
              onClick={() => choosePolicy(c.days)}
              className={`min-h-[36px] px-3 rounded-full text-xs font-medium transition-colors disabled:opacity-50 ${
                usage.policy.days === c.days
                  ? "bg-indigo-600 text-white"
                  : "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200"
              }`}
            >
              {c.label}
            </button>
          ))}
        </div>
        {usage.policy.days > 0 && (
          <button
            onClick={togglePlayedOnly}
            disabled={busy}
            className="flex items-center gap-2 text-xs text-gray-400 hover:text-gray-200 min-h-[36px]"
          >
            <span className={`w-9 h-5 rounded-full transition-colors relative flex-shrink-0 ${
              usage.policy.played_only ? "bg-indigo-600" : "bg-gray-700"
            }`}>
              <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all ${
                usage.policy.played_only ? "left-4" : "left-0.5"
              }`} />
            </span>
            Only clear episodes I have played
          </button>
        )}
      </div>

      {/* Free up space now */}
      <div className="space-y-2">
        {!pending && !done && (
          <button
            onClick={preview}
            disabled={busy}
            className="w-full min-h-[44px] rounded-xl bg-gray-800 hover:bg-gray-700 disabled:opacity-60 text-sm text-gray-200 font-medium transition-colors"
          >
            {busy ? "Working out what can go…" : "🧹 Free up space now"}
          </button>
        )}

        {pending && (
          <div className="bg-gray-900 rounded-2xl px-4 py-4 space-y-3">
            {pending.freed_bytes > 0 ? (
              <>
                <p className="text-sm text-gray-200">
                  Clearing <span className="font-semibold">{pending.episodes}</span> episode
                  {pending.episodes === 1 ? "" : "s"}
                  {pending.orphans > 0 && ` and ${pending.orphans} stray file${pending.orphans === 1 ? "" : "s"}`}
                  {" "}frees <span className="font-semibold">{fmtBytes(pending.freed_bytes)}</span>.
                </p>
                {pending.cleared.length > 0 && (
                  <ul className="text-xs text-gray-500 space-y-1 max-h-40 overflow-y-auto">
                    {pending.cleared.map(c => (
                      <li key={c.episode_id} className="flex justify-between gap-2">
                        <span className="truncate">{c.title}</span>
                        <span className="font-mono flex-shrink-0">{fmtBytes(c.bytes)}</span>
                      </li>
                    ))}
                  </ul>
                )}
                <div className="flex gap-2">
                  <button
                    onClick={confirm}
                    disabled={busy}
                    className="flex-1 min-h-[44px] rounded-xl bg-red-600/80 hover:bg-red-600 text-white text-sm font-semibold disabled:opacity-60"
                  >
                    Delete the audio
                  </button>
                  <button
                    onClick={() => setPending(null)}
                    className="min-h-[44px] px-4 rounded-xl bg-gray-800 hover:bg-gray-700 text-sm text-gray-300"
                  >
                    Cancel
                  </button>
                </div>
              </>
            ) : (
              <>
                <p className="text-sm text-gray-400">
                  Nothing to clear — everything on disk is queued, part-heard, or too recent.
                </p>
                <button
                  onClick={() => setPending(null)}
                  className="min-h-[40px] px-4 rounded-xl bg-gray-800 hover:bg-gray-700 text-sm text-gray-300"
                >
                  OK
                </button>
              </>
            )}
          </div>
        )}

        {done && (
          <div className="bg-green-900/20 border border-green-800/40 rounded-2xl px-4 py-3 text-sm text-green-300">
            Freed {fmtBytes(done.freed_bytes)} from {done.episodes} episode
            {done.episodes === 1 ? "" : "s"}.
          </div>
        )}

        {error && <p className="text-xs text-red-400 text-center">{error}</p>}
      </div>
    </div>
  );
}

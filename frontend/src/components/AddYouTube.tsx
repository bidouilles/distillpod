import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { addYoutubeVideo, getEpisodes, getTranscriptStatus, YouTubeAddResult } from "../api/client";

const fmtDuration = (s?: number) => {
  if (!s) return "";
  if (s < 60) return `${Math.round(s)}s`;
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m} min`;
};

/**
 * Paste a YouTube URL, get an episode.
 *
 * The server returns as soon as it has the video's metadata; captions and the
 * audio download continue in the background. So the card appears immediately
 * and then polls the same transcript-status endpoint the player uses, rather
 * than making the user stare at a spinner for the length of a download.
 */
export default function AddYouTube() {
  const nav = useNavigate();
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [added, setAdded] = useState<YouTubeAddResult | null>(null);
  const [status, setStatus] = useState<string>("");
  const [imported, setImported] = useState<number | null>(null);
  const poll = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

  // Poll until the transcript settles. Captions land in seconds; a video with
  // none falls back to speech-to-text and can take minutes.
  useEffect(() => {
    clearInterval(poll.current);
    // A channel has no transcript of its own to wait on; its videos are
    // imported in the background and appear on the channel page.
    const episodeId = added?.kind === "video" ? added.episode_id : undefined;
    if (!episodeId || status === "done" || status === "error") return;
    poll.current = setInterval(async () => {
      try {
        const r = await getTranscriptStatus(episodeId);
        setStatus(r.status);
      } catch { /* keep polling; a transient failure is not fatal */ }
    }, 3000);
    return () => clearInterval(poll.current);
  }, [added, status]);

  // A channel imports in the background, so the card has to stop claiming it is
  // still importing once the episodes are in.
  useEffect(() => {
    if (!added || added.kind !== "channel") return;
    let stop = false;
    let tries = 0;
    const tick = async () => {
      tries += 1;
      try {
        const eps = await getEpisodes(added.podcast_id);
        if (stop) return;
        if (eps.length > 0) { setImported(eps.length); return; }
      } catch { /* transient */ }
      if (!stop && tries < 20) setTimeout(tick, 3000);
    };
    tick();
    return () => { stop = true; };
  }, [added]);

  const submit = async () => {
    const trimmed = url.trim();
    if (!trimmed || busy) return;
    setBusy(true); setError(""); setAdded(null); setStatus(""); setImported(null);
    try {
      const r = await addYoutubeVideo(trimmed);
      setAdded(r);
      setStatus(r.kind === "channel" ? "done" : r.already_added ? "done" : "queued");
      setUrl("");
    } catch {
      setError("Could not add that. Check the link is a YouTube video or channel.");
    } finally {
      setBusy(false);
    }
  };

  const statusLabel: Record<string, string> = {
    queued: "Fetching transcript…",
    processing: "Fetching transcript…",
    done: "Transcript ready",
    error: "Transcript failed — you can still listen",
  };

  return (
    <div className="space-y-3">
      <div className="flex gap-2">
        <input
          value={url}
          onChange={e => setUrl(e.target.value)}
          onKeyDown={e => e.key === "Enter" && submit()}
          placeholder="Paste a video or channel link…"
          aria-label="YouTube video or channel URL"
          inputMode="url"
          autoCapitalize="off"
          autoCorrect="off"
          className="flex-1 bg-gray-800 text-white placeholder-gray-500 rounded-xl px-3 min-h-[44px] text-sm focus:outline-none focus:ring-2 ring-indigo-500"
        />
        <button
          onClick={submit}
          disabled={busy || !url.trim()}
          className="bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 px-4 rounded-xl font-medium text-sm min-h-[44px]"
        >
          {busy
            ? <span className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
            : "Add"}
        </button>
      </div>

      {error && <p className="text-red-400 text-sm">{error}</p>}

      {added && (
        <div className="bg-gray-900 rounded-lg p-4 flex gap-4 items-start">
          {added.image_url
            ? <img src={added.image_url} className="w-24 h-14 rounded object-cover flex-shrink-0" alt="" />
            : <div className="w-24 h-14 rounded bg-gray-800 flex-shrink-0 flex items-center justify-center text-xl">▶</div>}
          <div className="flex-1 min-w-0">
            <div className="font-semibold text-sm line-clamp-2">{added.title}</div>
            <div className="text-gray-400 text-xs mt-0.5">
              {added.kind === "channel"
                ? (imported === null
                    ? "Subscribed · importing recent videos…"
                    : `Subscribed · ${imported} video${imported === 1 ? "" : "s"} imported`)
                : [added.channel, fmtDuration(added.duration_seconds)].filter(Boolean).join(" · ")}
            </div>
            <div className={`text-xs mt-1 ${status === "error" ? "text-amber-400" : "text-indigo-400"} ${added.kind === "channel" ? "hidden" : ""}`}>
              {status !== "done" && status !== "error" && (
                <span className="inline-block w-3 h-3 mr-1.5 align-[-1px] border-2 border-indigo-400 border-t-transparent rounded-full animate-spin" />
              )}
              {statusLabel[status] ?? ""}
              {added.already_added && " · already in your library"}
            </div>
            <button
              onClick={() => nav(added.kind === "channel"
                ? `/subscriptions/${added.podcast_id}`
                : `/player/${added.episode_id}`)}
              className="mt-2 bg-indigo-700 hover:bg-indigo-600 text-white text-xs px-3 py-1.5 rounded font-medium"
            >
              {added.kind === "channel" ? "Open channel →" : "Open episode →"}
            </button>
          </div>
        </div>
      )}

      {!added && !error && (
        <p className="text-gray-600 text-xs">
          A video joins your feed as an episode. A channel link subscribes to it, so new
          uploads arrive nightly like a podcast — regular videos only, no Shorts or live
          streams. Captions are used where they exist, so a subscription costs no disk
          until you play something.
        </p>
      )}
    </div>
  );
}

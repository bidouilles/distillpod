import { useEffect, useState } from "react";
import { getTranscriptStatus, retryTranscription, type TranscriptionStatus as Status } from "../api/client";

export function useTranscription(episodeId?: string) {
  const [data, setData] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [retrying, setRetrying] = useState(false);
  useEffect(() => {
    setData(null);
    setError("");
    if (!episodeId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      try {
        const next = await getTranscriptStatus(episodeId);
        if (!cancelled) { setData(next); setError(""); }
      } catch {
        if (!cancelled) setError("Could not refresh transcription status.");
      }
      if (!cancelled) timer = setTimeout(read, 3000);
    };
    read();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [episodeId]);
  const retry = async (captionsOnly = false) => {
    if (!episodeId || retrying) return;
    setRetrying(true);
    setError("");
    try {
      const next = await retryTranscription(episodeId, captionsOnly);
      setData({ status: next.status, progress_percent: null,
        stage: captionsOnly ? "Waiting to check YouTube captions" : "Waiting for transcription", error: null });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not retry transcription.");
    } finally { setRetrying(false); }
  };
  return { data, error, retrying, retry, isYouTube: !!episodeId?.startsWith("yt-") };
}

export default function TranscriptionStatus({ state, onOpen }: {
  state: ReturnType<typeof useTranscription>; onOpen?: () => void;
}) {
  const { data, error, retrying, retry, isYouTube } = state;
  if (!data && !error) return null;
  const active = data?.status === "processing" || data?.status === "queued";
  const percent = data?.progress_percent;
  const checkingCaptions = !!data?.stage?.toLowerCase().includes("youtube caption");
  const title = data?.status === "done" ? "Transcript ready"
    : checkingCaptions && data?.status === "error" ? "Caption check failed"
    : checkingCaptions && active ? "Checking YouTube captions"
    : data?.status === "error" ? "Transcription failed"
    : data?.status === "queued" ? "Transcription queued"
    : data?.status === "processing" ? "Transcribing"
    : "No transcript yet";
  return (
    <section aria-label="Transcription status" className="w-full rounded-xl bg-gray-900 px-4 py-3 text-left text-xs space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className={data?.status === "error" ? "font-medium text-red-300" : "font-medium text-gray-200"}>
          {active && <span className="inline-block mr-2 h-2 w-2 rounded-full bg-indigo-400 animate-pulse" />}
          {title}{active && percent != null ? ` · ${Math.floor(percent)}%` : ""}
        </span>
        {data?.status === "done" && onOpen && <button onClick={onOpen} className="text-green-300 hover:text-green-200">Read along</button>}
        {!active && data?.status !== "done" && <div className="flex flex-wrap gap-2">
          {isYouTube && <button onClick={() => retry(true)} disabled={retrying}
            className="rounded-lg bg-indigo-600 px-3 py-2 font-semibold text-white disabled:opacity-50">
            {retrying ? "Starting…" : "Check YouTube captions"}
          </button>}
          <button onClick={() => retry()} disabled={retrying}
            className={`rounded-lg px-3 py-2 font-semibold text-white disabled:opacity-50 ${isYouTube ? "bg-gray-700" : "bg-indigo-600"}`}>
            {retrying ? "Starting…" : isYouTube ? "Transcribe audio locally" : data?.status === "error" ? "Retry transcription" : "Create transcript"}
          </button>
        </div>}
      </div>
      {data?.stage && data.status !== "done" && <p className="text-gray-400">{data.stage}</p>}
      {active && percent != null && <div role="progressbar" aria-label="Transcription progress"
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.floor(percent)}
        className="h-1.5 overflow-hidden rounded-full bg-gray-700">
        <div className="h-full bg-indigo-400 transition-[width]" style={{ width: `${percent}%` }} />
      </div>}
      {(data?.error || error) && <p role="alert" className="text-red-300 break-words leading-relaxed">{data?.error || error}</p>}
    </section>
  );
}

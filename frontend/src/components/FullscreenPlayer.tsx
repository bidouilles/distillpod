import { useEffect, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import LiveTranscript from "./LiveTranscript";
import { setActiveSource, useAudio } from "../context/AudioContext";
import { useSaved } from "../stores/savedStore";
import { fmtSaved, toCut, toOriginal } from "../lib/timeline";
import {
  getTranscriptStatus, getAdFreeStatus, getChapters, createGist,
  adFreeAudioUrl, bookmarkMoment,
  type AdFreeStatus, type ChaptersResult,
} from "../api/client";

// ─── Constants ─────────────────────────────────────────────────────────────────
const SPEEDS = [1, 1.2, 1.5, 1.8, 2, 0.5];

/** Sleep timer choices, in minutes, plus "this episode". */
const SLEEP_CHOICES = [15, 30, 45, 60];

function fmtTime(secs: number) {
  if (!isFinite(secs) || isNaN(secs)) return "0:00";
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}

// ─── Transcript badge ──────────────────────────────────────────────────────────
function TranscriptBadge({ status, onOpen }: { status: string; onOpen: () => void }) {
  // A ready transcript is the entry point to reading along, so the badge that
  // announces it is the control that opens it — no second affordance competing
  // for room next to the playback controls.
  if (status === "done") return (
    <button
      onClick={onOpen}
      className="inline-flex items-center gap-1.5 text-xs bg-white/10 hover:bg-white/20 text-green-300 px-3 py-1.5 rounded-full transition-colors"
    >
      ✓ Read along
    </button>
  );
  if (status === "error") return (
    <span className="inline-flex items-center gap-1 text-xs bg-white/10 text-red-300 px-2.5 py-1 rounded-full">
      ✗ Transcript error
    </span>
  );
  if (status === "processing" || status === "queued") return (
    <span className="inline-flex items-center gap-2 text-xs bg-white/10 text-yellow-300 px-2.5 py-1 rounded-full">
      <span className="w-1.5 h-1.5 bg-yellow-400 rounded-full animate-pulse inline-block" />
      Transcribing…
    </span>
  );
  return null;
}

// ─── Main component ────────────────────────────────────────────────────────────
export default function FullscreenPlayer() {
  const {
    episode, audioRef, isPlaying, currentTime, duration,
    audioReady, togglePlay, skipBy, rate, setRate, settings,
    sleepMode, sleepRemaining, setSleepTimer,
    playerExpanded, setPlayerExpanded,
  } = useAudio();

  const [sleepOpen, setSleepOpen]         = useState(false);
  // Tell the screens that list these things; they are siblings of this one.
  const distillSaved  = useSaved(s => s.distillSaved);
  const bookmarkSaved = useSaved(s => s.bookmarkSaved);
  // Episode-specific data
  const [transcriptStatus, setTranscriptStatus] = useState("none");
  const [adFreeStatus, setAdFreeStatus]   = useState<AdFreeStatus | null>(null);
  const [useAdFree, setUseAdFree]         = useState(false);
  const [chaptersData, setChaptersData]   = useState<ChaptersResult | null>(null);
  const [chaptersOpen, setChaptersOpen]   = useState(false);
  const [transcriptOpen, setTranscriptOpen] = useState(false);
  const nav = useNavigate();
  // Gist state
  const [gisting, setGisting]             = useState(false);
  const [gistFlash, setGistFlash]         = useState(false);
  const [gistCreated, setGistCreated]     = useState(false);
  // Bookmark state. Separate from the distill flash because the two are
  // different promises: one returns in a millisecond, the other in ~30s.
  const [marking, setMarking]             = useState(false);
  const [markCount, setMarkCount]         = useState(0);
  // Error
  const [error, setError]                 = useState("");
  // Swipe gesture
  const touchStartY                       = useRef(0);
  const pollRef                           = useRef<ReturnType<typeof setInterval> | null>(null);

  // The clean cut runs on its own clock, so everything recorded against the
  // original — chapters, transcript timings, distills, bookmarks — is converted
  // at this boundary. `segments` is null whenever the original is playing, and
  // both helpers are then the identity.
  const segments = useAdFree ? adFreeStatus?.segments ?? null : null;
  const inOriginal = (t: number) => toOriginal(segments, t);
  const inThisFile = (t: number) => toCut(segments, t);

  // Keep the rest of the app in step: progress is stored in the original
  // timeline, and it is saved from an audio event listener that cannot see this
  // component's state.
  useEffect(() => {
    setActiveSource(segments ? "clean" : "original", segments);
  }, [segments]);

  const chapters           = chaptersData?.chapters ?? [];
  const currentChapterIndex = chapters.reduce((best, ch, i) =>
    ch.start_time <= inOriginal(currentTime) ? i : best, -1);
  const currentChapter = currentChapterIndex >= 0 ? chapters[currentChapterIndex] : null;
  const progress       = duration > 0 ? (currentTime / duration) * 100 : 0;
  const remaining      = duration - currentTime;

  // ── History integration: back gesture closes the player ─────────────────────
  // Push a sentinel history entry when the player opens so the browser back
  // gesture / button pops it rather than navigating away from the episode page.
  useEffect(() => {
    if (playerExpanded) {
      window.history.pushState({ distillpodPlayer: true }, '');
    }
  }, [playerExpanded]);

  useEffect(() => {
    const onPopState = () => {
      if (playerExpanded) {
        setPlayerExpanded(false);
        setChaptersOpen(false);
        setTranscriptOpen(false);
      }
    };
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, [playerExpanded, setPlayerExpanded]);

  // ── Fetch data when episode changes ─────────────────────────────────────────
  useEffect(() => {
    if (!episode?.id) return;
    const id = episode.id;

    setTranscriptStatus("none");
    setAdFreeStatus(null);
    // Reset the source choice with the episode. Left set, it applied to the
    // next episode too: its toggle rendered as "Ad-free" the moment its status
    // arrived, and the swap effect then pointed the player at a cut that may
    // not exist. A per-podcast `prefer_adfree` is re-applied below.
    setUseAdFree(false);
    setChaptersData(null);
    setChaptersOpen(false);
    setTranscriptOpen(false);
    setError("");
    setGistCreated(false);
    setMarkCount(0);
    setSleepOpen(false);

    getTranscriptStatus(id).then(({ status }) => setTranscriptStatus(status)).catch(() => {});
    getAdFreeStatus(id).then(setAdFreeStatus).catch(() => {});
    getChapters(id).then(setChaptersData).catch(() => {});
  }, [episode?.id]);

  // ── Poll transcript until done ───────────────────────────────────────────────
  useEffect(() => {
    if (pollRef.current) clearTimeout(pollRef.current);
    if (!episode?.id || transcriptStatus === "done" || transcriptStatus === "error") return;

    // Bug 7: Exponential backoff — start at 5s, back off up to 30s
    const episodeId = episode.id; // capture to avoid stale closure
    let delay = 5000;

    const poll = async () => {
      try {
        const { status } = await getTranscriptStatus(episodeId);
        setTranscriptStatus(status);
        if (status === "done" || status === "error") return;
      } catch {}
      delay = Math.min(delay * 1.5, 30000);
      pollRef.current = setTimeout(poll, delay) as unknown as ReturnType<typeof setInterval>;
    };

    pollRef.current = setTimeout(poll, delay) as unknown as ReturnType<typeof setInterval>;
    return () => { if (pollRef.current) clearTimeout(pollRef.current); };
  }, [episode?.id, transcriptStatus]);

  // A show can ask for the ad-free cut by default, so the toggle starts where
  // its settings say rather than always on the original. Waits for the cut to
  // actually exist — ad detection runs after transcription.
  useEffect(() => {
    if (adFreeStatus?.has_adfree && settings.prefer_adfree) setUseAdFree(true);
  }, [adFreeStatus?.has_adfree, settings.prefer_adfree, episode?.id]);

  // ── Ad-free source swap ──────────────────────────────────────────────────────
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio || !episode?.id || !adFreeStatus?.has_adfree) return;
    const newSrc = useAdFree
      ? adFreeAudioUrl(episode.id)
      : `/player/audio/${episode.id}`;
    if (audio.src.endsWith(newSrc)) return;
    const wasPlaying = !audio.paused;
    // Carry the position across the two clocks rather than the number across
    // the two files: switching to the cut at 2:40 of the original should
    // continue where the listener was, not jump back by the ads removed.
    const cuts = adFreeStatus?.segments ?? null;
    const savedTime = useAdFree
      ? toCut(cuts, audio.currentTime)              // original -> clean
      : toOriginal(cuts, audio.currentTime);        // clean -> original
    audio.src = newSrc;
    audio.load();
    // Bug 4: Wait for loadedmetadata before seeking — synchronous seek is ignored
    const onReady = () => {
      audio.currentTime = savedTime;
      if (wasPlaying) audio.play().catch(() => {});
      audio.removeEventListener("loadedmetadata", onReady);
    };
    audio.addEventListener("loadedmetadata", onReady);
  }, [useAdFree, episode?.id, adFreeStatus?.has_adfree]); // Bug 4: added missing deps

  // ── Handlers ─────────────────────────────────────────────────────────────────
  const cycleSpeed = () => {
    const at = SPEEDS.indexOf(rate);
    setRate(SPEEDS[(at + 1) % SPEEDS.length] ?? 1);
  };

  const handleSeek = (e: React.ChangeEvent<HTMLInputElement>) => {
    const audio = audioRef.current;
    if (audio) audio.currentTime = Number(e.target.value);
  };

  const skipToNextChapter = () => {
    const audio = audioRef.current;
    if (!audio || currentChapterIndex < 0) return;
    const next = chapters[currentChapterIndex + 1];
    if (next) audio.currentTime = inThisFile(next.start_time);
  };

  const handleGist = async () => {
    if (!audioRef.current || !episode?.id) return;
    setGisting(true);
    setError("");
    try {
      await createGist(
        episode.id, audioRef.current.currentTime, segments ? "clean" : "original",
      );
      distillSaved();
      setGistFlash(true);
      setGistCreated(true);
      setTimeout(() => setGistFlash(false), 800);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setGisting(false);
    }
  };

  // Keeping a quote, without a model call. The whole point is that this can be
  // tapped six times on a drive: ⚗️ costs ~30s of waiting, this costs an INSERT,
  // so the two need different buttons rather than one with a mode.
  const handleBookmark = async () => {
    if (!audioRef.current || !episode?.id || marking) return;
    setMarking(true);
    setError("");
    try {
      await bookmarkMoment(
        episode.id, audioRef.current.currentTime, segments ? "clean" : "original",
      );
      bookmarkSaved();
      setMarkCount(n => n + 1);
    } catch (e: any) {
      setError(
        transcriptStatus === "done"
          ? "Nothing transcribed around that moment"
          : "Bookmarks need the transcript — still working on it",
      );
    } finally {
      setMarking(false);
    }
  };

  // Collapsing the player returns you to whatever screen you were on, which is
  // often not the episode's. Without this there was no route from a playing
  // episode to its own page — its summary, chat, distills and export — once you
  // had navigated away.
  const openEpisodePage = () => {
    if (!episode?.id) return;
    setChaptersOpen(false);
    setTranscriptOpen(false);
    setPlayerExpanded(false);
    // Deliberately not handleClose(): that pops the sentinel history entry with
    // history.back(), which is asynchronous, so the pending back landed after
    // this navigation and undid it — the panel closed and nothing moved.
    // Replacing consumes the sentinel instead of racing it, and leaves back
    // pointing at whatever screen the player was raised from.
    nav(`/player/${episode.id}`, {
      replace: !!window.history.state?.distillpodPlayer,
      state: { podcast_image: episode.podcast_image, podcast_title: episode.podcast_title },
    });
  };

  const handleClose = () => {
    setChaptersOpen(false);
    setPlayerExpanded(false);
    // Pop the sentinel history entry we pushed on open, so the history stack
    // stays clean after a manual close (handle tap, swipe down, etc.)
    if (window.history.state?.distillpodPlayer) {
      window.history.back();
    }
  };

  // ── Swipe down to close ───────────────────────────────────────────────────────
  const handleTouchStart = (e: React.TouchEvent) => {
    touchStartY.current = e.touches[0].clientY;
  };
  const handleTouchEnd = (e: React.TouchEvent) => {
    const delta = e.changedTouches[0].clientY - touchStartY.current;
    if (delta > 72) handleClose();
  };

  if (!episode || !audioReady) return null;

  return (
    /* Main sheet — fixed full-screen */
    <div
      className={`fixed inset-0 z-[60] flex flex-col overflow-hidden transition-transform duration-300 ease-out ${
        playerExpanded ? "translate-y-0" : "translate-y-full pointer-events-none"
      }`}
    >
      {/* ── Blurred artwork background ── */}
      <div className="absolute inset-0 overflow-hidden">
        {episode.podcast_image ? (
          <img
            src={episode.podcast_image}
            className="absolute inset-0 w-full h-full object-cover scale-110"
            style={{ filter: "blur(48px) brightness(0.25) saturate(1.4)" }}
            alt=""
          />
        ) : (
          <div className="absolute inset-0 bg-gray-950" />
        )}
        <div className="absolute inset-0 bg-black/50" />
      </div>

      {/* ── Scrollable player content ── */}
      <div
        className="relative flex flex-col h-full overflow-y-auto"
        style={{ paddingTop: "max(env(safe-area-inset-top), 12px)", paddingBottom: "max(env(safe-area-inset-bottom), 24px)" }}
      >
          {/* Swipe handle + chevron */}
          <div
            className="flex flex-col items-center gap-1.5 pt-1 pb-4 cursor-pointer select-none"
            onTouchStart={handleTouchStart}
            onTouchEnd={handleTouchEnd}
            onClick={handleClose}
          >
            <div className="w-10 h-1 bg-white/25 rounded-full" />
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5}
              strokeLinecap="round" strokeLinejoin="round"
              className="w-4 h-4 text-white/30">
              <polyline points="6 9 12 15 18 9" />
            </svg>
          </div>

          <div className="flex flex-col flex-1 px-6 gap-4">
            {/* ── Large artwork ── */}
            <div className="flex justify-center">
              {episode.podcast_image ? (
                <img
                  src={episode.podcast_image}
                  alt=""
                  className={`rounded-2xl object-cover shadow-2xl transition-all duration-500 ${
                    isPlaying
                      ? "w-64 h-64 shadow-indigo-900/30"
                      : "w-52 h-52 opacity-80"
                  }`}
                />
              ) : (
                <div className={`rounded-2xl bg-gray-800 flex items-center justify-center shadow-2xl transition-all duration-500 ${
                  isPlaying ? "w-64 h-64" : "w-52 h-52"
                }`}>
                  <span className="text-6xl">🎧</span>
                </div>
              )}
            </div>

            {/* ── Episode info — and the way to its page ── */}
            <button
              onClick={openEpisodePage}
              aria-label="Open episode details"
              className="text-center px-2 w-full active:opacity-70 transition-opacity"
            >
              {episode.podcast_title && (
                <p className="text-xs text-white/40 uppercase tracking-widest font-semibold mb-1">
                  {episode.podcast_title}
                </p>
              )}
              <h2 className="text-base font-bold text-white leading-snug line-clamp-2">
                {episode.title}
              </h2>
              {/* Spelled out rather than left as a tappable title: there is no
                  hover on a phone, so an invisible target is no target. */}
              <span className="mt-1.5 inline-flex items-center gap-1 text-[11px] text-white/40">
                Episode details
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5}
                     strokeLinecap="round" strokeLinejoin="round" className="w-3 h-3">
                  <polyline points="9 18 15 12 9 6" />
                </svg>
              </span>
            </button>

            {/* ── Error ── */}
            {error && (
              <div className="bg-red-500/20 border border-red-500/30 text-red-300 rounded-xl px-4 py-2 text-xs text-center">
                {error}
              </div>
            )}

            {/* ── Progress scrubber ── */}
            <div className="space-y-1.5">
              <div className="relative h-1 group cursor-pointer">
                {/* Track */}
                <div className="absolute inset-0 rounded-full bg-white/20" />
                {/* Fill */}
                <div
                  className="absolute left-0 top-0 h-full rounded-full bg-white pointer-events-none"
                  style={{ width: `${progress}%` }}
                />
                {/* Thumb (visible on hover/active) */}
                <div
                  className="absolute top-1/2 -translate-y-1/2 w-3 h-3 bg-white rounded-full shadow pointer-events-none opacity-0 group-hover:opacity-100 transition-opacity"
                  style={{ left: `calc(${progress}% - 6px)` }}
                />
                <input
                  type="range" min={0} max={duration || 100} step={1} value={currentTime}
                  onChange={handleSeek}
                  className="absolute inset-0 w-full opacity-0 cursor-pointer h-full"
                />
              </div>
              <div className="flex justify-between text-xs text-white/40 font-mono px-0.5">
                <span>{fmtTime(currentTime)}</span>
                <span>−{fmtTime(remaining)}</span>
              </div>
            </div>

            {/* ── Playback controls ── */}
            <div className="flex items-center justify-between px-2">
              {/* Speed */}
              <button
                onClick={cycleSpeed}
                aria-label={`Playback speed ${rate}x`}
                className="w-10 h-10 flex items-center justify-center text-white/60 hover:text-white text-sm font-bold rounded-full hover:bg-white/10 transition-colors"
              >
                {rate === 1 ? "1×" : `${rate}×`}
              </button>

              {/* Skip back 10s */}
              <button
                onClick={() => skipBy(-10)}
                className="w-12 h-12 flex items-center justify-center text-white/70 hover:text-white rounded-full hover:bg-white/10 transition-colors"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}
                  strokeLinecap="round" strokeLinejoin="round" className="w-8 h-8">
                  <path d="M2.5 12a9.5 9.5 0 1 1 2.3 6.2" />
                  <path d="M2.5 7v5h5" />
                  <text x="7.5" y="15" fontSize="6" fill="currentColor" stroke="none" fontWeight="bold">10</text>
                </svg>
              </button>

              {/* Play / Pause */}
              <button
                onClick={togglePlay}
                className="w-20 h-20 bg-white hover:bg-white/90 active:scale-95 rounded-full flex items-center justify-center transition-all shadow-2xl"
              >
                {isPlaying ? (
                  <svg viewBox="0 0 24 24" fill="#111827" className="w-8 h-8">
                    <rect x="6" y="4" width="4" height="16" rx="1" />
                    <rect x="14" y="4" width="4" height="16" rx="1" />
                  </svg>
                ) : (
                  <svg viewBox="0 0 24 24" fill="#111827" className="w-8 h-8 translate-x-0.5">
                    <polygon points="5 3 19 12 5 21 5 3" />
                  </svg>
                )}
              </button>

              {/* Skip forward 30s */}
              <button
                onClick={() => skipBy(30)}
                className="w-12 h-12 flex items-center justify-center text-white/70 hover:text-white rounded-full hover:bg-white/10 transition-colors"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8}
                  strokeLinecap="round" strokeLinejoin="round" className="w-8 h-8">
                  <path d="M21.5 12a9.5 9.5 0 1 0-2.3 6.2" />
                  <path d="M21.5 7v5h-5" />
                  <text x="6.5" y="15" fontSize="6" fill="currentColor" stroke="none" fontWeight="bold">30</text>
                </svg>
              </button>

              {/* Sleep timer. Occupies what used to be a spacer holding a
                  ⚗️ tick — the same information now lives on the Distill
                  button itself, and bedtime listening had no control at all. */}
              <button
                onClick={() => setSleepOpen(o => !o)}
                aria-label="Sleep timer"
                className={`w-10 h-10 flex items-center justify-center rounded-full transition-colors ${
                  sleepMode !== "off"
                    ? "text-indigo-300 bg-indigo-500/20"
                    : "text-white/60 hover:text-white hover:bg-white/10"
                }`}
              >
                {sleepMode === "time" && sleepRemaining != null ? (
                  <span className="text-[11px] font-bold font-mono">
                    {Math.ceil(sleepRemaining / 60)}m
                  </span>
                ) : (
                  <span className="text-base">{sleepMode === "episode" ? "🌙" : "☾"}</span>
                )}
              </button>
            </div>

            {/* ── Keep this moment: the cheap way and the expensive way ──
                Bookmark first, because it is the one that can be tapped
                repeatedly: it stores the sentence being spoken and returns at
                once. Distill sends the same moment to the agent CLI for a quote
                and an insight, which is worth the ~30s only now and then. */}
            <div className="flex gap-2">
              <button
                onClick={handleBookmark}
                disabled={marking || transcriptStatus !== "done"}
                className={`flex-1 py-3.5 rounded-2xl font-semibold text-sm transition-all active:scale-[0.98] ${
                  transcriptStatus === "done"
                    ? "bg-yellow-500/20 hover:bg-yellow-500/30 text-yellow-200"
                    : "bg-white/5 text-white/30 cursor-not-allowed"
                }`}
              >
                {marking ? "Keeping…" : markCount > 0 ? `🔖  Kept ${markCount}` : "🔖  Bookmark"}
              </button>
              <button
                onClick={handleGist}
                disabled={gisting || transcriptStatus !== "done"}
                className={`flex-1 py-3.5 rounded-2xl font-semibold text-sm transition-all active:scale-[0.98] ${
                  gistFlash
                    ? "bg-green-500/70 text-white scale-[0.98]"
                    : transcriptStatus === "done"
                      ? "bg-white/15 hover:bg-white/20 text-white"
                      : "bg-white/5 text-white/30 cursor-not-allowed"
                }`}
              >
                {gisting
                  ? "Distilling…"
                  : gistCreated
                    ? "⚗️  Distilled"
                    : transcriptStatus === "done"
                      ? "⚗️  Distill"
                      : transcriptStatus === "processing" || transcriptStatus === "queued"
                        ? "⏳  Transcribing…"
                        : "⏳  No transcript"}
              </button>
            </div>

            {/* ── Clean cut toggle ──
                Labelled by what was actually done to this episode: ads out,
                pauses shortened, or both. "Ad-free" would be a lie on a podcast
                that has no ads and was only trimmed. */}
            {adFreeStatus?.has_adfree && (
              <div className="flex items-center justify-center gap-3">
                <span className="text-xs text-white/30">
                  {[
                    adFreeStatus.ads_count > 0
                      && `${adFreeStatus.ads_count} ad${adFreeStatus.ads_count !== 1 ? "s" : ""}`,
                    adFreeStatus.trimmed_seconds >= 1
                      && `${fmtSaved(adFreeStatus.trimmed_seconds)} of pauses`,
                  ].filter(Boolean).join(" · ") || "cleaned"} removed
                </span>
                <div className="flex rounded-full bg-white/10 p-0.5">
                  <button
                    onClick={() => setUseAdFree(false)}
                    className={`px-3 py-1 rounded-full text-xs font-medium transition-all ${
                      !useAdFree ? "bg-white text-gray-900 shadow" : "text-white/50"
                    }`}
                  >Original</button>
                  <button
                    onClick={() => setUseAdFree(true)}
                    className={`px-3 py-1 rounded-full text-xs font-medium transition-all ${
                      useAdFree ? "bg-white text-gray-900 shadow" : "text-white/50"
                    }`}
                  >
                    {adFreeStatus.ads_count > 0 && adFreeStatus.trimmed_seconds >= 1
                      ? "Clean ✂️"
                      : adFreeStatus.ads_count > 0 ? "Ad-free ✂️" : "Trimmed ✂️"}
                  </button>
                </div>
              </div>
            )}

            {/* ── Current chapter + skip ── */}
            {currentChapter && (
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setChaptersOpen(o => !o)}
                  className="flex-1 text-left text-xs text-white/50 hover:text-white/80 truncate transition-colors"
                >
                  § {currentChapter.title}
                </button>
                {currentChapterIndex < chapters.length - 1 && (
                  <button
                    onClick={skipToNextChapter}
                    className="flex-shrink-0 text-xs text-white/40 hover:text-white px-2.5 py-1 rounded-full bg-white/10 hover:bg-white/15 transition-colors"
                  >
                    Next §
                  </button>
                )}
              </div>
            )}
            {chapters.length > 0 && !currentChapter && (
              <button
                onClick={() => setChaptersOpen(o => !o)}
                className="text-xs text-white/40 hover:text-white/60 transition-colors text-left"
              >
                § {chapters.length} chapters
              </button>
            )}

            {/* ── Transcript badge ── */}
            <div className="flex justify-center pb-2">
              <TranscriptBadge status={transcriptStatus} onOpen={() => { setChaptersOpen(false); setTranscriptOpen(true); }} />
            </div>
          </div>
        </div>

        {/* ── Sleep timer sheet ── */}
        {sleepOpen && (
          <div className="absolute inset-0 z-10 bg-black/50" onClick={() => setSleepOpen(false)} />
        )}
        <div
          className={`absolute inset-x-0 bottom-0 z-20 bg-gray-950 rounded-t-3xl transition-all duration-300 ease-out ${
            sleepOpen ? "translate-y-0" : "translate-y-full opacity-0 pointer-events-none"
          }`}
          style={{ paddingBottom: "max(env(safe-area-inset-bottom), 16px)" }}
        >
          <div className="flex justify-center pt-3 pb-1">
            <div className="w-10 h-1 bg-white/20 rounded-full" />
          </div>
          <div className="flex items-center justify-between px-5 py-3 border-b border-gray-800/60">
            <div>
              <div className="text-sm font-bold text-white">Sleep timer</div>
              <div className="text-[11px] text-white/40">
                {sleepMode === "time" && sleepRemaining != null
                  ? `Fading out in ${fmtTime(sleepRemaining)}`
                  : sleepMode === "episode"
                    ? "Stopping at the end of this episode"
                    : "Fades out and pauses — the queue does not carry on"}
              </div>
            </div>
            <button
              onClick={() => setSleepOpen(false)}
              aria-label="Close sleep timer"
              className="-mr-2 w-11 h-11 flex items-center justify-center text-gray-400 hover:text-white"
            >
              <span className="w-7 h-7 flex items-center justify-center rounded-full bg-gray-800 text-lg leading-none">×</span>
            </button>
          </div>
          <div className="px-5 py-4 flex flex-wrap gap-2">
            {SLEEP_CHOICES.map(mins => (
              <button
                key={mins}
                onClick={() => { setSleepTimer(mins); setSleepOpen(false); }}
                className="min-h-[44px] px-4 rounded-full bg-gray-800 hover:bg-gray-700 text-sm text-gray-200 font-medium transition-colors"
              >
                {mins} min
              </button>
            ))}
            <button
              onClick={() => { setSleepTimer("episode"); setSleepOpen(false); }}
              className={`min-h-[44px] px-4 rounded-full text-sm font-medium transition-colors ${
                sleepMode === "episode"
                  ? "bg-indigo-600 text-white"
                  : "bg-gray-800 hover:bg-gray-700 text-gray-200"
              }`}
            >
              End of episode
            </button>
            {sleepMode !== "off" && (
              <button
                onClick={() => { setSleepTimer(null); setSleepOpen(false); }}
                className="min-h-[44px] px-4 rounded-full bg-red-600/20 hover:bg-red-600/30 text-sm text-red-300 font-medium transition-colors"
              >
                Cancel timer
              </button>
            )}
          </div>
        </div>

        {/* ── Chapters backdrop — inside fixed container, below sheet ── */}
        {chaptersOpen && (
          <div
            className="absolute inset-0 z-10 bg-black/50"
            onClick={() => setChaptersOpen(false)}
          />
        )}

        {/* ── Chapters sheet — outside scroll container, covers all controls ──
            Hidden with opacity and pointer-events as well as a translate.
            `translate-y-full` shifts a sheet by its own height, which clears
            the screen only while this root is unscrolled; with few or no
            chapters this one is a ~90px header, so it has very little room to
            spare. Cheap insurance against it becoming a dead bar over the
            bottom nav. */}
        <div
          className={`absolute inset-x-0 bottom-0 z-20 bg-gray-950 rounded-t-3xl transition-all duration-300 ease-out max-h-[70vh] flex flex-col ${
            chaptersOpen ? "translate-y-0" : "translate-y-full opacity-0 pointer-events-none"
          }`}
          style={{ paddingBottom: "max(env(safe-area-inset-bottom), 16px)" }}
        >
          {/* Drag handle */}
          <div className="flex justify-center pt-3 pb-1 flex-shrink-0">
            <div className="w-10 h-1 bg-white/20 rounded-full" />
          </div>
          <div className="flex items-center justify-between px-5 py-3 border-b border-gray-800/60 flex-shrink-0">
            <span className="text-sm font-bold text-white">Chapters ({chapters.length})</span>
            <button
              onClick={() => setChaptersOpen(false)}
              aria-label="Close chapters"
              className="-mr-2 w-11 h-11 flex items-center justify-center text-gray-400 hover:text-white transition-colors"
            >
              <span className="w-7 h-7 flex items-center justify-center rounded-full bg-gray-800 text-lg leading-none">×</span>
            </button>
          </div>
          <div className="overflow-y-auto divide-y divide-gray-800/40">
            {chapters.map((ch, i) => (
              <button
                key={i}
                onClick={() => {
                  const audio = audioRef.current;
                  if (audio) audio.currentTime = inThisFile(ch.start_time);
                  setChaptersOpen(false);
                }}
                className={`w-full flex items-center gap-3 px-5 py-3.5 text-left hover:bg-gray-800/50 transition-colors ${
                  i === currentChapterIndex ? "bg-indigo-900/20" : ""
                }`}
              >
                <span className="text-xs font-mono text-gray-500 w-10 flex-shrink-0">
                  {fmtTime(ch.start_time)}
                </span>
                <span className={`text-sm flex-1 leading-snug ${
                  i === currentChapterIndex ? "text-indigo-300 font-semibold" : "text-gray-300"
                }`}>
                  {i === currentChapterIndex && <span className="mr-1">▶</span>}
                  {ch.title}
                </span>
              </button>
            ))}
          </div>
        </div>

        {/* ── Transcript backdrop ── */}
        {transcriptOpen && (
          <div
            className="absolute inset-0 z-10 bg-black/60"
            onClick={() => setTranscriptOpen(false)}
          />
        )}

        {/* ── Read-along transcript ──
            Taller than the chapters sheet: this one is meant to be read for
            minutes at a time, not glanced at and dismissed. */}
        <div
          className={`absolute inset-x-0 bottom-0 z-20 bg-gray-950 rounded-t-3xl transition-all duration-300 ease-out h-[85vh] flex flex-col ${
            transcriptOpen ? "translate-y-0" : "translate-y-full opacity-0 pointer-events-none"
          }`}
          style={{ paddingBottom: "max(env(safe-area-inset-bottom), 8px)" }}
        >
          <div className="flex justify-center pt-3 pb-1 flex-shrink-0">
            <div className="w-10 h-1 bg-white/20 rounded-full" />
          </div>
          <div className="flex items-center justify-between px-5 py-3 border-b border-gray-800/60 flex-shrink-0">
            <div className="min-w-0">
              <div className="text-sm font-bold text-white">Transcript</div>
              <div className="text-[11px] text-white/40">Tap a line to jump · hold to bookmark</div>
            </div>
            <button
              onClick={() => setTranscriptOpen(false)}
              aria-label="Close transcript"
              className="-mr-2 w-11 h-11 flex-shrink-0 flex items-center justify-center text-gray-400 hover:text-white transition-colors"
            >
              <span className="w-7 h-7 flex items-center justify-center rounded-full bg-gray-800 text-lg leading-none">×</span>
            </button>
          </div>
          {/* Stays mounted across open/close so the transcript is fetched once
              per episode rather than on every reopen — it is ~190KB for a long
              episode, and this is a phone. Nothing runs while closed: the
              fetch waits for the first open and the animation frame loop is
              gated on `open`. */}
          {episode?.id && (
            <LiveTranscript
              episodeId={episode.id}
              audioRef={audioRef}
              open={transcriptOpen}
              toOriginal={inOriginal}
              onSeek={(secs) => { const a = audioRef.current; if (a) a.currentTime = inThisFile(secs); }}
              onBookmarked={() => { bookmarkSaved(); setMarkCount(n => n + 1); }}
            />
          )}
        </div>
      </div>
  );
}

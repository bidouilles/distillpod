import {
  createContext, useContext, useRef, useState, useEffect, useCallback,
  type ReactNode, type RefObject,
} from "react";
import {
  startPlay, getEpisode, audioStreamUrl, getProgress, putProgress,
  getDownloadStatus, type AudioSource, type Episode, type PodcastSettings,
} from "../api/client";
import { toCut, toOriginal, type Segments } from "../lib/timeline";
import { useQueue } from "../stores/queueStore";

// ─── Progress persistence ─────────────────────────────────────────────────────
const PROGRESS_KEY = "distillpod:progress";

export interface ProgressEntry {
  currentTime:    number;
  duration:       number;
  title?:         string;
  podcast_image?: string;
  podcast_title?: string;
  savedAt:        number;
}

export function readProgress(): Record<string, ProgressEntry> {
  try { return JSON.parse(localStorage.getItem(PROGRESS_KEY) || "{}"); } catch { return {}; }
}

/**
 * Which file is playing, and how its clock maps to the original.
 *
 * Module state rather than context: `writeProgress` is called from an audio
 * event listener mounted once, and the alternative is threading the mapping
 * through every save. The player sets it when it swaps the source.
 */
let activeSource: AudioSource = "original";
let activeSegments: Segments | null = null;

export function setActiveSource(source: AudioSource, segments: Segments | null) {
  activeSource = source;
  activeSegments = segments;
}

/** The position to store: always in the original timeline. */
export function positionInOriginal(time: number): number {
  return activeSource === "clean" ? toOriginal(activeSegments, time) : time;
}

/** Where a stored position sits in the file currently playing. */
export function positionInActiveSource(time: number): number {
  return activeSource === "clean" ? toCut(activeSegments, time) : time;
}

function writeProgress(id: string, time: number, dur: number, ep: PlayableEpisode | null) {
  // Bug 6: Use proportional thresholds for short episodes
  // Don't save if nearly finished (within 30s or last 10% for short episodes)
  const endThreshold = dur > 0 ? Math.min(30, dur * 0.1) : 30;
  if (dur > 0 && time > dur - endThreshold) return;
  // Don't save if barely started (under 10s or first 5% for short episodes)
  const startThreshold = dur > 0 ? Math.min(10, dur * 0.05) : 10;
  if (time < startThreshold) return;
  const originalTime = positionInOriginal(time);
  try {
    const map = readProgress();
    map[id] = {
      currentTime:   originalTime,
      duration:      dur,
      title:         ep?.title,
      podcast_image: ep?.podcast_image,
      podcast_title: ep?.podcast_title,
      savedAt:       Date.now(),
    };
    localStorage.setItem(PROGRESS_KEY, JSON.stringify(map));
  } catch {}
  // Sent as-is with the source named, so the server does the translation with
  // the mapping it stored — one implementation for everything persisted.
  sync(id, { position: time, duration: dur, source: activeSource });
}

/** Forget where you were in an episode, without forgetting you opened it.
 *
 *  Resets the position rather than deleting the row: `played` drives the
 *  "unplayed" feed filter, and dismissing a half-finished episode should not
 *  make it look untouched. Syncing a zero also carries the dismissal to your
 *  other devices, where hydration drops the local copy in turn. */
export function clearProgress(id: string) {
  try {
    const map = readProgress();
    delete map[id];
    localStorage.setItem(PROGRESS_KEY, JSON.stringify(map));
  } catch {}
  // Position only. Finishing an episode is what usually clears it, and that
  // must not also erase the fact that it was finished.
  sync(id, { position: 0 });
}

const PLAYED_KEY = "distillpod:played";

export function readPlayed(): Set<string> {
  try { return new Set(JSON.parse(localStorage.getItem(PLAYED_KEY) || "[]")); } catch { return new Set(); }
}

function markPlayed(id: string) {
  try {
    const played = readPlayed();
    played.add(id);
    localStorage.setItem(PLAYED_KEY, JSON.stringify([...played]));
  } catch {}
  sync(id, { played: true });
}

/** Fire and forget. A failed sync must never interrupt playback — the local
 *  copy already holds the value, and the next save will carry it up. */
function sync(
  id: string,
  body: { position?: number; duration?: number; played?: boolean; source?: AudioSource },
) {
  putProgress(id, body).catch(() => {});
}

/**
 * Reconcile this device with the server's copy, once, at startup.
 *
 * Whichever side was written later wins, per episode, so the device you just
 * put down beats the one that has been sitting idle. `played` is merged as a
 * union instead: it only ever goes one way, and a union cannot lose a finish
 * that happened on another device.
 */
async function hydrateProgress(): Promise<void> {
  try {
    const records = await getProgress();
    const map = readProgress();
    const played = readPlayed();

    for (const r of records) {
      if (r.played) played.add(r.episode_id);
      const serverAt = Date.parse(r.updated_at);
      const local = map[r.episode_id];
      if (local && local.savedAt >= serverAt) continue;   // this device is ahead
      // Only a zeroed position means there is nothing to resume. `played` is
      // set when an episode is *opened*, not when it is finished, so testing it
      // here would discard the position of every episode ever started — the
      // resume would silently fall back to 0 on a device that had not played
      // it locally, which is precisely the case this whole feature exists for.
      if (r.position <= 0) { delete map[r.episode_id]; continue; }
      map[r.episode_id] = {
        currentTime:   r.position,
        duration:      r.duration ?? local?.duration ?? 0,
        title:         r.title         ?? local?.title,
        podcast_image: r.podcast_image ?? local?.podcast_image,
        podcast_title: r.podcast_title ?? local?.podcast_title,
        savedAt:       serverAt,
      };
    }
    localStorage.setItem(PROGRESS_KEY, JSON.stringify(map));
    localStorage.setItem(PLAYED_KEY, JSON.stringify([...played]));
  } catch {}
}

// ─── Types ────────────────────────────────────────────────────────────────────
export type PlayableEpisode = Episode & {
  podcast_image?: string;
  podcast_title?: string;
};

/** Off, counting down to a time, or stopping when this episode ends. */
export type SleepMode = "off" | "time" | "episode";

interface AudioContextValue {
  episode:           PlayableEpisode | null;
  audioRef:          RefObject<HTMLAudioElement | null>;
  isPlaying:         boolean;
  currentTime:       number;
  preparing:         string | null;
  duration:          number;
  audioReady:        boolean;
  loadEpisode:       (id: string, ep: PlayableEpisode | null, seekTo?: number) => Promise<void>;
  togglePlay:        () => void;
  seek:              (secs: number) => void;
  skipBy:            (delta: number) => void;
  rate:              number;
  setRate:           (rate: number) => void;
  /** The current podcast's preferences, as returned by /player/play. */
  settings:          PodcastSettings;
  sleepMode:         SleepMode;
  /** Seconds left when counting down to a time; null otherwise. */
  sleepRemaining:    number | null;
  /** Minutes, "episode", or null to cancel. */
  setSleepTimer:     (value: number | "episode" | null) => void;
  playerExpanded:    boolean;
  setPlayerExpanded: (v: boolean) => void;
}

const Ctx = createContext<AudioContextValue | null>(null);

// ─── Provider ─────────────────────────────────────────────────────────────────
export function AudioProvider({ children }: { children: ReactNode }) {
  const audioRef     = useRef<HTMLAudioElement>(null);
  const loadedIdRef  = useRef<string | null>(null); // prevents double-loading same episode
  const episodeRef   = useRef<PlayableEpisode | null>(null); // for access inside event listeners
  const lastSaveRef  = useRef<number>(0);            // throttle: last progress-save timestamp
  const settingsRef  = useRef<PodcastSettings>({});  // for access inside event listeners
  const sleepAtEndRef = useRef(false);
  const outroDoneRef = useRef<string | null>(null);  // episode whose outro was skipped

  const loadEpisodeRef = useRef<AudioContextValue["loadEpisode"] | null>(null);

  // Playback rate lives here rather than in the player sheet: a per-podcast
  // preference has to survive the sheet being closed, and the sheet unmounts.
  const [rate,           setRateState]      = useState(1);
  const [settings,       setSettings]       = useState<PodcastSettings>({});
  const [sleepUntil,     setSleepUntil]     = useState<number | null>(null);
  const [sleepAtEnd,     setSleepAtEnd]     = useState(false);
  const [sleepRemaining, setSleepRemaining] = useState<number | null>(null);

  const [episode,        setEpisode]        = useState<PlayableEpisode | null>(null);
  const [isPlaying,      setIsPlaying]      = useState(false);
  const [currentTime,    setCurrentTime]    = useState(0);
  // Episode id whose audio is being fetched, so the UI can say so.
  const [preparing,      setPreparing]      = useState<string | null>(null);
  const [duration,       setDuration]       = useState(0);
  const [audioReady,     setAudioReady]     = useState(false);
  const [playerExpanded, setPlayerExpanded] = useState(false);

  // Keep episodeRef in sync for use inside event listeners
  useEffect(() => { episodeRef.current = episode; }, [episode]);
  useEffect(() => { settingsRef.current = settings; }, [settings]);
  useEffect(() => { sleepAtEndRef.current = sleepAtEnd; }, [sleepAtEnd]);

  // ── Media Session: update metadata when episode changes ─────────────────
  useEffect(() => {
    if (!episode || !("mediaSession" in navigator)) return;
    // Proxy external artwork through our own domain so Chrome can load it
    const artwork: MediaImage[] = [
      { src: "/icon-512.png", sizes: "512x512", type: "image/png" }, // local fallback
    ];
    if (episode.podcast_image) {
      const proxied = `/proxy/image?url=${encodeURIComponent(episode.podcast_image)}`;
      artwork.unshift(
        { src: proxied, sizes: "512x512", type: "image/jpeg" },
        { src: proxied, sizes: "256x256", type: "image/jpeg" },
      );
    }
    navigator.mediaSession.metadata = new MediaMetadata({
      title:  episode.title         || "Unknown Episode",
      artist: episode.podcast_title  || "DistillPod",
      album:  "DistillPod ⚗️",
      artwork,
    });
  }, [episode]);

  // ── Media Session: action handlers (mounted once — audio element is stable) ─
  useEffect(() => {
    if (!("mediaSession" in navigator)) return;
    const audio = audioRef.current;
    if (!audio) return;

    const handlers: [MediaSessionAction, MediaSessionActionHandler][] = [
      ["play",           ()  => audio.play().catch(() => {})],
      ["pause",          ()  => audio.pause()],
      ["seekbackward",   (d) => { audio.currentTime = Math.max(0, audio.currentTime - (d.seekOffset ?? 10)); }],
      ["seekforward",    (d) => { audio.currentTime = Math.min(audio.duration || Infinity, audio.currentTime + (d.seekOffset ?? 30)); }],
      ["seekto",         (d) => { if (d.seekTime != null) audio.currentTime = d.seekTime; }],
      // previoustrack / nexttrack: shown in compact Android notification as ⏮ ⏭
      ["previoustrack",  ()  => { audio.currentTime = Math.max(0, audio.currentTime - 10); }],
      ["nexttrack",      ()  => { audio.currentTime = Math.min(audio.duration || Infinity, audio.currentTime + 30); }],
    ];
    handlers.forEach(([action, handler]) =>
      navigator.mediaSession.setActionHandler(action, handler)
    );
    return () => handlers.forEach(([action]) =>
      navigator.mediaSession.setActionHandler(action, null)
    );
  }, []);

  // Pull the server's copy in once, before the reader gets far enough to
  // navigate. One small request; a failure just leaves this device on its own
  // local copy, which is exactly the old behaviour.
  useEffect(() => { hydrateProgress(); }, []);

  // Wire up persistent audio event listeners once on mount
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;

    const onTime = () => {
      setCurrentTime(audio.currentTime);
      // Skip outro: a show with a two-minute sign-off every week should not
      // need the same manual skip every week. Seeking to the end lets the
      // ordinary `ended` path run, so auto-advance and progress behave
      // identically to reaching the end on foot.
      const outro = settingsRef.current.skip_outro;
      if (outro && audio.duration > outro && loadedIdRef.current
          && outroDoneRef.current !== loadedIdRef.current
          && audio.currentTime >= audio.duration - outro) {
        outroDoneRef.current = loadedIdRef.current;
        audio.currentTime = audio.duration;
        return;
      }
      // Throttled progress save (every 5 s)
      const now = Date.now();
      if (loadedIdRef.current && now - lastSaveRef.current > 5_000) {
        lastSaveRef.current = now;
        writeProgress(loadedIdRef.current, audio.currentTime, audio.duration || 0, episodeRef.current);
      }
      // Keep lock-screen scrubber in sync
      if ("mediaSession" in navigator && audio.duration > 0) {
        try {
          navigator.mediaSession.setPositionState({
            duration:     audio.duration,
            playbackRate: audio.playbackRate,
            position:     audio.currentTime,
          });
        } catch {}
      }
    };
    const onMeta  = () => setDuration(audio.duration || 0);
    const onPlay  = () => {
      setIsPlaying(true);
      if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "playing";
    };
    const onPause = () => {
      setIsPlaying(false);
      if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "paused";
    };
    const onEnded = () => {
      setIsPlaying(false);
      if ("mediaSession" in navigator) navigator.mediaSession.playbackState = "paused";
      // Episode finished — clear saved progress
      if (loadedIdRef.current) clearProgress(loadedIdRef.current);
      // Sleeping at the end of this episode means exactly that: not the end of
      // the next one the queue would have started.
      if (sleepAtEndRef.current) {
        setSleepAtEnd(false);
        return;
      }
      // Auto-advance: play next item from queue
      const next = useQueue.getState().shift();
      if (next && loadEpisodeRef.current) {
        loadEpisodeRef.current(next.episodeId, {
          id: next.episodeId,
          title: next.title,
          audio_url: next.audioUrl,
          image_url: next.imageUrl,
          podcast_image: next.imageUrl,
          podcast_title: next.podcastTitle,
          podcast_id: "",
          downloaded: false,
          transcript_status: "none",
        } as PlayableEpisode, 0);
      }
    };

    audio.addEventListener("timeupdate",     onTime);
    audio.addEventListener("loadedmetadata", onMeta);
    audio.addEventListener("durationchange", onMeta);
    audio.addEventListener("play",  onPlay);
    audio.addEventListener("pause", onPause);
    audio.addEventListener("ended", onEnded);
    return () => {
      audio.removeEventListener("timeupdate",     onTime);
      audio.removeEventListener("loadedmetadata", onMeta);
      audio.removeEventListener("durationchange", onMeta);
      audio.removeEventListener("play",  onPlay);
      audio.removeEventListener("pause", onPause);
      audio.removeEventListener("ended", onEnded);
    };
  }, []);

  /** Poll until the audio is on disk. Resolves false if it failed. */
  const waitForDownload = useCallback(async (id: string): Promise<boolean> => {
    setPreparing(id);
    try {
      // Generous: a long episode over a slow line takes a while, and the work
      // continues server-side regardless of what this loop does.
      for (let i = 0; i < 600; i++) {
        try {
          const s = await getDownloadStatus(id);
          if (s.downloaded) return true;
          if (s.error) return false;
          if (!s.downloading) return false;   // nothing running and nothing on disk
        } catch { /* transient — keep waiting */ }
        await new Promise(r => setTimeout(r, 1000));
      }
      return false;
    } finally {
      setPreparing(null);
    }
  }, []);

  const loadEpisode = useCallback(async (
    id: string,
    ep: PlayableEpisode | null,
    seekTo?: number,
  ) => {
    const audio = audioRef.current;
    if (!audio) return;

    // Same episode already loaded → just seek if needed, no reload
    if (loadedIdRef.current === id && audioReady) {
      if (seekTo != null) {
        audio.currentTime = seekTo;
        audio.play().catch(() => {});
      }
      return;
    }

    // Resolve episode data if not provided
    let resolved = ep;
    if (!resolved?.audio_url) {
      const savedImage = resolved?.podcast_image;
      const savedTitle = resolved?.podcast_title;
      resolved = await getEpisode(id);
      resolved = { ...resolved, podcast_image: savedImage, podcast_title: savedTitle };
    }

    // Ask for the audio. The download runs server-side now, so this returns
    // straight away and several episodes can be fetched at once; waiting here
    // is only about this one being playable. The wait lives in the provider,
    // which is mounted for the life of the app, so changing screen does not
    // abandon it.
    const started = await startPlay(id, resolved.audio_url);
    if (started.status === "downloading") {
      const ready = await waitForDownload(id);
      if (!ready) return;          // failed — leave the player as it was
    }

    // The podcast's own preferences, applied before the first frame of audio.
    // `null` means "no opinion", so the rate the listener last chose stands.
    const podcastSettings = started.settings ?? {};
    setSettings(podcastSettings);
    settingsRef.current = podcastSettings;
    outroDoneRef.current = null;
    if (podcastSettings.playback_rate) {
      setRateState(podcastSettings.playback_rate);
      audio.playbackRate = podcastSettings.playback_rate;
    }
    // Skipping the intro is only right when starting from the top: resuming
    // half way through an episode, or jumping to a distilled moment, means
    // something else entirely.
    if (seekTo == null && podcastSettings.skip_intro) {
      seekTo = podcastSettings.skip_intro;
    }

    // Swap src. A fresh episode always starts on its original file; the player
    // sets the clean source again if this podcast prefers it.
    setActiveSource("original", null);
    loadedIdRef.current = id;
    setEpisode(resolved);
    setAudioReady(false);
    setCurrentTime(0);
    setDuration(0);

    audio.src = audioStreamUrl(id);
    audio.load();
    setAudioReady(true);

    // Seek + autoplay
    const play = () => audio.play().catch(() => {});
    if (seekTo != null) {
      const doSeek = () => {
        audio.currentTime = seekTo;
        audio.addEventListener("seeked",   play, { once: true });
        audio.addEventListener("canplay",  play, { once: true });
      };
      audio.readyState >= 1
        ? doSeek()
        : audio.addEventListener("loadedmetadata", doSeek, { once: true });
    } else {
      play();
    }

    markPlayed(id);
  }, [audioReady]);

  // Keep ref in sync so onEnded can call loadEpisode
  useEffect(() => { loadEpisodeRef.current = loadEpisode; }, [loadEpisode]);

  const togglePlay = useCallback(() => {
    const audio = audioRef.current;
    if (!audio || !audioReady) return;
    isPlaying ? audio.pause() : audio.play().catch(() => {});
  }, [isPlaying, audioReady]);

  const seek = useCallback((secs: number) => {
    const audio = audioRef.current;
    if (audio) audio.currentTime = secs;
  }, []);

  const skipBy = useCallback((delta: number) => {
    const audio = audioRef.current;
    if (audio) audio.currentTime = Math.max(0, Math.min(audio.currentTime + delta, duration));
  }, [duration]);

  const setRate = useCallback((next: number) => {
    const audio = audioRef.current;
    setRateState(next);
    if (audio) audio.playbackRate = next;
  }, []);

  // Keep the element's rate through a source swap — the ad-free toggle
  // reassigns `src`, and a fresh load resets playbackRate to 1.
  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    const apply = () => { audio.playbackRate = rate; };
    audio.addEventListener("loadedmetadata", apply);
    apply();
    return () => audio.removeEventListener("loadedmetadata", apply);
  }, [rate, episode?.id]);

  const setSleepTimer = useCallback((value: number | "episode" | null) => {
    const audio = audioRef.current;
    if (audio) audio.volume = 1;             // cancel a fade in progress
    if (value === null) {
      setSleepUntil(null);
      setSleepAtEnd(false);
      setSleepRemaining(null);
      return;
    }
    if (value === "episode") {
      setSleepUntil(null);
      setSleepRemaining(null);
      setSleepAtEnd(true);
      return;
    }
    setSleepAtEnd(false);
    setSleepUntil(Date.now() + value * 60_000);
    setSleepRemaining(value * 60);
  }, []);

  // Count down, fade out, pause.
  //
  // Wall-clock rather than playback time, because a sleep timer is about the
  // person falling asleep, not the audio: pausing to say something and coming
  // back should not extend the night. The last few seconds fade, since silence
  // arriving abruptly is its own kind of wake-up.
  useEffect(() => {
    if (sleepUntil == null) return;
    const FADE_SECONDS = 8;
    const tick = setInterval(() => {
      const audio = audioRef.current;
      const remaining = Math.max(0, Math.round((sleepUntil - Date.now()) / 1000));
      setSleepRemaining(remaining);
      if (!audio) return;
      if (remaining <= 0) {
        audio.pause();
        audio.volume = 1;
        setSleepUntil(null);
        setSleepRemaining(null);
        return;
      }
      audio.volume = remaining <= FADE_SECONDS ? Math.max(0.05, remaining / FADE_SECONDS) : 1;
    }, 1000);
    return () => clearInterval(tick);
  }, [sleepUntil]);

  const sleepMode: SleepMode = sleepUntil != null ? "time" : sleepAtEnd ? "episode" : "off";

  return (
    <Ctx.Provider value={{
      episode, audioRef, isPlaying, currentTime, duration, audioReady, preparing,
      loadEpisode, togglePlay, seek, skipBy, rate, setRate, settings,
      sleepMode, sleepRemaining, setSleepTimer,
      playerExpanded, setPlayerExpanded,
    }}>
      {/* Single persistent audio element — never unmounts */}
      <audio ref={audioRef} preload="auto" className="hidden" />
      {children}
    </Ctx.Provider>
  );
}

// ─── Hook ─────────────────────────────────────────────────────────────────────
export function useAudio() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useAudio must be inside <AudioProvider>");
  return ctx;
}

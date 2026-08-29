import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getTranscript, TranscriptWord } from "../api/client";

/** Words grouped into a readable line, with the index range they occupy. */
interface Line {
  start: number;
  end: number;
  first: number;   // index of the first word, into the flat word array
  words: TranscriptWord[];
}

// Roughly a phone's width of text. Lines break at sentence punctuation when one
// falls in range, so a line usually ends where the speaker paused.
const SOFT_WRAP = 46;
const HARD_WRAP = 80;
const ENDS_SENTENCE = /[.!?…:;]["')\]]?$/;

function toLines(words: TranscriptWord[]): Line[] {
  const lines: Line[] = [];
  let current: TranscriptWord[] = [];
  let first = 0;
  let chars = 0;

  words.forEach((w, i) => {
    if (current.length === 0) first = i;
    current.push(w);
    chars += w[2].length;

    const breakable = chars >= SOFT_WRAP && ENDS_SENTENCE.test(w[2].trim());
    if (breakable || chars >= HARD_WRAP) {
      lines.push({ start: current[0][0], end: w[1], first, words: current });
      current = [];
      chars = 0;
    }
  });

  if (current.length) {
    lines.push({
      start: current[0][0],
      end: current[current.length - 1][1],
      first,
      words: current,
    });
  }
  return lines;
}

/** Last word that has started by `t`. -1 before the first word. */
function wordAt(words: TranscriptWord[], t: number): number {
  let lo = 0, hi = words.length - 1, found = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (words[mid][0] <= t) { found = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  return found;
}

/**
 * The transcript, read along with the audio.
 *
 * Position comes from the audio element directly on an animation frame rather
 * than from AudioContext's `currentTime`: that state updates on `timeupdate`,
 * which fires about four times a second — visibly behind the voice — and every
 * update re-renders the whole player. Reading the element costs nothing, and
 * state is only set when the active *word index* actually changes, so React
 * runs on word boundaries instead of on every frame.
 *
 * Only the active line renders per-word spans. Rendering all ~10k words of an
 * hour-long episode as individually styled nodes would make each highlight a
 * full-document restyle; the rest of the transcript is plain text.
 */
export default function LiveTranscript({
  episodeId,
  audioRef,
  open,
  onSeek,
}: {
  episodeId: string;
  audioRef: React.RefObject<HTMLAudioElement | null>;
  open: boolean;
  onSeek: (seconds: number) => void;
}) {
  const [words, setWords] = useState<TranscriptWord[]>([]);
  const [state, setState] = useState<"idle" | "loading" | "ready" | "empty" | "error">("idle");
  const [active, setActive] = useState(-1);
  const [following, setFollowing] = useState(true);

  const scroller = useRef<HTMLDivElement | null>(null);
  const activeLineEl = useRef<HTMLButtonElement | null>(null);
  const frame = useRef<number | undefined>(undefined);
  const fetchedFor = useRef<string | null>(null);

  const lines = useMemo(() => toLines(words), [words]);

  // Which line owns the active word. Lines are contiguous, so a scan from the
  // end is enough and avoids keeping a parallel word->line map in sync.
  const activeLine = useMemo(() => {
    if (active < 0) return -1;
    for (let i = lines.length - 1; i >= 0; i--) if (lines[i].first <= active) return i;
    return -1;
  }, [lines, active]);

  useEffect(() => {
    fetchedFor.current = null;
    setWords([]); setState("idle"); setActive(-1);
  }, [episodeId]);

  // Fetched once per episode, the first time the sheet is opened.
  //
  // The guard is a ref holding the episode being fetched, not the `state` this
  // sets. An effect that both reads and writes `state` tears itself down the
  // moment it starts: setting "loading" re-runs it, the previous run's cleanup
  // fires, and the in-flight response is discarded as stale — the request
  // succeeds and the sheet says "Loading" forever. Episode identity is the
  // thing that actually decides whether a response is still wanted.
  useEffect(() => {
    if (!open || !episodeId || fetchedFor.current === episodeId) return;
    fetchedFor.current = episodeId;
    setState("loading");
    getTranscript(episodeId)
      .then(r => {
        if (fetchedFor.current !== episodeId) return;   // episode changed under us
        setWords(r.words ?? []);
        setState((r.words ?? []).length ? "ready" : "empty");
      })
      .catch(() => {
        if (fetchedFor.current !== episodeId) return;
        fetchedFor.current = null;                      // let reopening retry
        setState("error");
      });
  }, [open, episodeId]);

  // Follow playback. Runs only while the sheet is open.
  useEffect(() => {
    if (!open || !words.length) return;
    const tick = () => {
      const audio = audioRef.current;
      if (audio) {
        const i = wordAt(words, audio.currentTime);
        setActive(prev => (prev === i ? prev : i));
      }
      frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);
    return () => { if (frame.current) cancelAnimationFrame(frame.current); };
  }, [open, words, audioRef]);

  // Keep the spoken line centred, unless the reader has taken over the scroll.
  useEffect(() => {
    if (!following || activeLine < 0) return;
    activeLineEl.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [activeLine, following]);

  useEffect(() => { if (open) setFollowing(true); }, [open]);

  // Wheel and touch only fire for a real reader, so they separate a manual
  // scroll from the smooth scrolling this component does itself.
  const releaseFollow = useCallback(() => setFollowing(false), []);

  const resume = () => {
    setFollowing(true);
    activeLineEl.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  };

  if (state === "loading" || state === "idle") {
    return <p className="text-center text-white/40 text-sm py-10">Loading transcript…</p>;
  }
  if (state === "error") {
    return <p className="text-center text-white/40 text-sm py-10">Transcript not available yet.</p>;
  }
  if (state === "empty") {
    return <p className="text-center text-white/40 text-sm py-10">This transcript is empty.</p>;
  }

  return (
    <div className="relative flex-1 min-h-0">
      <div
        ref={scroller}
        onWheel={releaseFollow}
        onTouchMove={releaseFollow}
        className="h-full overflow-y-auto px-5 py-4 space-y-3"
      >
        {lines.map((line, i) => {
          const isActive = i === activeLine;
          return (
            <button
              key={i}
              ref={isActive ? activeLineEl : undefined}
              onClick={() => { onSeek(line.start); setFollowing(true); }}
              className={`block w-full text-left text-[17px] leading-relaxed transition-colors duration-200 ${
                isActive ? "text-white" : i < activeLine ? "text-white/25" : "text-white/40"
              }`}
            >
              {isActive
                ? line.words.map((w, j) => (
                    <span
                      key={j}
                      className={
                        line.first + j === active
                          ? "text-indigo-300"
                          : line.first + j < active
                            ? "text-white"
                            : "text-white/45"
                      }
                    >
                      {w[2]}
                    </span>
                  ))
                : line.words.map(w => w[2]).join("")}
            </button>
          );
        })}
        {/* Room to centre the last line rather than pinning it to the bottom. */}
        <div className="h-[40vh]" aria-hidden />
      </div>

      {!following && activeLine >= 0 && (
        <button
          onClick={resume}
          className="absolute bottom-4 left-1/2 -translate-x-1/2 bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-medium px-4 py-2 rounded-full shadow-lg"
        >
          ↓ Follow along
        </button>
      )}
    </div>
  );
}

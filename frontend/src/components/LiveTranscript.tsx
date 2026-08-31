import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { bookmarkLine, getTranscript, listBookmarks, TranscriptWord } from "../api/client";

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
  onBookmarked,
  toOriginal,
}: {
  episodeId: string;
  audioRef: React.RefObject<HTMLAudioElement | null>;
  open: boolean;
  /** Seconds in the ORIGINAL timeline; the caller converts for the file playing. */
  onSeek: (seconds: number) => void;
  /** Called after a line is kept, so the player can show a count. */
  onBookmarked?: () => void;
  /** Maps the playing file's clock onto the transcript's. Identity by default;
   *  the clean cut runs behind the original by whatever was removed. */
  toOriginal?: (seconds: number) => number;
}) {
  const [words, setWords] = useState<TranscriptWord[]>([]);
  const [state, setState] = useState<"idle" | "loading" | "ready" | "empty" | "error">("idle");
  const [active, setActive] = useState(-1);
  const [following, setFollowing] = useState(true);
  // Where this episode's bookmarks start, so a kept quote is visible in the
  // text rather than only in a list somewhere else. Held as times rather than
  // line indexes: a bookmark made from the player starts mid-line — it is a
  // sentence, not a line — so the mark has to be matched by range.
  const [markedAt, setMarkedAt] = useState<number[]>([]);
  const [justMarked, setJustMarked] = useState<number | null>(null);
  const holdTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const heldRef = useRef(false);

  const scroller = useRef<HTMLDivElement | null>(null);
  const activeLineEl = useRef<HTMLButtonElement | null>(null);
  const frame = useRef<number | undefined>(undefined);
  const fetchedFor = useRef<string | null>(null);

  const lines = useMemo(() => toLines(words), [words]);

  /** Whether a bookmark begins inside this line. */
  const isMarked = useCallback(
    (line: Line) => markedAt.some(t => t >= line.start - 0.3 && t < line.end),
    [markedAt],
  );

  // Which line owns the active word. Lines are contiguous, so a scan from the
  // end is enough and avoids keeping a parallel word->line map in sync.
  const activeLine = useMemo(() => {
    if (active < 0) return -1;
    for (let i = lines.length - 1; i >= 0; i--) if (lines[i].first <= active) return i;
    return -1;
  }, [lines, active]);

  useEffect(() => {
    fetchedFor.current = null;
    setWords([]); setState("idle"); setActive(-1); setMarkedAt([]);
  }, [episodeId]);

  // Bookmarks already kept for this episode, so reopening the transcript shows
  // them.
  useEffect(() => {
    if (!open || !episodeId) return;
    listBookmarks(episodeId)
      .then(list => setMarkedAt(list.map(b => b.start_seconds)))
      .catch(() => {});
  }, [open, episodeId]);

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
        const at = toOriginal ? toOriginal(audio.currentTime) : audio.currentTime;
        const i = wordAt(words, at);
        setActive(prev => (prev === i ? prev : i));
      }
      frame.current = requestAnimationFrame(tick);
    };
    frame.current = requestAnimationFrame(tick);
    return () => { if (frame.current) cancelAnimationFrame(frame.current); };
  }, [open, words, audioRef, toOriginal]);

  // Keep the spoken line centred, unless the reader has taken over the scroll.
  //
  // Scrolls this container explicitly rather than calling scrollIntoView on the
  // line. scrollIntoView walks up and scrolls *every* scrollable ancestor, and
  // the fullscreen player root is `overflow-hidden` — which still scrolls
  // programmatically. Centring a line therefore dragged the whole player up by
  // ~90px, opening a strip at the foot of the screen that the mini player and
  // bottom nav showed through, over the transcript.
  const centreActiveLine = useCallback((behavior: ScrollBehavior) => {
    const box = scroller.current, line = activeLineEl.current;
    if (!box || !line) return;
    box.scrollTo({
      top: Math.max(0, line.offsetTop - box.clientHeight / 2 + line.offsetHeight / 2),
      behavior,
    });
  }, []);

  useEffect(() => {
    if (!following || activeLine < 0) return;
    centreActiveLine("smooth");
  }, [activeLine, following, centreActiveLine]);

  useEffect(() => { if (open) setFollowing(true); }, [open]);

  // Wheel and touch only fire for a real reader, so they separate a manual
  // scroll from the smooth scrolling this component does itself.
  const releaseFollow = useCallback(() => setFollowing(false), []);

  const resume = () => {
    setFollowing(true);
    centreActiveLine("smooth");
  };

  /**
   * Keep a line as a bookmark.
   *
   * A press-and-hold rather than a button per line: this is a wall of text
   * being read while audio plays, and 200 small targets down the right-hand
   * side would compete with the reading. Holding is also the gesture that
   * works with a thumb on a moving train, which is the case this whole feature
   * exists for.
   */
  const keepLine = useCallback(async (line: Line) => {
    const text = line.words.map(w => w[2]).join("").trim();
    if (!text) return;
    const at = line.start;
    setMarkedAt(prev => [...prev, at]);
    setJustMarked(at);
    setTimeout(() => setJustMarked(cur => (cur === at ? null : cur)), 1200);
    try {
      await bookmarkLine(episodeId, line.start, line.end, text);
      onBookmarked?.();
    } catch {
      setMarkedAt(prev => prev.filter(t => t !== at));
    }
  }, [episodeId, onBookmarked]);

  const startHold = (line: Line) => {
    heldRef.current = false;
    clearTimeout(holdTimer.current);
    holdTimer.current = setTimeout(() => {
      heldRef.current = true;
      // Confirm the gesture landed even when the screen is not being looked at.
      if ("vibrate" in navigator) navigator.vibrate?.(15);
      keepLine(line);
    }, 450);
  };

  const endHold = () => clearTimeout(holdTimer.current);

  useEffect(() => () => clearTimeout(holdTimer.current), []);

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
        className="relative h-full overflow-y-auto px-5 py-4 space-y-3"
      >
        {lines.map((line, i) => {
          const isActive = i === activeLine;
          return (
            <button
              key={i}
              ref={isActive ? activeLineEl : undefined}
              onClick={() => {
                // A hold has already done its work; the click that follows it
                // must not also jump the audio somewhere else.
                if (heldRef.current) { heldRef.current = false; return; }
                onSeek(line.start);
                setFollowing(true);
              }}
              onPointerDown={() => startHold(line)}
              onPointerUp={endHold}
              onPointerLeave={endHold}
              onContextMenu={e => e.preventDefault()}
              className={`block w-full text-left text-[17px] leading-relaxed transition-colors duration-200 relative ${
                isMarked(line) ? "border-l-2 border-yellow-500/60 -ml-3 pl-3" : ""
              } ${
                isActive ? "text-white" : i < activeLine ? "text-white/25" : "text-white/40"
              }`}
            >
              {justMarked === line.start && (
                <span className="absolute -left-1 -top-5 text-[11px] text-yellow-300 font-medium">
                  🔖 kept
                </span>
              )}
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

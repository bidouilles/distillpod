/**
 * Translating between the original audio and the clean cut of it.
 *
 * The clean file is a concatenation of the spans worth keeping, so it runs on
 * its own clock: after the first cut it is behind the original by however much
 * was removed. Chapters, transcript timings, distills and bookmarks are all
 * recorded against the original, which exists whether or not a cut was ever
 * made — so the player converts at the edges.
 *
 *   segments = [[0, 95], [155, 1180]]     // kept spans of the ORIGINAL
 *
 *   original:  0 ────────── 95   (ad)   155 ─────────────── 1180
 *   clean:     0 ────────── 95 ──────────── 1120
 *
 * This mirrors `backend/services/timeline.py`, which is the tested
 * specification — the two must agree. The server does the conversion for
 * anything it stores; this copy is for what the player does locally: seeking,
 * and following the transcript.
 */
export type Segments = [number, number][] | number[][];

/** Where a position in the clean cut falls in the original. */
export function toOriginal(segments: Segments | null | undefined, cutSeconds: number): number {
  if (!segments || segments.length === 0) return Math.max(0, cutSeconds);
  let remaining = Math.max(0, cutSeconds);
  for (const [start, end] of segments) {
    const length = end - start;
    if (remaining <= length) return start + remaining;
    remaining -= length;
  }
  return segments[segments.length - 1][1];
}

/**
 * Where a position in the original falls in the clean cut.
 *
 * A position inside a removed span snaps forward to where the audio resumes:
 * a chapter mark landing inside a sponsor read should start the chapter rather
 * than replay the end of the previous one.
 */
export function toCut(segments: Segments | null | undefined, originalSeconds: number): number {
  if (!segments || segments.length === 0) return Math.max(0, originalSeconds);
  const target = Math.max(0, originalSeconds);
  let elapsed = 0;
  for (const [start, end] of segments) {
    if (target < start) return elapsed;
    if (target <= end) return elapsed + (target - start);
    elapsed += end - start;
  }
  return elapsed;
}

/** How long the clean cut runs. */
export function keptDuration(segments: Segments | null | undefined): number {
  if (!segments) return 0;
  return segments.reduce((total, [start, end]) => total + (end - start), 0);
}

/** Minutes and seconds, for saying how much was skipped. */
export function fmtSaved(seconds: number): string {
  const total = Math.round(seconds);
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60);
  const s = total % 60;
  return s ? `${m}m ${s}s` : `${m}m`;
}

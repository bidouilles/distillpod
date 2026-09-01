import { useState } from "react";
import { copyText } from "../lib/clipboard";

/**
 * The copy, share and download controls, in one place.
 *
 * These three actions appear on messages, distills, summaries and the export
 * row, and had drifted into four different treatments — an SVG icon in one
 * place, a 📋 emoji in another, a bare text label in a third, and an ↗ arrow in
 * a fourth. Same action, four appearances. Everything now renders from here, so
 * copy looks like copy wherever it is.
 *
 * Two shapes only: `icon` for a bare 44px target where space is tight, and
 * `pill` where there is room for a label. Both give the same tick on success.
 */

type IconProps = { className?: string };

export const CopyIcon = ({ className = "w-4 h-4" }: IconProps) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
       strokeLinecap="round" strokeLinejoin="round" className={className}>
    <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
    <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
  </svg>
);

export const ShareIcon = ({ className = "w-4 h-4" }: IconProps) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
       strokeLinecap="round" strokeLinejoin="round" className={className}>
    <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8" />
    <polyline points="16 6 12 2 8 6" />
    <line x1="12" y1="2" x2="12" y2="15" />
  </svg>
);

export const DownloadIcon = ({ className = "w-4 h-4" }: IconProps) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
       strokeLinecap="round" strokeLinejoin="round" className={className}>
    <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8" />
    <polyline points="8 12 12 16 16 12" />
    <line x1="12" y1="2" x2="12" y2="16" />
  </svg>
);

export const CheckIcon = ({ className = "w-4 h-4" }: IconProps) => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5}
       strokeLinecap="round" strokeLinejoin="round" className={className}>
    <polyline points="20 6 9 17 4 12" />
  </svg>
);

type Variant = "icon" | "pill";

const SHELL: Record<Variant, string> = {
  // 44px is the smallest comfortable thumb target.
  icon: "w-11 h-11 flex-shrink-0 flex items-center justify-center rounded-lg",
  // 44px here too, so a labelled control is no harder to hit than a bare icon.
  pill: "min-h-[44px] px-3 inline-flex items-center gap-1.5 rounded-full text-xs font-semibold",
};

/** True where the browser can actually open a share sheet. */
export const canShare = typeof navigator !== "undefined" && !!navigator.share;

/** What a copy or share button asks for when pressed.
 *
 *  Allowed to be async: some of what these buttons offer is fetched on demand
 *  rather than already on screen — a research report is rendered from stored
 *  structure, and most are never exported, so fetching it eagerly for every
 *  card would be waste. */
export type TextSource = () => string | Promise<string>;

export function CopyButton({
  getText,
  label = "Copy",
  variant = "icon",
  className = "",
  disabled = false,
}: {
  getText: TextSource;
  label?: string;
  variant?: Variant;
  className?: string;
  disabled?: boolean;
}) {
  const [state, setState] = useState<"idle" | "ok" | "fail">("idle");

  const onClick = async () => {
    let ok = false;
    try {
      ok = await copyText(await getText());
    } catch {
      ok = false;              // the text could not be fetched
    }
    setState(ok ? "ok" : "fail");
    setTimeout(() => setState("idle"), 1500);
  };

  const tone =
    state === "ok" ? "text-green-400"
      : state === "fail" ? "text-red-400"
      : "text-gray-400 hover:text-white hover:bg-gray-800";

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={`${SHELL[variant]} ${tone} transition-colors disabled:opacity-30 disabled:hover:bg-transparent ${className}`}
    >
      {state === "ok" ? <CheckIcon /> : <CopyIcon />}
      {variant === "pill" && <span>{state === "ok" ? "Copied" : label}</span>}
    </button>
  );
}

export function ShareButton({
  getText,
  getTitle,
  label = "Share",
  variant = "icon",
  className = "",
}: {
  getText: TextSource;
  getTitle?: () => string;
  label?: string;
  variant?: Variant;
  className?: string;
}) {
  const [shared, setShared] = useState(false);

  // Nothing to offer where there is no share sheet — copy covers that case.
  if (!canShare) return null;

  const onClick = async () => {
    try {
      await navigator.share({ title: getTitle?.() || "DistillPod", text: await getText() });
      setShared(true);
      setTimeout(() => setShared(false), 1500);
    } catch { /* dismissed — nothing to report */ }
  };

  return (
    <button
      onClick={onClick}
      aria-label={label}
      title={label}
      className={`${SHELL[variant]} ${shared ? "text-green-400" : "text-gray-400 hover:text-white hover:bg-gray-800"} transition-colors ${className}`}
    >
      {shared ? <CheckIcon /> : <ShareIcon />}
      {variant === "pill" && <span>{shared ? "Shared" : label}</span>}
    </button>
  );
}

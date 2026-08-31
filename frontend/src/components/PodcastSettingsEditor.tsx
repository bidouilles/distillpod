import { useState } from "react";
import { setPodcastSettings, type PodcastSettings } from "../api/client";

/**
 * Per-show playback preferences.
 *
 * Shows differ in ways a global setting cannot express: one has a two-minute
 * cold open every week, another is only ever listened to at 1.5x, a third is
 * music nobody will search. Each field can be left unset, which means "no
 * opinion" — the player keeps whatever it was last on, rather than being reset
 * by a default nobody chose.
 *
 * `auto_transcribe` is the one that is more than taste. Transcription is the
 * single stage that can cost money or pin a core for minutes, so being able to
 * say "never for this show" is a load control.
 */

const RATES = [null, 1, 1.2, 1.5, 1.8, 2] as (number | null)[];
const SKIPS = [null, 15, 30, 60, 90] as (number | null)[];

export default function PodcastSettingsEditor({
  podcastId, value, onChange,
}: {
  podcastId: string;
  value: PodcastSettings;
  onChange: (s: PodcastSettings) => void;
}) {
  const [open, setOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const save = async (patch: Partial<PodcastSettings>) => {
    const next = { ...value, ...patch };
    onChange(next);                       // optimistic: the chips answer at once
    setSaving(true);
    setError("");
    try {
      const stored = await setPodcastSettings(podcastId, next);
      onChange(stored);
    } catch {
      setError("Could not save that");
    } finally {
      setSaving(false);
    }
  };

  const active =
    value.playback_rate != null || value.skip_intro != null ||
    value.skip_outro != null || value.prefer_adfree != null ||
    value.auto_transcribe === false;

  return (
    <div className="bg-gray-900 rounded-xl overflow-hidden">
      <button
        onClick={() => setOpen(o => !o)}
        className="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-800 transition-colors"
      >
        <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider flex items-center gap-2">
          Playback for this show
          {active && <span className="w-1.5 h-1.5 rounded-full bg-indigo-400" />}
        </span>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}
          strokeLinecap="round" strokeLinejoin="round"
          className={`w-4 h-4 text-gray-500 transition-transform ${open ? "rotate-180" : ""}`}>
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      {open && (
        <div className="px-4 pb-4 space-y-3">
          <Row label="Speed">
            {RATES.map(r => (
              <Pill key={String(r)} active={value.playback_rate === r}
                    onClick={() => save({ playback_rate: r })}>
                {r == null ? "Default" : `${r}×`}
              </Pill>
            ))}
          </Row>

          <Row label="Skip intro">
            {SKIPS.map(s => (
              <Pill key={String(s)} active={value.skip_intro === s}
                    onClick={() => save({ skip_intro: s })}>
                {s == null ? "Off" : `${s}s`}
              </Pill>
            ))}
          </Row>

          <Row label="Skip outro">
            {SKIPS.map(s => (
              <Pill key={String(s)} active={value.skip_outro === s}
                    onClick={() => save({ skip_outro: s })}>
                {s == null ? "Off" : `${s}s`}
              </Pill>
            ))}
          </Row>

          <Row label="Ad-free cut">
            <Pill active={value.prefer_adfree === true} onClick={() => save({ prefer_adfree: true })}>
              Prefer it
            </Pill>
            <Pill active={value.prefer_adfree == null} onClick={() => save({ prefer_adfree: null })}>
              Ask each time
            </Pill>
          </Row>

          <Row label="Transcribe">
            <Pill active={value.auto_transcribe !== false} onClick={() => save({ auto_transcribe: null })}>
              Yes
            </Pill>
            <Pill active={value.auto_transcribe === false} onClick={() => save({ auto_transcribe: false })}>
              Never
            </Pill>
          </Row>
          <p className="text-[11px] text-gray-600 leading-relaxed">
            Turning transcription off for a show skips the one stage that can cost
            money or hold a core for minutes — useful for music or background listening.
          </p>

          {error && <p className="text-xs text-red-400">{error}</p>}
          {saving && <p className="text-[11px] text-gray-600">Saving…</p>}
        </div>
      )}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[11px] text-gray-500 uppercase tracking-wider font-semibold mb-1.5">
        {label}
      </div>
      <div className="flex gap-1.5 flex-wrap">{children}</div>
    </div>
  );
}

function Pill({ active, onClick, children }: {
  active: boolean; onClick: () => void; children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      aria-pressed={active}
      className={`min-h-[34px] px-2.5 rounded-full text-xs font-medium transition-colors ${
        active ? "bg-indigo-600 text-white" : "bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200"
      }`}
    >
      {children}
    </button>
  );
}

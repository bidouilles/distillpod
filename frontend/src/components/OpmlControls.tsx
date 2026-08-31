import { useRef, useState } from "react";
import { importOpml, opmlExportUrl } from "../api/client";
import { bustCache } from "../cache";

/**
 * Import and export the subscription list as OPML.
 *
 * The one feature here whose whole value is that other software understands it:
 * it is how a library arrives from another app, and the only thing that makes
 * this one survivable if the box has to be rebuilt.
 *
 * The file is read in the browser and posted as text rather than uploaded as
 * multipart — one less moving part, and an OPML file is a few kilobytes.
 */
export default function OpmlControls({ onImported }: { onImported: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  const handleFile = async (file: File) => {
    setBusy(true);
    setStatus("");
    try {
      const result = await importOpml(await file.text());
      if (result.found === 0) {
        setStatus("No feeds found in that file");
      } else if (result.added === 0) {
        setStatus(`Already subscribed to all ${result.found}`);
      } else {
        setStatus(
          `Added ${result.added} show${result.added === 1 ? "" : "s"}` +
          (result.skipped ? `, skipped ${result.skipped} already here` : "") +
          " — tap Refresh on Home to fetch episodes",
        );
        bustCache("home:feed");
        onImported();
      }
    } catch {
      setStatus("That file could not be read as OPML");
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <button
          onClick={() => input.current?.click()}
          disabled={busy}
          className="flex-1 min-h-[40px] rounded-xl bg-gray-800 hover:bg-gray-700 disabled:opacity-60 text-xs text-gray-300 font-medium transition-colors"
        >
          {busy ? "Importing…" : "↓ Import OPML"}
        </button>
        <a
          href={opmlExportUrl()}
          className="flex-1 min-h-[40px] rounded-xl bg-gray-800 hover:bg-gray-700 text-xs text-gray-300 font-medium transition-colors flex items-center justify-center"
        >
          ↑ Export OPML
        </a>
      </div>
      <input
        ref={input}
        type="file"
        accept=".opml,.xml,text/xml,application/xml"
        className="hidden"
        onChange={e => { const f = e.target.files?.[0]; if (f) handleFile(f); }}
      />
      {status && <p className="text-[11px] text-gray-500 leading-relaxed">{status}</p>}
    </div>
  );
}

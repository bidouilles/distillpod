import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  deleteReport, getResearchMarkdown, listReports, researchPdfUrl,
  type ReportSummary,
} from "../api/client";
import { CopyButton, ShareButton, DownloadIcon } from "./ActionButtons";
import { downloadText, slugify } from "../lib/clipboard";

/**
 * Every research report, in one place.
 *
 * They used to be reachable only through the distillation that produced them —
 * three screens deep, and not at all if you had forgotten which moment it came
 * from. They are also the only thing the app makes that had no way to be
 * deleted: the row and its files simply accumulated.
 */

const VERDICTS: Record<string, { label: string; className: string }> = {
  supported:   { label: "Supported",   className: "bg-green-900/40 text-green-300" },
  mixed:       { label: "Mixed",       className: "bg-yellow-900/40 text-yellow-300" },
  contested:   { label: "Contested",   className: "bg-orange-900/40 text-orange-300" },
  unsupported: { label: "Not supported", className: "bg-red-900/40 text-red-300" },
  no_evidence: { label: "No evidence", className: "bg-gray-800 text-gray-400" },
};

function fmtDate(iso?: string | null) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export default function ReportsPanel() {
  const nav = useNavigate();
  const [reports, setReports] = useState<ReportSummary[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = () => { listReports().then(setReports).catch(() => setReports([])); };
  useEffect(load, []);

  const remove = async (report: ReportSummary) => {
    if (!confirm("Delete this report? The distillation it came from is kept.")) return;
    setBusy(report.id);
    setReports(prev => (prev ?? []).filter(r => r.id !== report.id));
    try { await deleteReport(report.gist_id); }
    catch { load(); }
    finally { setBusy(null); }
  };

  if (reports === null) {
    return <div className="bg-gray-900 rounded-2xl h-24 animate-pulse" />;
  }

  if (reports.length === 0) {
    return (
      <div className="text-center py-10 text-gray-500">
        <div className="text-4xl mb-3">🔬</div>
        <p className="text-gray-300 font-medium">No research yet</p>
        <p className="text-sm mt-1 max-w-xs mx-auto">
          Open a distillation in Saved and tap Research to check what was said
          against sources outside the podcast.
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {reports.map(report => {
        const verdict = VERDICTS[report.verdict];
        const failed = report.status === "error";
        const running = report.status === "pending" || report.status === "running";
        const markdown = () => getResearchMarkdown(report.gist_id).then(r => r.markdown);

        return (
          <div key={report.id} className="bg-gray-900 rounded-2xl px-4 py-3 space-y-2">
            <div className="flex items-start gap-2">
              <div className="min-w-0 flex-1">
                <div className="text-[11px] text-gray-500 truncate">
                  {report.podcast_title}
                  {report.episode_title ? ` · ${report.episode_title}` : ""}
                </div>
                <div className="text-sm text-gray-100 leading-snug line-clamp-2 mt-0.5">
                  {report.claim || (failed
                    ? "Research failed"
                    : running ? "Researching…" : "Report")}
                </div>
              </div>
              <button
                onClick={() => remove(report)}
                disabled={busy === report.id}
                aria-label="Delete report"
                className="w-8 h-8 flex-shrink-0 flex items-center justify-center text-gray-600 hover:text-red-400 rounded-full hover:bg-gray-800 transition-colors"
              >
                ✕
              </button>
            </div>

            <div className="flex items-center gap-2 flex-wrap text-[11px] text-gray-500">
              {verdict && (
                <span className={`font-semibold rounded-full px-2 py-0.5 ${verdict.className}`}>
                  {verdict.label}
                </span>
              )}
              {report.sources > 0 && <span>{report.sources} sources</span>}
              <span>{fmtDate(report.finished_at || report.created_at)}</span>
              {failed && report.error && (
                <span className="text-red-400/80 line-clamp-1">{report.error}</span>
              )}
            </div>

            {report.status === "done" && (
              <div className="flex items-center gap-1 flex-wrap pt-0.5">
                {report.public_url && (
                  <button
                    onClick={() => window.open(report.public_url!, "_blank")}
                    className="text-xs font-semibold px-2.5 py-1 rounded-lg"
                    style={{ background: "#FFD700", color: "#1A1A1A" }}
                  >
                    Open
                  </button>
                )}
                {report.pdf && (
                  <a
                    href={researchPdfUrl(report.gist_id)}
                    className="text-xs font-medium px-2.5 py-1 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors"
                    title="Typeset as a PDF briefing note"
                  >
                    PDF
                  </a>
                )}
                {report.markdown && (
                  <>
                    <CopyButton getText={markdown} label="Copy as markdown" />
                    <ShareButton
                      getText={markdown}
                      getTitle={() => report.claim || "Research report"}
                      label="Share report"
                    />
                    <button
                      onClick={async () => {
                        const text = await markdown().catch(() => null);
                        if (text) downloadText(`${slugify(report.claim || "research")}.md`, text);
                      }}
                      aria-label="Download as markdown"
                      title="Download as .md"
                      className="w-8 h-8 flex items-center justify-center text-gray-400 hover:text-white rounded-full hover:bg-gray-800 transition-colors"
                    >
                      <DownloadIcon />
                    </button>
                  </>
                )}
                <span className="flex-1" />
                <button
                  onClick={() => nav(`/player/${report.episode_id}`)}
                  className="text-[11px] text-gray-500 hover:text-gray-300 px-1"
                >
                  the episode ›
                </button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

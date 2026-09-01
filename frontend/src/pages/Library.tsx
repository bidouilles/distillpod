import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import SegmentedTabs from "../components/SegmentedTabs";
import ShowsPanel from "./Subscriptions";
import PlaylistsPanel from "../components/PlaylistsPanel";
import StoragePanel from "../components/StoragePanel";
import ReportsPanel from "../components/ReportsPanel";
import { listPlaylists, listReports, getSubscriptions } from "../api/client";

type Section = "shows" | "playlists" | "reports" | "storage";

/**
 * The library as a place rather than a list.
 *
 * Subscriptions, playlists and what the audio is costing are all answers to
 * "what do I have", and on a phone they cannot each have a bottom tab. The
 * section lives in the URL so a back gesture returns to the one you were on
 * and a link can point at it.
 */
export default function Library() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const section: Section =
    raw === "playlists" || raw === "storage" || raw === "reports" ? raw : "shows";

  const [counts, setCounts] = useState<{ shows: number; playlists: number; reports: number }>({
    shows: 0, playlists: 0, reports: 0,
  });

  // Counts on the tabs, so the sections say how much is behind them before
  // they are opened. Cheap: two small queries, once per visit.
  useEffect(() => {
    getSubscriptions().then(s => setCounts(c => ({ ...c, shows: s.length }))).catch(() => {});
    listPlaylists().then(p => setCounts(c => ({ ...c, playlists: p.length }))).catch(() => {});
    listReports().then(r => setCounts(c => ({ ...c, reports: r.length }))).catch(() => {});
  }, [section]);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">Library</h1>

      <SegmentedTabs
        value={section}
        onChange={(key) => setParams(key === "shows" ? {} : { tab: key }, { replace: true })}
        segments={[
          { key: "shows",     label: "Shows",     badge: counts.shows },
          { key: "playlists", label: "Playlists", badge: counts.playlists },
          { key: "reports",   label: "Reports",   badge: counts.reports },
          { key: "storage",   label: "Storage" },
        ]}
      />

      {section === "shows"     && <ShowsPanel embedded />}
      {section === "playlists" && <PlaylistsPanel />}
      {section === "reports"   && <ReportsPanel />}
      {section === "storage"   && <StoragePanel />}
    </div>
  );
}

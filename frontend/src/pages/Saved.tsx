import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import SegmentedTabs from "../components/SegmentedTabs";
import Gists from "./Gists";
import BookmarkList from "../components/BookmarkList";
import { listBookmarks, listGists } from "../api/client";

type Section = "distills" | "bookmarks";

/**
 * Everything kept from an episode, in one place.
 *
 * Two kinds, side by side rather than merged: a distillation is the model's
 * reading of a moment and costs a CLI round trip; a bookmark is the listener's
 * own and costs nothing. Keeping them apart is what makes the cheap one
 * worth having.
 */
export default function Saved() {
  const [params, setParams] = useSearchParams();
  const section: Section = params.get("tab") === "bookmarks" ? "bookmarks" : "distills";
  const [counts, setCounts] = useState({ distills: 0, bookmarks: 0 });

  useEffect(() => {
    listGists().then(g => setCounts(c => ({ ...c, distills: g.length }))).catch(() => {});
    listBookmarks().then(b => setCounts(c => ({ ...c, bookmarks: b.length }))).catch(() => {});
  }, [section]);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">Saved</h1>

      <SegmentedTabs
        value={section}
        onChange={key => setParams(key === "distills" ? {} : { tab: key }, { replace: true })}
        segments={[
          { key: "distills",  label: "⚗️ Distills",  badge: counts.distills },
          { key: "bookmarks", label: "🔖 Bookmarks", badge: counts.bookmarks },
        ]}
      />

      {section === "distills" ? <Gists embedded /> : <BookmarkList />}
    </div>
  );
}

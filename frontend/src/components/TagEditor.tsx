import { useEffect, useState } from "react";
import { Tag, getTags, createTag, setPodcastTags } from "../api/client";

/** Assign tags to one podcast. Type a new name to create and attach it in one go. */
export default function TagEditor({
  podcastId, value, onChange,
}: {
  podcastId: string;
  value: Tag[];
  onChange: (tags: Tag[]) => void;
}) {
  const [all, setAll] = useState<Tag[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => { getTags().then(setAll).catch(() => {}); }, []);

  const selected = new Set(value.map(t => t.id));

  const commit = async (ids: string[]) => {
    setBusy(true);
    setError("");
    try {
      onChange(await setPodcastTags(podcastId, ids));
      setAll(await getTags());          // refresh podcast_count on the chips
    } catch {
      setError("Could not save tags");
    } finally {
      setBusy(false);
    }
  };

  const toggle = (tag: Tag) => {
    const ids = selected.has(tag.id)
      ? [...selected].filter(id => id !== tag.id)
      : [...selected, tag.id];
    commit(ids);
  };

  const addNew = async () => {
    const name = draft.trim();
    if (!name || busy) return;
    setBusy(true);
    setError("");
    try {
      // create is idempotent on name, so typing an existing tag just attaches it
      const tag = await createTag(name);
      setDraft("");
      await commit([...selected, tag.id]);
    } catch {
      setError("Could not create that tag");
      setBusy(false);
    }
  };

  const unselected = all.filter(t => !selected.has(t.id));

  return (
    <div className="bg-gray-900 rounded-xl p-3 space-y-2">
      <p className="text-[11px] font-semibold text-gray-500 uppercase tracking-widest">Tags</p>

      <div className="flex flex-wrap gap-1.5">
        {value.map(t => (
          <button
            key={t.id}
            onClick={() => toggle(t)}
            disabled={busy}
            title="Remove this tag"
            className="min-h-[32px] px-2.5 rounded-full bg-indigo-600 text-white text-xs font-medium disabled:opacity-50 inline-flex items-center gap-1"
          >
            #{t.name}<span className="text-indigo-200">✕</span>
          </button>
        ))}
        {value.length === 0 && (
          <span className="text-xs text-gray-600 py-1.5">None yet — add one below.</span>
        )}
      </div>

      {unselected.length > 0 && (
        <div className="flex flex-wrap gap-1.5 pt-1 border-t border-gray-800">
          {unselected.map(t => (
            <button
              key={t.id}
              onClick={() => toggle(t)}
              disabled={busy}
              className="min-h-[32px] px-2.5 rounded-full bg-gray-800 text-gray-400 hover:bg-gray-700 hover:text-gray-200 text-xs disabled:opacity-50"
            >
              + {t.name}
            </button>
          ))}
        </div>
      )}

      <div className="flex gap-1.5">
        <input
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); addNew(); } }}
          placeholder="New tag…"
          maxLength={32}
          className="flex-1 bg-gray-800 text-white placeholder-gray-600 rounded-lg px-3 min-h-[40px] text-xs focus:outline-none focus:ring-1 focus:ring-indigo-500"
        />
        <button
          onClick={addNew}
          disabled={!draft.trim() || busy}
          className="min-h-[40px] px-3 rounded-lg bg-indigo-600 text-white text-xs font-medium disabled:opacity-30"
        >
          Add
        </button>
      </div>

      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  );
}

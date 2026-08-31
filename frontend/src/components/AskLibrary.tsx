import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  askLibrary, clearAskHistory, getAskHistory,
  type AskMessage, type Citation,
} from "../api/client";
import SemanticIndexCard from "./SemanticIndexCard";

/**
 * Ask the whole library a question.
 *
 * The one thing a self-hosted archive can do that a podcast app cannot: the
 * answer is drawn from everything you have listened to, and every claim carries
 * the episode and the second it came from, so it can be checked by listening
 * rather than taken on trust. Tapping a citation opens the player there.
 *
 * Answers take tens of seconds — two model calls through the agent CLI, one to
 * work out what to search for and one to answer from what came back — so the
 * wait says which of the two is happening.
 */

const EXAMPLES = [
  "What have I heard about evaluating models?",
  "Where did someone talk about burning out?",
  "What books have been recommended?",
];

function fmtTime(secs: number) {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = Math.floor(secs % 60);
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    : `${m}:${String(s).padStart(2, "0")}`;
}

/** Renders [1] [2] in an answer as taps that open the source. */
function Answer({ text, citations, onCite }: {
  text: string; citations: Citation[]; onCite: (c: Citation) => void;
}) {
  const byIndex = new Map(citations.map(c => [c.index, c]));
  const parts = text.split(/(\[\d+\])/g);
  return (
    <p className="text-sm text-gray-100 leading-relaxed whitespace-pre-line selectable">
      {parts.map((part, i) => {
        const m = /^\[(\d+)\]$/.exec(part);
        if (!m) return <span key={i}>{part}</span>;
        const citation = byIndex.get(Number(m[1]));
        if (!citation) return <span key={i} className="text-gray-500">{part}</span>;
        return (
          <button
            key={i}
            onClick={() => onCite(citation)}
            title={`${citation.episode_title} · ${fmtTime(citation.start)}`}
            className="align-baseline text-[11px] font-semibold text-indigo-300 bg-indigo-600/20 hover:bg-indigo-600/40 rounded px-1 mx-0.5 transition-colors"
          >
            {m[1]}
          </button>
        );
      })}
    </p>
  );
}

function Sources({ citations, onCite }: {
  citations: Citation[]; onCite: (c: Citation) => void;
}) {
  const [open, setOpen] = useState(false);
  if (citations.length === 0) return null;
  return (
    <div className="mt-3 space-y-2">
      <button
        onClick={() => setOpen(o => !o)}
        className="text-[11px] font-semibold text-gray-500 hover:text-gray-300 uppercase tracking-wider"
      >
        {open ? "Hide" : "Show"} {citations.length} source{citations.length === 1 ? "" : "s"}
      </button>
      {open && citations.map(c => (
        <button
          key={`${c.episode_id}-${c.start}`}
          onClick={() => onCite(c)}
          className="w-full text-left bg-gray-800/60 hover:bg-gray-800 rounded-xl p-3 transition-colors"
        >
          <div className="flex items-center gap-2 mb-1">
            <span className="text-[10px] font-bold text-indigo-300 bg-indigo-600/20 rounded px-1.5 flex-shrink-0">
              {c.index}
            </span>
            <span className="text-xs text-gray-400 truncate">{c.podcast_title}</span>
            <span className="text-xs text-gray-600 flex-shrink-0 font-mono">
              {fmtTime(c.start)}
            </span>
          </div>
          <div className="text-xs text-gray-300 truncate">{c.episode_title}</div>
          <div className="text-[11px] text-gray-500 mt-1 line-clamp-3 leading-relaxed">
            {c.text}
          </div>
        </button>
      ))}
    </div>
  );
}

export default function AskLibrary() {
  const nav = useNavigate();
  const [history, setHistory] = useState<AskMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [stage, setStage] = useState<"idle" | "searching" | "reading">("idle");
  const [error, setError] = useState("");
  const bottom = useRef<HTMLDivElement | null>(null);

  useEffect(() => { getAskHistory().then(setHistory).catch(() => {}); }, []);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [history.length, stage]);

  const openCitation = (c: Citation) => {
    nav(`/player/${c.episode_id}`, {
      state: { seekTo: c.start, podcast_title: c.podcast_title, podcast_image: c.podcast_image },
    });
  };

  const submit = async (question: string) => {
    const text = question.trim();
    if (!text || stage !== "idle") return;
    setDraft("");
    setError("");
    // Shown at once, so the question is on screen while the answer is worked out.
    setHistory(prev => [...prev, {
      id: `pending-${Date.now()}`, role: "user", content: text,
      citations: [], created_at: new Date().toISOString(),
    }]);
    setStage("searching");
    // The stages are honest about what is slow: working out what to search for,
    // then reading what came back.
    const toReading = setTimeout(() => setStage("reading"), 8000);
    try {
      const answer = await askLibrary(text);
      setHistory(prev => [...prev, answer]);
    } catch {
      setError("That question could not be answered — the agent CLI may be busy or unavailable.");
    } finally {
      clearTimeout(toReading);
      setStage("idle");
    }
  };

  const reset = async () => {
    await clearAskHistory().catch(() => {});
    setHistory([]);
    setError("");
  };

  return (
    <div className="space-y-3">
      {/* What retrieval can currently see. Disappears once the index is complete. */}
      <SemanticIndexCard />

      {history.length === 0 && stage === "idle" && (
        <div className="text-center py-6 space-y-3">
          <div className="text-4xl">🔎</div>
          <p className="text-gray-300 font-medium">Ask your episodes</p>
          <p className="text-sm text-gray-500 max-w-sm mx-auto leading-relaxed">
            Answered from the transcripts you already have, with the episode and
            timestamp behind every claim. Searches by keyword and — once the
            index is built — by meaning.
          </p>
          <div className="flex flex-col gap-2 pt-1">
            {EXAMPLES.map(e => (
              <button
                key={e}
                onClick={() => submit(e)}
                className="text-sm text-indigo-300 bg-gray-900 hover:bg-gray-800 rounded-xl min-h-[44px] px-4 transition-colors"
              >
                {e}
              </button>
            ))}
          </div>
        </div>
      )}

      {history.map(m => (
        m.role === "user" ? (
          <div key={m.id} className="flex justify-end">
            <div className="bg-indigo-600 text-white rounded-2xl rounded-br-sm px-4 py-2.5 max-w-[85%] text-sm">
              {m.content}
            </div>
          </div>
        ) : (
          <div key={m.id} className="bg-gray-900 rounded-2xl rounded-bl-sm px-4 py-3">
            <Answer text={m.content} citations={m.citations} onCite={openCitation} />
            <Sources citations={m.citations} onCite={openCitation} />
          </div>
        )
      ))}

      {stage !== "idle" && (
        <div className="bg-gray-900 rounded-2xl px-4 py-3 flex items-center gap-2.5 text-sm text-gray-400">
          <span className="w-3.5 h-3.5 border-2 border-gray-600 border-t-indigo-400 rounded-full animate-spin inline-block" />
          {stage === "searching" ? "Working out what to search for…" : "Reading the passages it found…"}
        </div>
      )}

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 text-red-300 rounded-xl px-4 py-2.5 text-xs">
          {error}
        </div>
      )}

      <div ref={bottom} />

      {/* Composer */}
      <div className="flex gap-2 sticky bottom-0 bg-gray-950 pt-2">
        <input
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") submit(draft); }}
          placeholder="Ask across your episodes…"
          disabled={stage !== "idle"}
          className="flex-1 bg-gray-900 text-white placeholder-gray-500 rounded-xl px-4 min-h-[48px] text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:opacity-60"
        />
        <button
          onClick={() => submit(draft)}
          disabled={stage !== "idle" || !draft.trim()}
          className="min-h-[48px] px-4 rounded-xl bg-indigo-600 hover:bg-indigo-500 disabled:opacity-40 text-white font-semibold text-sm"
        >
          Ask
        </button>
      </div>

      {history.length > 0 && (
        <div className="text-center">
          <button onClick={reset} className="text-xs text-gray-600 hover:text-gray-400 min-h-[36px] px-3">
            Clear conversation
          </button>
        </div>
      )}
    </div>
  );
}

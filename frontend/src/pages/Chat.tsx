import React, { useEffect, useRef, useState } from "react";
import { useParams, useLocation, useNavigate } from "react-router-dom";
import { getChat, initChat, sendChatMessage, getEpisode, ChatMessage } from "../api/client";
import { useAudio } from "../context/AudioContext";
import { copyText, downloadText, slugify } from "../lib/clipboard";
import ReactMarkdown from "react-markdown";

const markdownComponents = {
  li: ({ children, ...props }: any) => {
    // Unwrap <p> tags that react-markdown adds inside list items
    // when source markdown has blank lines between items.
    // This prevents the list marker from being orphaned on its own line.
    const unwrapped = React.Children.map(children, (child: React.ReactNode) => {
      if (React.isValidElement<{ children?: React.ReactNode }>(child) && child.type === "p") {
        return <>{child.props.children}</>;
      }
      return child;
    });
    return <li {...props}>{unwrapped}</li>;
  },
};

/** Copy one message's raw Markdown.
 *
 *  Sits beside the bubble rather than below it: bubbles are max-w-[85%], so the
 *  leftover gutter fits a 44px touch target without adding vertical space to
 *  every turn. Always visible — hover affordances do not exist on a phone.
 */
function MessageCopyButton({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "ok" | "fail">("idle");

  const onClick = async () => {
    const ok = await copyText(text);
    setState(ok ? "ok" : "fail");
    setTimeout(() => setState("idle"), 1500);
  };

  return (
    <button
      onClick={onClick}
      aria-label="Copy this message"
      title="Copy this message"
      className={`flex-shrink-0 w-11 h-11 flex items-center justify-center rounded-lg transition-colors ${
        state === "ok" ? "text-green-400" : state === "fail" ? "text-red-400" : "text-gray-600 hover:text-gray-300 hover:bg-gray-800"
      }`}
    >
      {state === "ok" ? (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      ) : (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className="w-4 h-4">
          <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
          <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
        </svg>
      )}
    </button>
  );
}


/** Render the conversation as Markdown. Assistant replies are already Markdown,
 *  so they drop in verbatim; user turns are plain text. */
function buildMarkdown(episodeTitle: string, messages: ChatMessage[]): string {
  const out = [
    `# ${episodeTitle}`,
    "",
    `_Chat exported from DistillPod — ${new Date().toLocaleString()}_`,
    "",
    "---",
    "",
  ];
  for (const m of messages) {
    out.push(m.role === "user" ? "### You" : "### DistillPod", "", m.content.trim(), "");
  }
  return out.join("\n");
}


export default function Chat() {
  const { episodeId } = useParams<{ episodeId: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const passedTitle = (location.state as { episodeTitle?: string } | null)?.episodeTitle;
  // location.state is empty on a direct load or refresh, so fall back to the API
  // rather than showing (and exporting) a generic "Episode".
  const [fetchedTitle, setFetchedTitle] = useState<string | null>(null);
  const episodeTitle = passedTitle ?? fetchedTitle ?? "Episode";
  const { episode: audioEpisode } = useAudio();
  const bottomOffset = audioEpisode ? "calc(56px + 56px + env(safe-area-inset-bottom))" : "calc(56px + env(safe-area-inset-bottom))";

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [initializing, setInitializing] = useState(true);
  const [exported, setExported] = useState<"copied" | "failed" | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages, loading]);

  useEffect(() => {
    if (!episodeId || passedTitle) return;
    let cancelled = false;
    getEpisode(episodeId)
      .then(ep => { if (!cancelled) setFetchedTitle(ep.title); })
      .catch(() => { /* header falls back to "Episode" */ });
    return () => { cancelled = true; };
  }, [episodeId, passedTitle]);

  useEffect(() => {
    if (!episodeId) return;
    let cancelled = false;
    (async () => {
      setInitializing(true);
      try {
        const existing = await getChat(episodeId);
        if (cancelled) return;
        if (existing.length > 0) {
          setMessages(existing);
        } else {
          setLoading(true);
          const first = await initChat(episodeId);
          if (cancelled) return;
          setMessages([first]);
          setLoading(false);
        }
      } catch {
        // ignore
      } finally {
        if (!cancelled) setInitializing(false);
      }
    })();
    return () => { cancelled = true; };
  }, [episodeId]);

  const handleSend = async () => {
    if (!input.trim() || !episodeId || loading) return;
    const text = input.trim();
    setInput("");
    const userMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: "user",
      content: text,
      created_at: new Date().toISOString(),
    };
    setMessages(prev => [...prev, userMsg]);
    setLoading(true);
    try {
      const reply = await sendChatMessage(episodeId, text);
      setMessages(prev => [...prev, reply]);
    } catch {
      setMessages(prev => [
        ...prev,
        { id: crypto.randomUUID(), role: "assistant", content: "Sorry, something went wrong. Please try again.", created_at: new Date().toISOString() },
      ]);
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = async () => {
    const ok = await copyText(buildMarkdown(episodeTitle, messages));
    setExported(ok ? "copied" : "failed");
    setTimeout(() => setExported(null), 2000);
  };

  const handleDownload = () => {
    downloadText(`${slugify(episodeTitle)}-chat.md`, buildMarkdown(episodeTitle, messages));
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    // z-[45] sits above the app banner and MiniPlayer (both z-40) so this
    // full-screen view's own header — back arrow, title, export actions — is not
    // painted underneath them, but stays below BottomNav/QueueSheet (z-50) and
    // FullscreenPlayer (z-[60]).
    <div className="fixed top-0 left-0 right-0 flex flex-col z-[45]" style={{ background: "#1A1A1A", bottom: bottomOffset }}>
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-800 bg-gray-900" style={{ paddingTop: "max(0.75rem, env(safe-area-inset-top))" }}>
        <button
          onClick={() => navigate(-1)}
          aria-label="Back"
          className="flex-shrink-0 -ml-2 w-11 h-11 flex items-center justify-center text-gray-300 hover:text-white transition-colors"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" className="w-6 h-6">
            <polyline points="15 18 9 12 15 6" />
          </svg>
        </button>
        <div className="min-w-0 flex-1">
          <h1 className="text-sm font-bold truncate" style={{ color: "#FFD700" }}>Chat</h1>
          <p className="text-xs text-gray-400 truncate">{episodeTitle}</p>
        </div>

        <div className="flex flex-shrink-0 gap-1">
          <button
            onClick={handleCopy}
            disabled={messages.length === 0}
            title="Copy conversation as Markdown"
            className="text-xs text-gray-400 hover:text-white px-2 min-h-[44px] inline-flex items-center rounded hover:bg-gray-700 transition-colors disabled:opacity-30 disabled:hover:bg-transparent"
          >
            {exported === "copied" ? "✓ Copied" : exported === "failed" ? "✕ Failed" : "📋 Copy"}
          </button>
          <button
            onClick={handleDownload}
            disabled={messages.length === 0}
            title="Download conversation as a .md file"
            className="text-xs text-gray-400 hover:text-white px-2 min-h-[44px] inline-flex items-center rounded hover:bg-gray-700 transition-colors disabled:opacity-30 disabled:hover:bg-transparent"
          >
            ⬇ .md
          </button>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
        {initializing && messages.length === 0 && (
          <div className="flex items-center justify-center h-full">
            <div className="text-gray-500 text-sm flex items-center gap-2">
              <span className="w-4 h-4 border-2 border-gray-500 border-t-yellow-400 rounded-full animate-spin inline-block" />
              Loading conversation...
            </div>
          </div>
        )}

        {messages.map((msg) => (
          <div key={msg.id} className={`flex items-end gap-0.5 ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
            {msg.role === "user" && <MessageCopyButton text={msg.content} />}
            <div
              className={`selectable max-w-[85%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed ${
                msg.role === "assistant"
                  ? "bg-gray-800 text-gray-100"
                  : "bg-gray-800"
              }`}
              style={msg.role === "user" ? { color: "#FFD700", whiteSpace: "pre-wrap" } : undefined}
            >
              {msg.role === "assistant"
                ? <div className="markdown-chat"><ReactMarkdown components={markdownComponents}>{msg.content}</ReactMarkdown></div>
                : msg.content}
            </div>
            {msg.role === "assistant" && <MessageCopyButton text={msg.content} />}
          </div>
        ))}

        {loading && (
          <div className="flex justify-start">
            <div className="bg-gray-800 rounded-2xl px-4 py-2.5 text-sm text-gray-400">
              <span className="inline-flex gap-1">
                <span className="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                <span className="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                <span className="w-2 h-2 bg-gray-500 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
              </span>
            </div>
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      {/* Input */}
      <div className="border-t border-gray-800 bg-gray-900 px-4 py-3" style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}>
        <div className="flex gap-2 items-end">
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about this episode..."
            rows={1}
            className="flex-1 bg-gray-800 text-white placeholder-gray-500 rounded-xl px-4 py-2.5 text-sm resize-none focus:outline-none focus:ring-1"
            style={{ focusRingColor: "#FFD700" } as React.CSSProperties}
            disabled={loading}
          />
          <button
            onClick={handleSend}
            disabled={!input.trim() || loading}
            className="flex-shrink-0 w-11 h-11 rounded-xl flex items-center justify-center transition-colors disabled:opacity-40"
            style={{ background: "#FFD700" }}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="#1A1A1A" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round" className="w-5 h-5">
              <line x1="22" y1="2" x2="11" y2="13" />
              <polygon points="22 2 15 22 11 13 2 9 22 2" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}

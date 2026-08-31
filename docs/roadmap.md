# DistillPod — Feature backlog

_Compiled 2026-08-31 from a survey of [Metacast](https://metacast.app/features),
[Podwise](https://podwise.ai/#features) and the
[Pocket Casts feature blog](https://blog.pocketcasts.com/category/feature/),
checked line by line against what this fork already ships._

> [!NOTE]
> **Tier 1, Tier 2, 3.1, 3.2 and parts of Tier 4/6 shipped on 2026-08-31** — entries below
> are marked **✅ Shipped**. What went in: the server-side queue, bookmarks,
> sleep timer, per-podcast playback settings, OPML, media retention, playlists
> (manual and smart), inbox semantics, duration filters and sorting, and a
> Library restructured into sections. See the README's Features list for what
> each one does. Everything unmarked is still open.

Each entry says what it is, where the idea comes from, why it fits a self-hosted
app whose model calls are free but slow, and which files it lands in.
Effort is rough: **S** ≈ an evening, **M** ≈ a weekend, **L** ≈ multi-session.

---

## Where DistillPod already stands

Worth stating, because most of the "AI podcast app" feature lists are already covered:

| Competitor headline | DistillPod today |
|---|---|
| Transcript for every episode (Metacast) | ✅ word-level, Voxtral/whisper/mlx |
| Read-along highlighted transcript (Pocket Casts, Jun 2026) | ✅ `LiveTranscript.tsx` |
| AI chapters (Pocket Casts, Jul 2026) | ✅ `chapterizer.py` + imported YouTube chapters |
| AI summary / insights (Podwise) | ✅ brief + distills |
| Ask anything (Podwise) | ✅ per-episode chat |
| Transcript search | ✅ accent-insensitive, seeks to the word |
| Export to Obsidian / Markdown (Podwise) | ✅ `note_builder.py`, `GET /player/export/{id}` |
| Mind map (Podwise) | ◐ built for the Obsidian note, never shown in-app |
| YouTube as a source (Podwise multi-source) | ✅ videos and channels |
| Ad-free audio | ✅ — nobody else on this list does it |
| Deep research from a quote | ✅ — nobody else on this list does it |
| Bookmark a quote (Metacast's core gesture) | ✅ *(shipped 2026-08-31)* |
| Playlists, manual + smart (Pocket Casts 8.0) | ✅ *(shipped 2026-08-31)* |
| Podcast inbox (Metacast) | ✅ *(shipped 2026-08-31)* |
| OPML import/export (both) | ✅ *(shipped 2026-08-31)* |
| Sleep timer (every podcast app) | ✅ *(shipped 2026-08-31)* |
| Private / paid RSS feeds (both) | ✗ — see 4.1 |
| Speaker names (Metacast) | ✗ — see 6.1 |

So the gaps are **not** in AI features. They are in **player craft**, **library
organization**, **capture that costs nothing**, and **cross-episode** reach.

---

## Tier 1 — small, high-value, no model calls

### 1.1 Server-side queue · S — ✅ Shipped
`stores/queueStore.ts` persists Up Next to `localStorage` only, while playback
position and opened-episode state already live server-side. The queue is the one
thing that silently breaks the cross-device promise the README makes.
→ new `queue` table, `player.py` endpoints, keep the store as the offline cache
(same stale-while-revalidate shape as everything else).

### 1.2 Bookmarks — free capture · S/M — ✅ Shipped
_Metacast's core gesture._ Swipe a transcript line to save a verbatim quote with
its timestamp. No model call, no 30s wait: a distill costs a CLI round-trip, a
bookmark costs an INSERT. That difference matters when you are listening in the
car and want six of them.
→ `bookmarks` table, tap/swipe target in `LiveTranscript.tsx`, fold into the
Obsidian note and the export alongside distills.

### 1.3 Sleep timer · S — ✅ Shipped
Absent from `FullscreenPlayer.tsx` (only speed and skip exist). Table stakes for
bedtime listening; end-of-episode + 15/30/45 min, fade out.

### 1.4 Silence trimming and volume normalization · S — ✅ Shipped
_Shipped 2026-08-31, per-podcast. Doing it properly first required fixing a
pre-existing bug it would have multiplied: the ad-free cut was never mapped back
to the original timeline, so every timestamp feature was silently wrong on it.
The cut now stores the spans it keeps, and positions are translated at the
edges._

ffmpeg is already a hard dependency for ad-free audio, and `ad_detector.py`
already rewrites the file. Add `silenceremove` and `loudnorm` as a second pass,
toggled per podcast. Pocket Casts' most-loved audio features, and here they are
essentially free — the transcript's word timings even let the trimmed timeline be
remapped exactly rather than estimated.

### 1.5 Per-podcast playback settings · S — ✅ Shipped
Speed, skip-intro/skip-outro seconds, ad-free default, auto-transcribe on/off.
→ columns on `subscriptions`, honored by `AudioContext.tsx`.
The auto-transcribe toggle is also a load control: some shows you always want
transcribed, others never.

### 1.6 OPML import/export · S — ✅ Shipped
Zero occurrences of `opml` in the codebase. It is the migration path both
Metacast and Pocket Casts advertise, and the only way this app survives a
rebuild of the VPS without re-searching 40 shows by hand.
→ `podcasts.py`, reuse `rss.py`.

### 1.7 Media retention policy · S — ✅ Shipped
`/media/` accumulates the original MP3 **and** the ad-free re-encode for every
episode ever played, forever. On a small VPS that is the failure mode that ends
the project. Auto-delete audio for played episodes older than _N_ days, keep the
transcript (which is the valuable part and 1000× smaller), re-download on demand.
→ `daily-sync.py`, plus a disk-usage line in the UI.

---

## Tier 2 — library organization

### 2.1 Playlists, manual and smart · M — ✅ Shipped
_Pocket Casts 8.0, and Metacast's "coming soon"._ The smart half is nearly built:
`FeedFilterBar.tsx` already carries unplayed / transcribed / distilled / ad-free /
downloaded plus tag and title filters, and `EpisodeListFilter.tsx` the per-show
subset. Persist a named rule set and it becomes a Smart Playlist ("under 25 min,
unplayed, #tech"). Manual playlists are the same table as the queue with a name.

### 2.2 Inbox semantics · S — ✅ Shipped
_Metacast's "podcast inbox — like email but for podcasts."_ The home feed is
already a unified list; what is missing is the read/unread contract: a badge
count, mark-all-seen, and "only show me what arrived since I last looked."

### 2.3 Filter/sort by duration · S — ✅ Shipped
Falls out of 2.1 but earns its own line: "I have 20 minutes" is the most common
real query, and duration is already stored.

---

## Tier 3 — cross-episode reach (where a self-hosted archive wins)

This is the tier no competitor can do well, because they do not have your whole
history sitting in one SQLite file.

### 3.1 Ask my library · M — ✅ Shipped
_Shipped 2026-08-31, as sketched: planned keyword searches, retrieval through
the existing FTS index ranked by how many searches agree on a passage, then an
answer citing each with a deep link into the player. Retrieval needed OR/prefix
matching to work at all — the AND semantics the search box uses found nothing
for a planned query like "model evals". Verified end to end through the real
agent CLI in ~15s, including a French passage retrieved for an English
question._

### 3.2 Semantic search alongside keyword · M — ✅ Shipped
_Shipped 2026-08-31. Overlapping ~60s windows, embedded through a swappable
backend (mistral / local / off), stored as float32 blobs and searched by a linear
dot-product pass — no vector extension, and no numpy required on the box. Ask
fuses the two retrieval paths by reciprocal rank. Building the index is opt-in,
because the automatic backend would otherwise start sending transcript text to a
hosted API on upgrade._

### 3.3 Weekly digest · S
Telegram is already wired (`researcher.py`, `daily-sync.py`). Sunday morning: what
arrived, what you finished, the best distills of the week, one thing worth
listening to that you skipped. Cheap, and it makes the nightly job's work visible.

### 3.4 Entity/topic index · M
The note builder already extracts a `mentioned` list (books, people, papers,
tools). Persist those as first-class rows and you get "every book mentioned across
my library" — the single highest-value artifact this app could produce, and one no
SaaS podcast app offers.

### 3.5 Highlight resurfacing · S
Readwise's whole business, on your own data: a daily card with three old
bookmarks/distills, link back to the moment. Now unblocked: 1.2 shipped, so
there is a corpus of listener-made highlights to resurface.

---

## Tier 4 — new sources

### 4.1 Private / paid RSS feeds · M
_Both Metacast and Pocket Casts sell this._ Patreon, Substack, member-only feeds:
tokenized URLs, sometimes HTTP basic auth. Currently `subscriptions` assumes a
public feed URL.
→ store per-feed credentials, pass through in `rss.py` **and** in
`downloader.py` (the enclosure URL usually carries the token too).
Note the secret-handling implication: those URLs must never end up in a share
link or an exported note.

### 4.2 Audio file upload · S
_Podwise supports it._ Drop an `.mp3`/`.m4a`/`.wav` and it becomes an episode with
the full pipeline on top. The plumbing already exists for YouTube; this is a
smaller version of it, and it covers voice memos, conference recordings, lecture
audio.

### 4.3 Apple/Spotify link resolution · S
Paste any podcast URL and resolve it to the RSS feed (iTunes lookup API for Apple;
title match via PodcastIndex for Spotify). Removes the "search by name and hope"
step when someone sends you a link.

---

## Tier 5 — agent surfaces (the natural fork of this project)

Podwise ships a **CLI**, an **agent skill** and an **MCP server**. For an app that
already shells out to `codex exec`, this is the cheapest differentiation available.

### 5.1 MCP server · M
Expose `search_transcripts`, `get_transcript`, `list_distills`, `ask_episode`,
`add_youtube` over MCP. Then any Claude Code / Codex session can cite your
listening history. Given the workspace already runs MCP servers under
`infra/mcp/`, this is a thin FastAPI-to-MCP shim over existing routers.

### 5.2 CLI · S
`distillpod search "…"`, `distillpod transcript <id> --md`. Mostly argparse over
`api/client.ts`'s endpoints; also makes the nightly jobs scriptable by hand.

### 5.3 Skill · S
A `podcast-memory` skill wrapping 5.1 so the agent knows when to consult your
library unprompted.

---

## Tier 6 — bigger bets

### 6.1 Speaker diarization · L
Metacast exports speaker names; DistillPod's transcript has no speaker
dimension at all — `stt.py` returns words and times, and `LiveTranscript.tsx`
only infers line breaks from pauses. For
interview podcasts, "who said this" changes the value of a quote entirely.
Voxtral does not return speakers, so this means a separate diarization pass
(pyannote / whisperx) plus a name-guessing step from the intro — genuinely heavy
on a small VPS. Worth it only if you mostly listen to interviews.

### 6.2 Reading language ≠ audio language · M
_Podwise's "any language", 12 of them._ Keep the transcript verbatim in the source
language — the README is right that a translated transcript poisons search and
makes distill quotes into inventions — but render **summary, chapters, key points
and insights** in a chosen reading language. For a French/English mixed library
that is a real daily improvement, and it is one extra field in existing prompts.

### 6.3 In-app mind map · S/M
`note_builder.py` already produces a labelled-node diagram for the Obsidian note.
Render it in the episode view too. Podwise sells this as a headline feature; here
it is already computed and thrown away.

### 6.4 Audio clip export · M
Share a 30-second clip with burned-in subtitles instead of a text quote. ffmpeg
plus word timings; the most shareable artifact the app could make. Metacast users
resort to screen recording for this.

### 6.5 Offline PWA · M
`manifest.json` exists, no service worker. Cached audio + transcript for a flight
would make the mobile-first claim complete.

### 6.6 Listening stats / year in review · S
_Pocket Casts "Playback"._ Hours, top shows, streaks, most-distilled episode. Pure
SQL over `playback` and `gists`, and a good excuse for one nice-looking page.

### 6.7 Web push · M
Notifications currently go to Telegram only. Web push would cover "transcription
finished" and "new episode from a followed show" without a second app — but it
means VAPID keys and a service worker, so it pairs with 6.5.

---

## Explicitly not worth doing

- **Themes.** Pocket Casts sunset a theme in Jan 2026 precisely because it caused
  "a disproportionate number of UI glitches" for little use. One dark theme is enough.
- **TV / car-screen apps.** Pocket Casts shipped TV in Jul 2026; for a single-user
  self-hosted web app the return is nil.
- **Funding / creator support links.** Real feature, wrong project.
- **Social feeds, follower counts, comments.** The share link already covers the
  only sharing that matters here.

---

## What to do next

The original next-three (queue, bookmarks, retention) all shipped, along with the
rest of Tier 1 and Tier 2. The ranking for what follows:

1. **3.4 entity/topic index** — the note builder already extracts `mentioned`
   on every export; persisting those rows turns them into "every book mentioned
   across my library".

Then **4.1 private feeds** if any paid subscription is worth having here, or
**5.1 MCP server** to make the library legible to the agents already on the box.

---

## Sources

- Metacast — https://metacast.app/features (transcripts, chapters & summaries,
  playlists, discovery/OPML, private & Patreon feeds, inbox, sharing)
- Podwise — https://podwise.ai/#features (summary, mind map, insights, ask
  anything, 12 languages, exports to Notion/Readwise/Obsidian/Logseq/Markdown/PDF,
  CLI, agent skill, MCP, multi-source input incl. private feeds and audio upload)
- Pocket Casts feature blog — https://blog.pocketcasts.com/category/feature/
  (AI chapters Jul 2026, highlighted transcripts Jun 2026, dynamic type Mar 2026,
  Playback year-in-review Dec 2025, playlists & smart playlists Nov 2025,
  new search Nov 2025, notifications Jul 2025, funding Jun 2025, TV Jul 2026)

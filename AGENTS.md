# DistillPod

AI-powered podcast player with transcription, AI chat, gist extraction, ad removal, and deep-research reports.

## Tech Stack

- **Backend:** Python 3 / FastAPI / uvicorn / aiosqlite (SQLite with WAL)
- **Frontend:** React 18 / TypeScript / Vite / Tailwind CSS / Zustand
- **AI:** Codex CLI (`codex exec`) called via subprocess through `services/llm.py`; transcription via `services/stt.py` (Mistral Voxtral or faster-whisper)
- **Auth:** Google OAuth2 with JWT session cookies (authlib + python-jose)
- **External API:** Podcast Index API for search/discovery

## Local Development

### Backend

```bash
cd backend
pip install -r requirements.txt
# Create .env or export env vars (see below)
uvicorn main:app --host 127.0.0.1 --port 8124 --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev          # Vite dev server on :5173
npm run build        # Production build -> frontend/dist/
```

The backend serves the built frontend from `frontend/dist/` (SPA catch-all). In dev, the Vite dev server runs separately on port 5173.

### Tests

```bash
# Backend unit tests
pytest                          # asyncio_mode = strict

# Frontend E2E
cd frontend && npx playwright test
```

## Deployment

- **Service:** `distillpod.service` (systemd)
- **Port:** 8124 (localhost only; reverse-proxy expected)
- **Restart:** `sudo systemctl restart distillpod`
- **Logs:** `journalctl -u distillpod -f` and `logs/` directory
- **Build step:** Run `cd frontend && npm run build` before deploying frontend changes
- **DB file:** `distillpod.db` in project root (SQLite, auto-migrated on startup)

## Environment Variables

Env file: `/etc/distillpod.env` (mode 600, owned by root, loaded via `EnvironmentFile=` in systemd)

| Variable | Purpose |
|---|---|
| `PODCAST_INDEX_API_KEY` | Podcast Index API key |
| `PODCAST_INDEX_SECRET` | Podcast Index API secret |
| `LLM_BACKEND` | Agent CLI: `codex` (default) or `claude` |
| `LLM_BIN` | Path to the CLI binary (optional, falls back to PATH) |
| `LLM_MODEL` | Model passed to `codex exec -m` (optional) |
| `PUBLIC_URL` | Public-facing domain (CORS, OAuth redirect, report URLs) |
| `GOOGLE_CLIENT_ID` | Google OAuth2 client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth2 client secret |
| `ALLOWED_EMAILS` | Comma-separated email allowlist |
| `SESSION_SECRET` | JWT signing secret (`openssl rand -hex 32`) |
| `STT_BACKEND` | Transcription: `auto` (default), `voxtral`, `mlx`, or `whisper` |
| `MISTRAL_API_KEY` | Mistral key; its presence makes `auto` pick Voxtral |
| `STT_MODEL` | Voxtral model (default: `voxtral-mini-latest`) |
| `MLX_MODEL_REPO` | Pin an MLX repo; else derived from `WHISPER_MODEL` |
| `STT_LANGUAGE` | ISO code e.g. `fr`; empty = auto-detect |
| `WHISPER_MODEL` | Whisper model size (default: `medium`) |
| `WHISPER_DEVICE` | Whisper device (default: `cpu`) |
| `YTDLP_BIN` | Path to `yt-dlp` (optional, falls back to PATH) |
| `TELEGRAM_BOT_TOKEN` | Telegram notifications (optional) |
| `TELEGRAM_CHAT_ID` | Telegram chat ID (optional) |

## Architecture

```
backend/
  main.py              # FastAPI app, middleware, SPA serving
  config.py            # pydantic-settings (Settings)
  database.py          # aiosqlite connection + schema + migrations
  models.py            # Pydantic models
  middleware/auth.py   # JWT session auth middleware
  routers/
    podcasts.py        # Search, subscribe, feed (+ filters), suggestions
    tags.py            # Tags on subscriptions: CRUD + assignment
    search.py          # Full-text search across transcripts
    youtube.py         # Add a YouTube video as an episode
    player.py          # Download, stream audio, transcription status, chapters
    gists.py           # Create/list/delete AI gists (distillations)
    bookmarks.py       # Transcript quotes kept with no model call
    ask.py             # Library-wide questions (retrieval lives in librarian.py)
    queue.py           # Up Next, server-side (the client mirrors it locally)
    playlists.py       # Playlists: manual membership or a stored smart rule
    storage.py         # Media disk usage, retention policy, prune
    chat.py            # Per-episode AI chat
    research.py        # Deep research reports from gists
    auth.py            # Google OAuth2 login/logout
  services/
    llm.py             # Agent CLI adapter — the ONLY place a model is invoked
    transcript_search.py # FTS hits -> timestamped snippets
    stt.py             # Speech-to-text adapter — Voxtral, mlx-whisper, or faster-whisper
    podcast_index.py   # Podcast Index API client
    rss.py             # RSS feed parser
    youtube.py         # yt-dlp adapter: metadata, captions, audio (no DB)
    downloader.py      # Episode audio downloader (dispatches YouTube to yt-dlp)
    transcriber.py     # transcription orchestration + DB writes (STT lives in stt.py)
    snip_engine.py     # Gist extraction from transcript
    bookmark_engine.py # Sentence around a timestamp — pure transcript lookup
    episode_query.py   # THE place episode filters become SQL (feed + smart playlists)
    retention.py       # What media costs; what may be deleted
    opml.py            # OPML import/export
    ad_detector.py     # Ad segment detection (the model call)
    audio_processor.py # Silence measurement, the ffmpeg cut, and its mapping
    timeline.py        # Original <-> clean-cut clock conversion
    jobs.py            # Turn-taking lanes: one job at a time per resource
    librarian.py       # Plan searches -> retrieve passages -> answer with citations
    embeddings.py      # Text -> vectors (mistral | local | off), normalised here
    semantic_index.py  # Transcript windows + vector search feeding the librarian
    chapterizer.py     # Auto-chapter generation
    researcher.py      # Deep research report generation
frontend/
  src/                 # React SPA (pages, components, stores, api)
    lib/clipboard.ts   # copy/download helpers (execCommand fallback for non-HTTPS)
scripts/
  daily-sync.py        # Cron job: sync feeds
  suggest-podcasts.py  # Cron job: AI podcast suggestions
```

## Key Notes

- The backend serves the built frontend as a SPA catch-all -- no separate web server needed.
- Protected API routes: `/gists`, `/podcasts`, `/player`, `/chat`, `/research`,
  `/tags`, `/search`, `/youtube`, `/queue`, `/bookmarks`, `/playlists`, `/storage`.
  Auth routes and frontend assets are public. `/chat` and `/research`
  were missing from that list and were reachable unauthenticated — anything new
  that reads user data or spends the model subscription must be added there.
  `/storage` can delete media, so it is not optional.
- **SPA paths and API prefixes share one namespace.** The catch-all that serves
  the SPA runs last, so a router registering a path the SPA also uses makes that
  screen return JSON on reload — and only on reload, which is the case nobody
  clicks through while developing. `/queue` and `/playlists/:id` were lost this
  way; the SPA now uses `/up-next` and `/library/playlists/:id`.
  `tests/test_spa_routes.py` asserts every SPA path is still unclaimed — add to
  its `SPA_PATHS` when you add a screen.
- Episode filters (`unplayed`, status, tag, duration, sort) live in
  `services/episode_query.py` and nowhere else. The feed and smart playlists
  both build their query there, so a rule cannot mean one thing on Home and
  another inside a playlist. Counts there are scalar subqueries, not joins:
  joining `gists` and `bookmarks` in one statement multiplies the rows and an
  episode with 3 distills and 2 bookmarks reports 6 of each.
- `episodes.created_at` is stamped by a **trigger**, not by callers. Six insert
  sites feed that table (RSS, channel sync, the nightly job, …) and SQLite
  rejects a non-constant column default in `ALTER TABLE`, so the database does
  it. The inbox counts from it rather than `published_at`, because a channel
  import backfills years of uploads at once and none of those are news.
- Two ways to keep a moment, on purpose: **⚗️ distill** costs a CLI round trip
  and ~30s; **🔖 bookmark** costs an INSERT. Do not merge them — the cheap one
  is only useful because it is cheap, and in a vault six months later, which of
  the two produced a quote is the difference between standing behind it and
  having to check it.
- **Background work takes turns per resource** (`services/jobs.py`). Nine places
  started work with `create_task` and the cron script did the same work from
  another process, so a play could race the nightly sync for yt-dlp and for the
  two cores. Lanes — `youtube`, `media`, `stt`, `llm`, `web` — are serialised
  independently, so fetching audio does not wait on a transcription, and turns
  in `youtube` are spaced because being refused there costs hours. Priority is a
  contextvar (`jobs.priority_scope`), so a request handler marks the play path
  `USER` once and every service call underneath inherits it; housekeeping stays
  at the default `BACKGROUND`. Serialisation crosses processes through a lock
  file per lane, which is why `set_lock_dir` is called both at app startup and
  at the top of `scripts/daily-sync.py`. Adding a new yt-dlp or model call means
  wrapping it in `async with jobs.lane(...)`, not adding another `create_task`.
  Lanes are reentrant per task — nesting is natural (fetching captions is one
  turn whose helper takes the same lane) and would otherwise deadlock — and
  `key=` deduplicates: two browsers pressing play on one episode produce one
  download, with the second caller waiting and then told `turn.duplicate`. The
  in-memory guards in `player.py` cannot see across processes, so the nightly
  script also skips episodes already `processing` or `queued`.
- **A YouTube episode tries its own captions before speech-to-text.** They carry
  word-level timings, cost nothing and arrive in seconds. The ingest path always
  did this; the play path did not, so any video the nightly caption pass had not
  reached was sent to a paid backend to re-derive a transcript YouTube would
  have handed over. See `transcriber._obtain_words`.
- **Research refuses rather than degrading.** `services/researcher.py` needs
  `TAVILY_API_KEY`: the agent CLI runs sandboxed with no network, so a search API
  is the only way out of the box. With the key unset in production, every search
  returned `[]`, nothing distinguished that from "no key", the model was asked to
  analyse sources it had not been given, and the pipeline marked the result
  `done` and announced it. So: no key or no sources is now an `error` with a
  reason, and no HTML is written. The premise also includes the episode, its
  summary and the transcript around the moment — a distilled quote can be five
  seconds long and name nothing searchable.
- **Semantic search is optional and opt-in.** `EMBED_BACKEND=auto` resolves to
  Mistral when a key is present, which is usually the case for Voxtral — so the
  *first* index is only ever built by pressing the button
  (`semantic_index.opted_in`), because embedding sends transcript text off the
  box and an upgrade must not start doing that on its own. After that the
  nightly job keeps it level. With no backend, Ask falls back to keyword
  retrieval, which is how it shipped and still works; every code path here
  returns empty rather than raising when embeddings are unavailable.
- **Library-wide questions retrieve before they generate.** No model call can
  hold three hundred transcripts, so `services/librarian.py` plans keyword
  searches (one small call), retrieves passages through the existing FTS index,
  and answers from those alone (one call), citing each. Two things there are
  load-bearing: retrieval uses `fts_any_query` — OR of prefix-matched tokens —
  rather than `fts_query`'s AND, because the queries are a model's guess at the
  words a speaker used and "model evals" found nothing in an episode saying
  "models" and "evals"; and when retrieval comes back empty the answer is a
  fixed sentence rather than a second model call, which is both cheaper and more
  honest. Prefix matching over the accent-folding tokenizer also bridges
  languages: "model" reaches "modèle".
- **The clean cut runs on its own clock.** It is a concatenation of the kept
  spans, so after the first cut it is behind the original by whatever was
  removed. Everything stored — playback positions, distills, bookmarks,
  chapters, transcript timings — uses the ORIGINAL timeline, and the conversion
  happens at the edges: endpoints that accept a position take
  `source: "original" | "clean"` and convert server-side
  (`services/timeline.resolve`), while the player converts locally for seeking
  and read-along (`frontend/src/lib/timeline.ts`, a mirror of the Python).
  Keep the two implementations in step; `tests/test_timeline.py` is the
  specification. The stored mapping is also corrected to the audio ffmpeg
  actually wrote — cutting an MP3 lands on frame boundaries and each concat join
  pads, which over a couple of hundred shortened pauses would drift by seconds.
- Retention (`services/retention.py`) is off by default and never touches
  anything queued, part-heard, or (unless asked) unplayed. It deletes audio
  only: the transcript is a thousandth of the size and cannot be re-derived,
  the audio can just be downloaded again.
- The read-along view (`frontend/src/components/LiveTranscript.tsx`) takes its
  position from the audio element on an animation frame, not from AudioContext's
  `currentTime`: that state updates on `timeupdate`, roughly 4Hz, which is
  visibly behind the voice, and every update re-renders the whole player. State
  is set only when the active *word index* changes, and only the active line
  renders per-word spans — styling all ~10k words of an hour-long episode
  individually would make each highlight a full-document restyle.
- `GET /player/transcript/{id}` sends `[start, end, text]` triples rather than
  objects. Repeating three JSON keys per word roughly doubles a payload that is
  already ~10k words an hour, and there is no gzip middleware (adding one would
  also try to compress the audio responses).
- Transcript search is two-stage: FTS5 (`transcripts_fts`) picks the episodes,
  then a Python walk over `words_json` finds the timestamp. FTS5 can produce a
  snippet but not a position in the audio, and the timestamp is the whole point.
  `index_transcript` must be called on every transcript write or the episode
  becomes unsearchable.
- Feed filtering is server-side (`GET /podcasts/feed?q=&tag_id=&status=`). It has
  to be: the feed is capped, so filtering the already-truncated page in the client
  would hide matches older than the cap.
- Playback position and played state live in the `playback` table, so an
  episode started on the phone resumes on the laptop. localStorage is still
  written first and read synchronously — it makes resume instant and works
  offline — but the server is the copy the devices reconcile against, per
  episode, last-write-wins, on `AudioContext.hydrateProgress` at startup.
  `played` merges as a union instead, since it only ever goes one way.
- `played` means the episode was *opened*, not finished: `markPlayed` is called
  at the end of `loadEpisode`. It drives the "unplayed" feed filter. Anything
  asking "is there something to resume" must test `position > 0` instead —
  gating on `played` silently discards the position of every episode ever
  started. The "Continue listening" rail and the hydration merge both got this
  wrong once.
- The "unplayed" filter still runs client-side, but now over synced state, so
  it no longer disagrees between devices.
- `TEST_MODE=true` bypasses auth entirely -- never set in production.
- The model is invoked as a CLI subprocess, never via an HTTP API. Every call goes
  through `services/llm.py` — add features there, not with a new `subprocess.run`.
- `codex exec` writes a banner, reasoning trace and token count to stdout, so the
  adapter reads the answer from `-o/--output-last-message` instead. For structured
  replies it passes a JSON Schema via `--output-schema`, which is why callers no
  longer strip markdown fences.
- Each call runs with `--sandbox read-only --ephemeral` in a throwaway working
  directory, so the model cannot see or act on this repo.
- Transcription goes through `services/stt.py`, which must return
  `[{word, start, end}]` with the leading space kept on each word. Distill
  windows, the ad segmenter and chapter seeks all index into that array, so a
  backend that only returns phrase-level spans cannot be plugged in as-is.
- faster-whisper cannot use a Mac GPU: CTranslate2 has no Metal backend and
  rejects `mps`/`metal`. `STT_BACKEND=mlx` runs the same model on the GPU at
  roughly 8x the speed. mlx returns numpy floats, which `json.dumps` cannot
  serialise — `_transcribe_mlx` casts them, and the transcript goes straight
  into the DB, so do not remove that.
- Voxtral quantises timings to 0.1s and occasionally emits `end < start`;
  `stt._normalise` clamps that. Audio is downmixed to 16 kHz mono before upload,
  which keeps long episodes under the size limit and costs no accuracy.
- YouTube channels are subscriptions (`services/youtube_library.py`), polled by
  the nightly sync through `process_youtube_channel`. Episodes are built from
  two requests for the whole channel and none per video: the `/videos` tab for
  which uploads count (it excludes Shorts and streams by construction, and
  carries durations), and the Atom feed for the publish dates the tab returns
  as null on some channels. Do not "simplify" this into a metadata call per
  video — ten of those in a row tripped YouTube's bot check and had the address
  refused for everything, single calls included, for a while afterwards. The
  transcript pass is the only part that still costs a call per video, so it is
  capped and spaced; anything it misses transcribes on first play.
- Feed-imported and hand-added videos share one id scheme (`yt-<videoId>`), so
  subscribing to a channel cannot duplicate a video already added. The listing
  upsert is `ON CONFLICT DO NOTHING` because a hand-added row has a description
  and chapters the listing does not — overwriting would be a downgrade.
- A YouTube video is ingested as an ordinary `episodes` row, which is why no
  feature had to learn about YouTube. Two things make that work: the channel is
  written as a pseudo-subscription (`yt-<channel_id>`) because the feed joins on
  one, and `services/downloader.py` dispatches YouTube audio_urls to yt-dlp — so
  play, re-download and the daily sync all get it for free.
- YouTube ingestion depends on a *current* `yt-dlp` on the box, not just any
  yt-dlp: distro packages are years stale (Ubuntu 22.04 ships `2022.04.08` and
  offers nothing newer) and fail to extract at all. The deployed VPS runs the
  official standalone binary in `/usr/local/bin`, which precedes the apt one in
  the service PATH, alongside `deno` — yt-dlp needs a JS runtime to solve
  YouTube's signature challenge, and without it extraction is degraded enough to
  lose captions and chapters on some videos. Neither is installed by `deploy.sh`.
- YouTube captions are preferred over STT because `json3` gives a timestamp per
  word. But *human-written* subtitles put a whole line in one seg, and a
  phrase-long "word" breaks every seek — `youtube._words_from_json3` splits any
  multi-word seg and shares the span across it. Do not simplify that away.
  Machine-translated caption tracks are deliberately never selected.
- Every transcript write goes through `transcriber.store_transcript`, whichever
  backend produced it, so the FTS index cannot be forgotten.
- `services/auto_snipper.py` picks highlights from a finished transcript. The
  timestamps come back from the model, so none of them are trusted: each is
  clamped into the episode's duration, snapped to real transcript words, and
  dropped if it lands within half a window of one already taken. The stored
  `text` is always the real excerpt, never the model's quote — the quote goes
  in `summary` alongside the insight, in the same shape manual distills use, so
  the UI renders both identically.
- Auto-snips are capped in the nightly job (`MAX_AUTO_SNIP_EPISODES`) and
  scoped by recency, unlike chapterization: each one is a model call over a
  full transcript, so an unscoped query would take on the whole back catalogue
  the first night. `POST /gists/auto/{episode_id}` is the on-demand route, and
  the only one that reaches YouTube videos — the sync skips `yt-` feeds.
- Background tasks (transcription, research) run via `asyncio.create_task` -- they do not survive restarts.
- Git remote: `upstream` -> `https://github.com/andrepaim/distillpod.git` (fork of andrepaim/distillpod).

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
    player.py          # Download, stream audio, transcription status, chapters
    gists.py           # Create/list/delete AI gists (distillations)
    chat.py            # Per-episode AI chat
    research.py        # Deep research reports from gists
    auth.py            # Google OAuth2 login/logout
  services/
    llm.py             # Agent CLI adapter — the ONLY place a model is invoked
    transcript_search.py # FTS hits -> timestamped snippets
    stt.py             # Speech-to-text adapter — Voxtral, mlx-whisper, or faster-whisper
    podcast_index.py   # Podcast Index API client
    rss.py             # RSS feed parser
    downloader.py      # Episode audio downloader
    transcriber.py     # transcription orchestration + DB writes (STT lives in stt.py)
    snip_engine.py     # Gist extraction from transcript
    ad_detector.py     # Ad segment detection
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
  `/tags`. Auth routes and frontend assets are public. `/chat` and `/research`
  were missing from that list and were reachable unauthenticated — anything new
  that reads user data or spends the model subscription must be added there.
- Transcript search is two-stage: FTS5 (`transcripts_fts`) picks the episodes,
  then a Python walk over `words_json` finds the timestamp. FTS5 can produce a
  snippet but not a position in the audio, and the timestamp is the whole point.
  `index_transcript` must be called on every transcript write or the episode
  becomes unsearchable.
- Feed filtering is server-side (`GET /podcasts/feed?q=&tag_id=&status=`). It has
  to be: the feed is capped, so filtering the already-truncated page in the client
  would hide matches older than the cap.
- Played state is localStorage only, so the "unplayed" filter is the one that
  must stay client-side.
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
- Background tasks (transcription, research) run via `asyncio.create_task` -- they do not survive restarts.
- Git remote: `upstream` -> `https://github.com/andrepaim/distillpod.git` (fork of andrepaim/distillpod).

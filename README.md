# DistillPod ⚗️

> A self-hosted, mobile-first podcast app with AI-powered features: transcription, distillations, ad detection, chapter generation, episode chat, and deep research reports. No per-call API costs — all AI runs via a coding-agent CLI through your existing subscription.

> [!NOTE]
> **This is a fork of [andrepaim/distillpod](https://github.com/andrepaim/distillpod).** It makes both AI workloads swappable so you can fit them to your server:
> **reasoning** runs on the **Codex CLI** by default (`LLM_BACKEND=claude` restores upstream's Claude CLI), and **transcription** uses **Mistral Voxtral** when a `MISTRAL_API_KEY` is present, falling back to local faster-whisper otherwise (`STT_BACKEND` pins either).
> See [Choosing your backends](#choosing-your-backends). The "Why I Built This" section below is the upstream author's.

---

## Screenshots

<div align="center">

| Home feed | Episode | Player | Chat | Distillation | Shared link |
|:---:|:---:|:---:|:---:|:---:|:---:|
| ![Home](screenshots/1-home.png) | ![Episode](screenshots/2-episode.png) | ![Player](screenshots/3-player.png) | ![Chat](screenshots/4-chat.png) | ![Distills](screenshots/5b-distill-detail.png) | ![Unauthorized](screenshots/7-unauthorized.png) |
| Latest episodes across subscriptions | Episode detail with AI summary | Full-screen player with chapters | AI-generated insights + Q&A | Distillation card with Copy / Delete / Research | What a recipient sees when following a shared link |

</div>

---

## Why I Built This

I was paying for a premium podcast app specifically for its AI distillation feature — the ability to extract insights from what I was listening to on the fly. It worked well, but it felt wasteful: I was already running a VPS with the Claude CLI for my own OpenClaw agent, paying for a Claude subscription. Why route that through someone else's SaaS?

So I cut out the middleman.

DistillPod runs entirely on your own server. The AI features — distillations, ad detection, chapters, chat, research — all go through a coding-agent CLI using your existing subscription. No separate API key, no extra per-call charges on top of what you already pay, no data leaving your infrastructure to a third-party podcast app, no feature flags behind a paywall.

If you have a VPS and a Codex (or Claude) subscription, you already have everything you need to run this.

---

## Choosing your backends

Both AI workloads are swappable, because the right answer depends entirely on
what your server can actually do. Neither is hardcoded — each is one env var,
and you can mix them freely.

### 1. Reasoning — distillations, chat, ad detection, chapters, research

Everything goes through `backend/services/llm.py`, which shells out to a coding-agent
CLI authenticated by your existing subscription. No API key, no per-call billing.

| `LLM_BACKEND` | Uses | Choose when |
|---|---|---|
| `codex` *(default)* | `codex exec` | You have a Codex subscription |
| `claude` | `claude --print` | You have a Claude subscription (upstream's original setup) |

```bash
LLM_BACKEND=codex     # npm i -g @openai/codex && codex login
LLM_BACKEND=claude    # npm i -g @anthropic-ai/claude-code && claude login
```

Both need Node on the box and a one-time interactive login. Callers never know
which is in use, so switching is genuinely a one-line change.

### 2. Transcription — turning audio into a word-level transcript

This is the one that will actually strain a small VPS. Everything downstream
reads the transcript, so a weak one silently degrades every feature.

| `STT_BACKEND` | Uses | Cost on your server |
|---|---|---|
| `auto` *(default)* | Voxtral if a key is set, else `mlx` if importable, else `whisper` | — |
| `voxtral` | Mistral hosted API | Negligible: an ffmpeg downmix and one HTTP request |
| `mlx` | mlx-whisper on the Apple Silicon GPU | Local, but ~8x faster than the CPU path |
| `whisper` | faster-whisper, on CPU | ~1.5GB RAM for `medium`, pins a core for minutes |

**On a Mac, `WHISPER_DEVICE` cannot use the GPU.** faster-whisper is built on
CTranslate2, which has no Metal backend at all — it rejects `mps` and `metal`
outright and was not compiled with CUDA. Use `STT_BACKEND=mlx` instead
(`pip install mlx-whisper`), which runs the same Whisper model on the GPU.

Measured on the same machine and clip with `medium`:

| Backend | Speed | 27-min episode |
|---|---|---|
| `voxtral` | — | ~35s |
| `mlx` (GPU) | 14.5x realtime | ~2 min |
| `whisper` (CPU) | 1.79x realtime | ~15 min |

**Very long episodes:** Voxtral accepts up to 3 hours per request, so anything
longer is split into 1-hour pieces and stitched back onto one timeline
automatically — a 5-hour Lex Fridman episode transcribes fine. Splitting costs
roughly 1.5% of words at the cut boundaries, so episodes under an hour are sent
whole and pay nothing.

**On a 27-minute episode:** Voxtral takes **~35s**. faster-whisper `small` takes
**~7 min** on a warm Apple M-series core (~3.9× realtime) — and a shared vCPU
with `medium` or `large-v3` will be considerably slower again.

```bash
# Small VPS, or non-English podcasts: let Mistral do the heavy lifting
MISTRAL_API_KEY=sk-...
STT_LANGUAGE=fr          # optional; skips auto-detection

# Everything stays on your box — no key needed
STT_BACKEND=whisper
WHISPER_MODEL=medium
```

### Which should you pick?

- **Small or shared VPS** (1–2 vCPU, ≤2GB RAM) — use Voxtral. Local Whisper will
  either swap itself to death or make transcription take longer than listening
  to the episode.
- **Non-English podcasts** — use Voxtral. It is markedly better on French than
  `medium`, and you would otherwise need `large-v3`, which is heavier still.
- **Audio must not leave your server** — use `STT_BACKEND=whisper`. This is the
  one hard trade-off: Voxtral uploads the episode audio to Mistral. Setting the
  backend explicitly means a stray `MISTRAL_API_KEY` in the environment can
  never silently override that choice.
- **No Mistral billing** — use whisper, and size the model to your CPU.

Transcription is the only stage that leaves your infrastructure, and only if you
choose Voxtral. The reasoning CLI always runs locally against your own
subscription either way.

---

## Features

- **📰 Home feed** — unified list of the latest episodes across all subscriptions, sorted by date. Shows distillation count per episode.
- **🔎 Transcript search** — search what was actually *said* across every episode you have transcribed, and tap a result to jump straight to that moment in the audio. Accent-insensitive, so `retro` finds `rétro-ingénierie`. Search → **In my episodes**.
- **↔️ Cross-device resume** — where you are in an episode and which ones you have opened are kept server-side, so you can start on the laptop and pick it up on the phone at the same second. A **Continue listening** rail on the home feed shows what you are part-way through. localStorage still backs it, so resume stays instant and works offline.
- **🎤 Read along** — tap *Read along* in the player to follow the transcript while it plays, karaoke-style: the spoken line lifts out of the dimmed text and the current word is highlighted as it is said. Tap any line to jump there. Word-level timings make this exact rather than approximate.
- **🏷️ Tags & filters** — tag your podcasts (`#tech`, `#français`, …) and filter the feed by tag, by title search, or by state: unplayed, transcribed, distilled, ad-free, downloaded. Filters combine, and searching looks across your whole library rather than just the page on screen.
- **🔍 Search** — find podcasts via the iTunes Search API (no key needed). When the search box is empty, a **🤖 Suggested for you** section surfaces daily AI-generated recommendations based on your listening history.
- **📚 Library** — browse your subscribed podcasts and their episode lists with transcript status badges.
- **▶️ Fullscreen Player** — Spotify-style slide-up player with chapter navigation, ad-free toggle, and distillation controls.
- **⚙️ Auto-snips** — the nightly job picks the handful of moments per new episode that would have been worth tapping, and saves them as distills, badged so you know they were suggested rather than chosen. Also available on demand from any transcribed episode via **⚙ Suggest highlights**, which is the only route for YouTube videos and anything older than the job's window. Measured at ~13s for a 12-minute episode.
- **⚗️ Distill** — tap at any moment while listening. Captures the last 60 seconds of transcript, calls the agent CLI, and returns a verbatim quote and a 1–2 sentence insight (~30s).
- **✂️ Ad-free audio** — after transcription, the model classifies ad segments and ffmpeg cuts them out. Stream the clean version from the player.
- **📖 Chapters** — the model generates 4–10 named chapters with timestamps from the full transcript. Tap any chapter to jump directly.
- **💬 Episode chat** — ask questions about any transcribed episode. The model answers using the full transcript as context. History kept per episode (capped at 50 messages). Copy the whole conversation as Markdown or download it as a `.md` file from the chat header.
- **📺 YouTube videos** — paste a link under Search → **YouTube** and the video joins your library as an ordinary episode: audio you can listen to, a word-level transcript, and every AI feature on top of it. Grouped under its channel, so videos filter and search like any show. See [YouTube videos](#youtube-videos).
- **🔬 Research** — trigger a deep research report from any distillation. The model generates queries, Tavily runs web searches, then synthesizes findings into an HTML report. Delivered via Telegram.
- **📋 Distillations library** — all your distillations grouped by episode. Copy, delete, or trigger research from any entry.
- **⚡ Stale-while-revalidate caching** — data is cached in localStorage with a 30-minute TTL and refreshed silently in the background.

---

## How It Works

All AI features go through a single adapter, `backend/services/llm.py`, which shells
out to the agent CLI. Nothing else in the codebase invokes a model.

```python
from services import llm

# free text
answer = llm.run(prompt, timeout=120)

# structured reply, constrained by a JSON Schema — no fence-stripping needed
data = llm.run_json(prompt, schema=CHAPTERS_SCHEMA, timeout=240)
```

Under the hood, on the default Codex backend:

```python
subprocess.run([
    "codex", "exec",
    "--skip-git-repo-check",
    "--sandbox", "read-only",     # pure text tasks; no tool use
    "--ephemeral",                # don't accumulate session files
    "-C", scratch_dir,            # throwaway cwd: the model never sees this repo
    "-o", out_file,               # the ONLY clean source of the final message
    "--output-schema", schema_file,
    prompt,
])
```

Two details matter. `codex exec` prints a banner, reasoning trace and token count to
stdout, so the answer is read from `--output-last-message` rather than parsed out of
stdout. And `--output-schema` constrains the reply to a JSON Schema, which is why the
callers no longer strip markdown fences.

The CLI authenticates through your existing subscription — no API key, no per-call billing.

**Setup:** install the Codex CLI and log in once:

```bash
npm install -g @openai/codex
codex login
```

To use Claude instead, set `LLM_BACKEND=claude` and install its CLI:

```bash
npm install -g @anthropic-ai/claude-code
claude login
```

### AI features summary

| Feature | Trigger |
|---|---|
| Distillation (quote + insight) | User taps ⚗️ while listening |
| Ad detection + audio cut | After transcription completes |
| Chapter generation + summary | Daily sync or manual script run |
| Episode chat | User opens chat or sends message |
| Deep research report | User triggers from a distillation |
| Podcast suggestions | Daily cron at 09:00 BRT |

### Transcription

When you tap Play:

1. `POST /player/play` triggers a background download + transcription task
2. The episode MP3 is downloaded to `/media/` (streaming, skips if cached)
3. `services/stt.py` transcribes to word-level timestamps
4. Word-level timestamps saved to `transcripts` table
5. Ad detection runs non-fatally after transcription
6. The ⚗️ Distill button unlocks when `transcript_status = done`

#### Choosing a transcription backend

`STT_BACKEND=auto` (the default) uses **Voxtral** when `MISTRAL_API_KEY` is set,
and falls back to **faster-whisper** otherwise. Set it to `voxtral` or `whisper`
to pin one explicitly — an explicit `whisper` wins even if a key is present.

| | Voxtral (`voxtral-mini-latest`) | faster-whisper |
|---|---|---|
| Speed | ~35s for a 27-minute episode | ~7 min for the same (`small`, warm, Apple M-series CPU); slower on a typical VPS and with `medium`/`large-v3` |
| Privacy | Audio is uploaded to Mistral | Never leaves your server |
| Cost | Per-minute API billing | Free, but pins a CPU core |
| Non-English | Strong | Needs `large-v3` to compete |
| Install | Nothing | ~1.5GB model + CTranslate2 |

Audio is downmixed to 16 kHz mono before upload, which keeps long episodes under
the API size limit and costs nothing in accuracy — speech recognition gains
nothing from stereo or music-grade bitrates.

Whisper model sizes (`WHISPER_MODEL`): `base` fastest, `small`, `medium`
(default), `large-v3` best. Set `STT_LANGUAGE=fr` to skip auto-detection.

---

## YouTube videos

Sometimes what you want to listen to is on YouTube. Paste the link under
Search → **YouTube** and it becomes an episode — nothing downstream knows the
difference, so the player, transcript search, distills, chat and research all
work on it unchanged.

```
paste link ──▶ yt-dlp -J ──▶ episode row (+ channel as a subscription)
                   │
                   ├── captions? ──▶ word-level transcript in seconds, free
                   └── none?     ──▶ yt-dlp -x mp3 ──▶ the usual STT backend
```

**Requires a current `yt-dlp` on PATH**, plus the ffmpeg you already need for
ad-free audio. Set `YTDLP_BIN` if it lives somewhere unusual.

*Current* is not pedantry, and this is the one part of the feature that will
bite on a server. YouTube changes its extractor constantly, so a yt-dlp more
than a few months old degrades and an old one fails outright — Ubuntu 22.04's
apt package (`2022.04.08`, with no newer candidate) could not extract so much as
a video title. Distro packages are the trap here: install the official
standalone binary instead, and keep it ahead of any packaged one on PATH.

```bash
# On a server, where /usr/local/bin precedes /usr/bin in the service PATH:
curl -sSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux \
  -o /usr/local/bin/yt-dlp && chmod 755 /usr/local/bin/yt-dlp
```

**A JavaScript runtime is worth installing too.** YouTube protects media URLs
with an obfuscated JS challenge, and yt-dlp now runs it in a real engine rather
than its own Python interpreter; without one it warns that extraction is
deprecated and some formats may be missing. That is not theoretical — on a box
without it, one test video came back with no captions and no chapters that a box
with it extracted fine. `deno` is the default choice (yt-dlp runs the untrusted
YouTube code inside its sandbox) and is a single binary:

```bash
curl -sSL -o /tmp/deno.zip \
  https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
python3 -c "import zipfile; zipfile.ZipFile('/tmp/deno.zip').extractall('/usr/local/bin')"
chmod 755 /usr/local/bin/deno
```

**Captions are used when the video has them.** YouTube's `json3` caption format
carries a timestamp per *word*, which is exactly the shape `services/stt.py`
produces — so a captioned video is transcribed in seconds at no cost, and the
STT backend is only woken for videos that have none. Human-written subtitles are
preferred over auto-generated ones.

**Always in the video's own language.** A French video is transcribed in French,
an English one in English. YouTube offers auto-captions machine-translated into
~157 languages, and an English transcript over French audio would make search
match words nobody said and turn distill quotes into inventions — so only the
original is ever taken, and a video whose language cannot be established falls
through to speech-to-text rather than guessing. The language is resolved from
yt-dlp's declared audio language, failing that from the single `<lang>-orig`
caption key (which names the language speech recognition ran on), failing that
from a lone hand-written subtitle track. Within that language the `-orig` track
wins, since the bare language code is nominally the translation back into the
same language.

Two caveats worth knowing:

- **Auto-caption timings are per word; human-written ones are per line.** A
  subtitle line is split back into words and its span shared out across them, so
  seeks land within a word rather than at the start of the line.
- **No ad detection on the caption path.** Ad segmentation runs as part of an STT
  transcription, so a captioned video skips it. Sponsor segments stay in.

**Paste a channel link to subscribe to it** — `youtube.com/@LowLevelTV`, its
`/videos` tab, or a `/channel/UC…` URL all work. New uploads then arrive nightly
the way a podcast's episodes do, and the channel appears in your library like
any other show. Adding a single video also creates its channel as a
subscription, so videos group under whoever made them.

**Regular videos only.** Shorts and live streams never arrive. The listing comes
from the channel's `/videos` tab, which excludes both by construction, with a
duration floor and a live-status check as a backstop.

**A subscription costs no disk.** Episodes are created from the listing alone,
and audio is only ever downloaded when you actually play something. Where a
video has captions, its transcript costs nothing either.

Two requests per channel and none per video, which is deliberate: asking yt-dlp
for each video's metadata on subscribing was enough to trip YouTube's "confirm
you're not a bot" check and get the address refused for a while. The `/videos`
tab says which uploads count and how long they run; the channel's Atom feed
supplies the publish dates the tab returns as null for some channels. The
transcript pass is the only part still costing a call per video, so it is capped
and spaced out — whatever it does not reach transcribes on first play.

The uploader's own chapter marks are imported when present — free, and better
than generated ones.

Re-adding a video you already have is a no-op that returns the existing episode.

---

## Stack

| Layer | Tech |
|---|---|
| Frontend | [React 18](https://react.dev/) + [TypeScript](https://www.typescriptlang.org/) + [Vite](https://vitejs.dev/) + [Tailwind CSS v3](https://tailwindcss.com/) |
| Client routing | [React Router v6](https://reactrouter.com/) |
| State | [Zustand](https://zustand-demo.pmnd.rs/) |
| Backend | [FastAPI](https://fastapi.tiangolo.com/) + [uvicorn](https://www.uvicorn.org/) |
| Database | [aiosqlite](https://aiosqlite.omnilib.dev/) — async SQLite (WAL mode) |
| Transcription | [Voxtral](https://mistral.ai/) hosted STT, or [faster-whisper](https://github.com/SYSTRAN/faster-whisper) locally (CTranslate2, int8) |
| AI | Codex CLI (`codex exec`), or Claude CLI (`claude --print`) |
| RSS | [feedparser](https://feedparser.readthedocs.io/) |
| HTTP client | [httpx](https://www.python-httpx.org/) |
| Auth | [authlib](https://docs.authlib.org/) — Google OAuth2 |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         User (browser)                       │
│              https://your-domain.example.com                 │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTPS (Apache reverse proxy)
┌──────────────────────────▼──────────────────────────────────┐
│              FastAPI app  — port 8124 (localhost)            │
│                                                             │
│  ┌────────────────┐  ┌───────────────────────────────────┐  │
│  │  Static files  │  │           API Routers             │  │
│  │  (React SPA)   │  │  /auth  /podcasts  /player        │  │
│  │  /assets/**    │  │  /gists  /chat  /research         │  │
│  └────────────────┘  └──────────────┬────────────────────┘  │
└─────────────────────────────────────┼───────────────────────┘
                                      │
              ┌───────────────────────┼──────────────────────┐
              │                       │                       │
   ┌──────────▼──────┐   ┌───────────▼──────┐   ┌──────────▼──────┐
   │   SQLite DB      │   │   Media files     │   │   Agent CLI     │
   │ distillpod.db    │   │  /media/*.mp3     │   │   codex exec    │
   └─────────────────┘   └──────────────────┘   └────────────────┘
```

### Component breakdown

| Component | Responsibility |
|---|---|
| `frontend/` | React SPA — UI only, all state on backend |
| `backend/main.py` | FastAPI entry point, serves built frontend and reports |
| `backend/routers/auth.py` | Google OAuth2 flow, session cookie management |
| `backend/routers/podcasts.py` | Search, subscribe, episode listing, suggestions |
| `backend/routers/player.py` | Play trigger, audio streaming, transcript status, chapters |
| `backend/routers/gists.py` | Create, list, delete distillations |
| `backend/routers/chat.py` | Episode Q&A — init, message, history |
| `backend/routers/research.py` | Trigger + poll research reports |
| `backend/services/downloader.py` | Async MP3 download to `/media/` |
| `backend/services/stt.py` | Speech-to-text adapter — Voxtral or faster-whisper |
| `backend/services/transcriber.py` | Transcription orchestration, DB writes, async background task |
| `backend/services/llm.py` | Agent CLI adapter — the only place a model is invoked |
| `backend/services/snip_engine.py` | Timestamp window lookup + distillation |
| `backend/services/ad_detector.py` | Ad classification + ffmpeg audio surgery |
| `backend/services/chapterizer.py` | Chapter + summary generation |
| `backend/services/researcher.py` | Multi-turn research pipeline: model + Tavily → HTML report |
| `backend/services/rss.py` | RSS feed parsing |
| `backend/services/podcast_index.py` | PodcastIndex API wrapper |
| `backend/database.py` | SQLite connection, schema init, WAL mode |
| `backend/config.py` | All settings via env vars (`pydantic-settings`) |

---

## Self-hosting

### Requirements

- Python 3.10+
- Node.js 18+
- ffmpeg (for ad-free audio generation)
- yt-dlp — optional, only for [YouTube videos](#youtube-videos), and it must be **current**
- [Codex CLI](https://developers.openai.com/codex/cli) installed and authenticated (or the [Claude CLI](https://claude.ai/code) with `LLM_BACKEND=claude`)
- A VPS with a few GB of RAM if you use faster-whisper (`medium` uses ~1.5GB); far less with Voxtral

### Quick start

```bash
git clone https://github.com/your-username/distillpod.git
cd distillpod
cp .env.example .env       # edit with your settings

# Backend
cd backend
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 8124

# Frontend (production build)
cd ../frontend
npm install
npm run build              # outputs to frontend/dist/
```

FastAPI serves `frontend/dist/` at `/` automatically.

### Environment variables

Copy `.env.example` to `backend/.env` and edit:

| Variable | Required | Description |
|----------|----------|-------------|
| `GOOGLE_CLIENT_ID` | Yes | Google OAuth2 client ID |
| `GOOGLE_CLIENT_SECRET` | Yes | Google OAuth2 client secret |
| `ALLOWED_EMAILS` | Yes | Comma-separated email allowlist |
| `SESSION_SECRET` | Yes | JWT signing secret (`openssl rand -hex 32`) |
| `PUBLIC_URL` | Yes | Public-facing domain (CORS, OAuth redirect, report URLs) |
| `PODCAST_INDEX_API_KEY` | No | Podcast Index API key for richer metadata |
| `PODCAST_INDEX_SECRET` | No | Podcast Index API secret |
| `TAVILY_API_KEY` | No | Enables deep research reports |
| `TELEGRAM_BOT_TOKEN` | No | Telegram notifications |
| `TELEGRAM_CHAT_ID` | No | Telegram chat ID |
| `STT_BACKEND` | No | `auto` (default), `voxtral`, or `whisper` |
| `MISTRAL_API_KEY` | No | Mistral key; its presence makes `auto` choose Voxtral |
| `STT_MODEL` | No | Voxtral model (default `voxtral-mini-latest`) |
| `STT_LANGUAGE` | No | ISO code e.g. `fr`; empty auto-detects |
| `WHISPER_MODEL` | No | Whisper model size: `base`, `small`, `medium` (default), `large-v3` |
| `YTDLP_BIN` | No | Path to `yt-dlp`; resolved via PATH when empty |
| `MEDIA_DIR` | No | Path for downloaded MP3s (default: `media/`) |
| `REPORTS_DIR` | No | Path for HTML research reports (default: `reports/`) |

### Development

Run backend and frontend with hot reload in separate terminals:

```bash
# Terminal 1 — backend
cd backend
uvicorn main:app --host 127.0.0.1 --port 8124 --reload

# Terminal 2 — frontend
cd frontend
npm install
npm run dev   # http://localhost:5173
```

### Deploy (systemd)

#### Service file

```ini
[Unit]
Description=DistillPod — AI-powered podcast player
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/path/to/distillpod/backend
ExecStart=/usr/bin/python3 -m uvicorn main:app --host 127.0.0.1 --port 8124
Restart=always
RestartSec=5
EnvironmentFile=/path/to/distillpod/backend/.env

[Install]
WantedBy=multi-user.target
```

#### Reverse proxy (Apache)

```apache
ProxyPreserveHost On
ProxyPass / http://127.0.0.1:8124/
ProxyPassReverse / http://127.0.0.1:8124/
```

### Scheduled jobs

Two daily cron jobs run as background scripts:

| Job | Schedule | Script | What it does |
|---|---|---|---|
| `distillpod-daily-sync` | 03:00 BRT | `scripts/daily-sync.py` | RSS fetch → download → transcribe → ad detection → chapterization |
| `distillpod-suggest` | 09:00 BRT | `scripts/suggest-podcasts.py` | Model generates queries → iTunes search → 4 suggestions stored |

The daily sync pipeline per subscription:
1. Reset stuck `processing` episodes
2. Fetch latest 5 RSS episodes
3. Download recent episodes (≤48h old)
4. Transcribe via `services/stt.py`
5. Detect + remove ads (non-fatal)
6. Generate chapters + episode summary
7. Telegram alert on errors

---

## Testing

### Backend (pytest)

```bash
cd /path/to/distillpod
python3 -m pytest tests/ -v
```

Tests run against an in-memory SQLite DB. Auth bypassed via test session cookie.

### E2E (Playwright)

```bash
cd frontend
npx playwright test
```

Mobile viewport (390x844), Chromium. Requires `TEST_MODE=true` in `.env` for auth bypass.

Test files: navigation, home feed, search, library, player (fullscreen, gists, chapters, ad-free, chat), gists library, caching, SPA routing.

---

## License

MIT

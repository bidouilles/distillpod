# Contabo deployment

Installation: `/home/admin/docker-servers/distillpod` on `admin@contabo`.
Run `docker compose -f compose.contabo.yml up -d --build` in that directory.
Persistent state lives in `data/`, `media/`, `reports/`, `agent/` and `cache/`.
Keep `.env`, `agent/auth.json` and any connection-credentials files mode 600.
Do not commit them, include them in image builds, or print resolved Compose
configuration (it contains credentials).

The process runs as UID 1000, with a read-only image, no added capabilities and
no privilege escalation. The ports bind only loopback and Docker's host gateway,
not the public interface. Nginx Proxy Manager should forward
`distillpod.privatelab.eu` to **HTTP, `172.17.0.1`, port `8124`**. Enable SSL and
Force SSL; forwarding the Authorization header is required for the bot API.
The app expects `PUBLIC_URL=https://distillpod.privatelab.eu` and emits Secure
cookies, so browser login requires HTTPS at the proxy.

Password login uses `LOGIN_USERNAME` and `LOGIN_PASSWORD_HASH`; the generated
owner login is separate from `INTEGRATION_API_KEY`. To change the password:

```bash
docker compose -f compose.contabo.yml exec distillpod python3 /app/scripts/set-login-password.py
```

Paste the resulting `LOGIN_PASSWORD_HASH=...` line into the private `.env`, then
run `docker compose -f compose.contabo.yml up -d --force-recreate`. Change
`SESSION_SECRET` too if existing browser sessions should be invalidated.
Only a scrypt hash is stored in the server environment. Hashing uses stdlib
scrypt with N=2^17, r=8, p=1 and a random 16-byte salt, following
[OWASP guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
Verification runs off the event loop and allows at most two simultaneous hashes
(about 256 MiB total), with ten login attempts per minute per process. This is a
single-owner deployment with one worker; multiple workers require a shared rate
limiter. Invalid origins, oversized input and incorrect credentials are rejected.
Hash time is O(N*r*p), memory O(N*r), with fixed parameters.

The image includes ffmpeg, Codex, current yt-dlp, Deno and faster-whisper.
Transcription uses the multilingual `small` model on CPU with INT8 weights and
three threads. Model files persist in `cache/`; the first use downloads them.
`MISTRAL_API_KEY` does not select Voxtral because Compose explicitly sets
`STT_BACKEND=whisper`. Other Mistral-backed features retain their own settings.

Playback and nightly podcast sync reuse stored transcripts, then try source
transcripts before speech-to-text. Podcasting 2.0 RSS transcript links support
VTT, SRT and JSON (`segments` with `startTime`, `endTime`, `body`). Cue-level
timestamps are distributed across words, so word timing is approximate.
Missing source links permit local STT; fetch failures, missing RSS entries,
untimed text/HTML-only transcripts and invalid timecodes report an error instead
of silently running STT. Retry after a transient fetch failure. This discovers
RSS-advertised transcripts, not arbitrary publisher web pages. YouTube caption
fetch failures likewise remain errors; genuinely captionless videos use local
STT on playback. Tests: `tests/test_podcast_transcripts.py`, `tests/test_jobs.py`.
Parsing costs O(input bytes + words log words), memory O(input bytes + words),
with an 8 MiB publisher download bound. One cross-process STT lane protects
the check, computation and transcript write, preventing duplicate work.
Set `AGENT_CONFIG=/home/admin/.codex` in `.env` to use Contabo's existing Codex
login, or leave it unset to use the private `agent/` directory. Credentials are
never part of the image; the writable mount permits normal credential refresh.
Reports and playback paths in the copied
SQLite backup are adjusted to container paths. The original server is unchanged.

To retain the previous schedules, use the admin crontab with `docker compose exec -T`:
feed sync at 06:00 and suggestions at 09:00 in the host's Europe/Berlin time zone. Shared lane locks sit beside
the SQLite file in `data/locks/`, so scheduled commands and interactive jobs take
turns even though they run in different processes. Do not run both servers'
nightly jobs long-term against copies of the same library if duplicate model/STT
work is unwanted. This setup leaves the original timers untouched.

Useful checks:

```bash
docker compose -f compose.contabo.yml ps
curl --fail http://127.0.0.1:8124/health
docker compose -f compose.contabo.yml logs --tail=50
```

The private client connection file records the owner username/password and bot
API key. Import it into your password manager or consuming bot's secret store.
The application environment contains the password hash, not the plaintext.

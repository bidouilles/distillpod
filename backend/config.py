import shutil
from pydantic_settings import BaseSettings
from pathlib import Path

class Settings(BaseSettings):
    # Podcast Index API (https://podcastindex.org/developer)
    podcast_index_api_key: str = ""
    podcast_index_secret: str = ""

    # Agent CLI backing every AI feature. "codex" (default) or "claude".
    llm_backend: str = "codex"
    llm_bin: str = ""      # explicit path; resolved via PATH when empty
    llm_model: str = ""    # passed to `codex exec -m`; empty means the CLI default

    @property
    def llm(self) -> str:
        name = "claude" if self.llm_backend == "claude" else "codex"
        return self.llm_bin or shutil.which(name) or name

    @property
    def claude(self) -> str:
        """Deprecated alias kept so any straggling caller keeps working."""
        return self.llm

    # yt-dlp, for ingesting YouTube videos as episodes.
    ytdlp_bin: str = ""    # explicit path; resolved via PATH when empty

    # Storage
    media_dir: Path = Path("media")
    db_path: Path = Path("distillpod.db")
    reports_dir: Path = Path("reports")

    # Public-facing domain (used for report URLs, OAuth redirect, CORS)
    public_url: str = "https://your-domain.example.com"

    # Server
    host: str = "127.0.0.1"
    port: int = 8124
    frontend_origin: str = "http://localhost:5173"

    # Transcription. "auto" prefers a hosted key, then the GPU, then the CPU.
    stt_backend: str = "auto"                # auto / voxtral / mlx / whisper
    stt_model: str = "voxtral-mini-latest"   # voxtral only
    stt_language: str = ""                   # ISO code, e.g. "fr"; empty = auto-detect
    mistral_api_key: str = ""
    mlx_model_repo: str = ""                 # pin an MLX repo; else derived from whisper_model

    # ── Embeddings, for meaning-based search over transcripts ────────────────
    # Optional in the strongest sense: with no backend, search stays keyword-only
    # and every feature still works. Chosen the same way STT is, and for the same
    # reason — what a given box can actually run differs wildly.
    embed_backend: str = "auto"              # auto / mistral / local / off
    embed_model: str = "mistral-embed"       # mistral only
    embed_local_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

    # ── Research (web search) ────────────────────────────────────────────────
    # Without this, deep research has nothing to read: the agent CLI runs
    # sandboxed with no network, so the search API is the only way out. Research
    # refuses to start rather than writing a report from no sources.
    tavily_api_key: str = ""

    # Typesetting research reports. Optional: without the binary the PDF is
    # simply not offered, and the Markdown covers the same ground.
    typst_bin: str = ""                      # empty = look for `typst` on PATH

    @property
    def stt(self) -> str:
        if self.stt_backend in ("voxtral", "mlx", "whisper"):
            return self.stt_backend
        if self.mistral_api_key:
            return "voxtral"
        # Prefer the Apple Silicon GPU over the CPU when it is available. Both
        # are local, so this only ever trades speed for speed.
        from services.stt import mlx_available
        return "mlx" if mlx_available() else "whisper"

    # Whisper model size, shared by the mlx and whisper backends.
    @property
    def embed_engine(self) -> str:
        """Which embedding backend to use.

        `mistral` sends transcript text to a hosted API, which is a smaller step
        than the audio Voxtral already uploads — but it is still a step, so an
        explicit `local` or `off` is always honoured over the automatic choice.
        """
        if self.embed_backend in ("mistral", "local", "off"):
            return self.embed_backend
        if self.mistral_api_key:
            return "mistral"
        try:                                  # a local model, if one is installed
            import sentence_transformers  # noqa: F401
            return "local"
        except ImportError:
            return "off"

    whisper_model: str = "medium"           # base / small / medium / large-v3
    # faster-whisper only. CTranslate2 has no Metal backend, so on macOS this
    # can only ever be "cpu" — use STT_BACKEND=mlx for the GPU there.
    whisper_device: str = "cpu"
    gist_context_seconds: int = 60        # seconds of audio captured per shot

    # Auth — Google OAuth
    google_client_id: str = ""
    google_client_secret: str = ""
    allowed_emails: str = ""     # comma-separated allowlist
    session_secret: str = ""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not self.session_secret or self.session_secret == "change-me-in-production":
            import warnings
            warnings.warn(
                "SESSION_SECRET is not set or uses the default value. "
                "Set a strong random secret in your .env file (openssl rand -hex 32).",
                stacklevel=2,
            )
    session_max_age: int = 30 * 24 * 3600           # 30 days in seconds

    # Test mode — bypass auth for E2E tests. NEVER true in prod.
    test_mode: bool = False

    # Notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    class Config:
        env_file = ".env"

settings = Settings()

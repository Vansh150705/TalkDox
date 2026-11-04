"""Central configuration. Everything is read from environment variables once, so
deployment never requires code changes. `Settings` is a plain dataclass so tests
can construct variants without touching process-wide state."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field, fields
from pathlib import Path

log = logging.getLogger("talkdox.config")


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        log.warning("Invalid integer for %s; using %s", name, default)
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        log.warning("Invalid number for %s; using %s", name, default)
        return default


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    # --- runtime ---------------------------------------------------------
    env: str = "development"  # development | production | test
    data_dir: Path = Path("./data")
    secret_key: str = ""
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173"])
    trust_proxy_headers: bool = False  # honour X-Forwarded-For (only behind a trusted proxy)
    trusted_proxy_hops: int = 1  # proxies in front of the app that append to X-Forwarded-For

    # --- model providers -------------------------------------------------
    llm_provider: str = "auto"  # auto | gemini | groq | fake
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_fallback_model: str = ""  # explicit, user-visible fallback; never switched silently
    gemini_thinking_budget: int = -1  # -1 = model default, 0 = off
    gemini_base_url: str = ""  # optional gateway/proxy (also used by protocol tests)
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    embedding_provider: str = "auto"  # auto | gemini | hash
    embedding_model: str = "gemini-embedding-001"
    embedding_dim: int = 768
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3
    llm_max_output_tokens: int = 4096

    # --- ingestion limits ------------------------------------------------
    max_upload_mb: int = 25
    max_pdf_pages: int = 1000
    max_uncompressed_mb: int = 250  # zip-bomb guard for docx/pptx/xlsx
    max_zip_entries: int = 5000
    max_sources_per_workspace: int = 40
    max_chunks_per_source: int = 20000
    max_sheet_rows: int = 200000
    max_text_chars: int = 8_000_000
    ingest_workers: int = 2
    ingest_timeout_s: int = 1800
    ocr_engine: str = "auto"  # auto | gemini | tesseract | none
    max_ocr_pages: int = 60

    # --- web -------------------------------------------------------------
    respect_robots: bool = True
    user_agent: str = "TalkDoxBot/1.0 (+https://github.com/Vansh150705/TalkDox)"
    fetch_timeout_s: float = 15.0
    fetch_max_bytes: int = 8 * 1024 * 1024
    fetch_max_redirects: int = 5
    allowed_ports: list[int] = field(default_factory=lambda: [80, 443])
    allow_private_network: bool = False  # TEST ONLY. Disables the SSRF guard.
    outbound_proxy: str = ""
    crawl_max_pages: int = 50
    crawl_max_depth: int = 2
    crawl_delay_s: float = 0.5
    crawl_timeout_s: int = 300
    enable_js_render: bool = False
    js_render_timeout_s: float = 20.0

    # --- youtube ---------------------------------------------------------
    supadata_api_key: str = ""
    youtube_api_key: str = ""
    youtube_proxy_url: str = ""
    enable_ytdlp_fallback: bool = True
    enable_gemini_video_fallback: bool = False
    youtube_max_playlist_videos: int = 25

    # --- retrieval / answering ------------------------------------------
    chunk_target_chars: int = 1100
    chunk_max_chars: int = 1800
    chunk_overlap_chars: int = 160
    retrieval_top_k: int = 8
    retrieval_candidates: int = 40
    min_relevance: float = 0.15
    multi_query: bool = True
    context_char_budget: int = 24000

    # --- abuse / privacy ------------------------------------------------
    rate_chat_per_min: int = 20
    rate_ingest_per_hour: int = 30
    rate_workspace_create_per_hour: int = 20
    rate_tools_per_hour: int = 40
    max_question_chars: int = 4000
    retention_days: int = 30  # 0 disables automatic purge

    @property
    def is_prod(self) -> bool:
        return self.env == "production"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "talkdox.sqlite3"

    @property
    def resolved_llm_provider(self) -> str:
        if self.llm_provider != "auto":
            return self.llm_provider
        if self.gemini_api_key:
            return "gemini"
        if self.groq_api_key:
            return "groq"
        return "none"

    @property
    def resolved_embedding_provider(self) -> str:
        if self.embedding_provider != "auto":
            return self.embedding_provider
        return "gemini" if self.gemini_api_key else "none"

    def public_dict(self) -> dict:
        """Settings safe to expose to the frontend (no secrets)."""
        return {
            "max_upload_mb": self.max_upload_mb,
            "max_pdf_pages": self.max_pdf_pages,
            "max_sources_per_workspace": self.max_sources_per_workspace,
            "crawl_max_pages": self.crawl_max_pages,
            "crawl_max_depth": self.crawl_max_depth,
            "js_render": self.enable_js_render,
            "ocr_engine": self.ocr_engine,
            "retention_days": self.retention_days,
            "respect_robots": self.respect_robots,
        }


def load_settings() -> Settings:
    d = Settings()
    s = Settings(
        env=os.getenv("TALKDOX_ENV", d.env),
        data_dir=Path(os.getenv("DATA_DIR", str(d.data_dir))),
        secret_key=os.getenv("SECRET_KEY", ""),
        cors_origins=_list("CORS_ORIGINS", ",".join(d.cors_origins)),
        trust_proxy_headers=_bool("TRUST_PROXY_HEADERS", d.trust_proxy_headers),
        trusted_proxy_hops=max(1, _int("TRUSTED_PROXY_HOPS", d.trusted_proxy_hops)),
        llm_provider=os.getenv("LLM_PROVIDER", d.llm_provider).lower(),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", d.gemini_model),
        gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", ""),
        gemini_thinking_budget=_int("GEMINI_THINKING_BUDGET", d.gemini_thinking_budget),
        gemini_base_url=os.getenv("GEMINI_BASE_URL", ""),
        groq_api_key=os.getenv("GROQ_API_KEY", ""),
        groq_model=os.getenv("GROQ_MODEL", d.groq_model),
        embedding_provider=os.getenv("EMBEDDING_PROVIDER", d.embedding_provider).lower(),
        embedding_model=os.getenv("EMBEDDING_MODEL", d.embedding_model),
        embedding_dim=_int("EMBEDDING_DIM", d.embedding_dim),
        llm_timeout_s=_float("LLM_TIMEOUT_S", d.llm_timeout_s),
        llm_max_retries=_int("LLM_MAX_RETRIES", d.llm_max_retries),
        llm_max_output_tokens=_int("LLM_MAX_OUTPUT_TOKENS", d.llm_max_output_tokens),
        max_upload_mb=_int("MAX_UPLOAD_MB", d.max_upload_mb),
        max_pdf_pages=_int("MAX_PDF_PAGES", d.max_pdf_pages),
        max_uncompressed_mb=_int("MAX_UNCOMPRESSED_MB", d.max_uncompressed_mb),
        max_zip_entries=_int("MAX_ZIP_ENTRIES", d.max_zip_entries),
        max_sources_per_workspace=_int("MAX_SOURCES_PER_WORKSPACE", d.max_sources_per_workspace),
        max_chunks_per_source=_int("MAX_CHUNKS_PER_SOURCE", d.max_chunks_per_source),
        max_sheet_rows=_int("MAX_SHEET_ROWS", d.max_sheet_rows),
        max_text_chars=_int("MAX_TEXT_CHARS", d.max_text_chars),
        ingest_workers=_int("INGEST_WORKERS", d.ingest_workers),
        ingest_timeout_s=_int("INGEST_TIMEOUT_S", d.ingest_timeout_s),
        ocr_engine=os.getenv("OCR_ENGINE", d.ocr_engine).lower(),
        max_ocr_pages=_int("MAX_OCR_PAGES", d.max_ocr_pages),
        respect_robots=_bool("RESPECT_ROBOTS", d.respect_robots),
        user_agent=os.getenv("CRAWLER_USER_AGENT", d.user_agent),
        fetch_timeout_s=_float("FETCH_TIMEOUT_S", d.fetch_timeout_s),
        fetch_max_bytes=_int("FETCH_MAX_BYTES", d.fetch_max_bytes),
        fetch_max_redirects=_int("FETCH_MAX_REDIRECTS", d.fetch_max_redirects),
        allowed_ports=[int(p) for p in _list("ALLOWED_PORTS", "80,443")],
        allow_private_network=_bool("ALLOW_PRIVATE_NETWORK", False),
        outbound_proxy=os.getenv("OUTBOUND_PROXY", ""),
        crawl_max_pages=_int("CRAWL_MAX_PAGES", d.crawl_max_pages),
        crawl_max_depth=_int("CRAWL_MAX_DEPTH", d.crawl_max_depth),
        crawl_delay_s=_float("CRAWL_DELAY_S", d.crawl_delay_s),
        crawl_timeout_s=_int("CRAWL_TIMEOUT_S", d.crawl_timeout_s),
        enable_js_render=_bool("ENABLE_JS_RENDER", d.enable_js_render),
        js_render_timeout_s=_float("JS_RENDER_TIMEOUT_S", d.js_render_timeout_s),
        supadata_api_key=os.getenv("SUPADATA_API_KEY", ""),
        youtube_api_key=os.getenv("YOUTUBE_API_KEY", ""),
        youtube_proxy_url=os.getenv("YOUTUBE_PROXY_URL", ""),
        enable_ytdlp_fallback=_bool("ENABLE_YTDLP_FALLBACK", d.enable_ytdlp_fallback),
        enable_gemini_video_fallback=_bool("ENABLE_GEMINI_VIDEO_FALLBACK", d.enable_gemini_video_fallback),
        youtube_max_playlist_videos=_int("YOUTUBE_MAX_PLAYLIST_VIDEOS", d.youtube_max_playlist_videos),
        chunk_target_chars=_int("CHUNK_TARGET_CHARS", d.chunk_target_chars),
        chunk_max_chars=_int("CHUNK_MAX_CHARS", d.chunk_max_chars),
        chunk_overlap_chars=_int("CHUNK_OVERLAP_CHARS", d.chunk_overlap_chars),
        retrieval_top_k=_int("RETRIEVAL_TOP_K", d.retrieval_top_k),
        retrieval_candidates=_int("RETRIEVAL_CANDIDATES", d.retrieval_candidates),
        min_relevance=_float("MIN_RELEVANCE", d.min_relevance),
        multi_query=_bool("MULTI_QUERY", d.multi_query),
        context_char_budget=_int("CONTEXT_CHAR_BUDGET", d.context_char_budget),
        rate_chat_per_min=_int("RATE_CHAT_PER_MIN", d.rate_chat_per_min),
        rate_ingest_per_hour=_int("RATE_INGEST_PER_HOUR", d.rate_ingest_per_hour),
        rate_workspace_create_per_hour=_int("RATE_WORKSPACE_CREATE_PER_HOUR", d.rate_workspace_create_per_hour),
        rate_tools_per_hour=_int("RATE_TOOLS_PER_HOUR", d.rate_tools_per_hour),
        max_question_chars=_int("MAX_QUESTION_CHARS", d.max_question_chars),
        retention_days=_int("RETENTION_DAYS", d.retention_days),
    )
    return finalize(s)


def finalize(s: Settings) -> Settings:
    """Create the data dir, resolve the signing secret and validate config."""
    s.data_dir.mkdir(parents=True, exist_ok=True)
    if not s.secret_key:
        key_file = s.data_dir / ".secret_key"
        if key_file.exists():
            s.secret_key = key_file.read_text().strip()
        else:
            s.secret_key = secrets.token_urlsafe(48)
            key_file.write_text(s.secret_key)
            try:
                key_file.chmod(0o600)
            except OSError:
                pass
        if s.is_prod:
            log.warning("SECRET_KEY not set; generated one in %s. Set SECRET_KEY explicitly "
                        "when running more than one instance.", key_file)
    if s.is_prod:
        if "*" in s.cors_origins:
            raise RuntimeError("CORS_ORIGINS must not contain '*' in production")
        if s.allow_private_network:
            raise RuntimeError("ALLOW_PRIVATE_NETWORK is for tests only and is refused in production")
    if s.embedding_dim not in (128, 256, 512, 768, 1024, 1536, 2048, 3072):
        log.warning("Unusual EMBEDDING_DIM=%s", s.embedding_dim)
    return s


def config_problems(s: Settings) -> list[str]:
    """Human readable list of things that will prevent features from working."""
    out = []
    if s.resolved_llm_provider == "none":
        out.append("No LLM configured: set GEMINI_API_KEY (or GROQ_API_KEY). Chat and AI tools are disabled.")
    if s.resolved_embedding_provider == "none":
        out.append("No embedding provider configured: set GEMINI_API_KEY. Ingestion is disabled.")
    return out


__all__ = ["Settings", "load_settings", "finalize", "config_problems", "fields"]

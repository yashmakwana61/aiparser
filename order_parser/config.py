from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "order_parser"
    app_env: str = Field(default="development")
    log_level: str = Field(default="INFO")
    log_dir: str = Field(default="logs")
    # Persistent rotating JSONL application logs (Phase 22). Off by default;
    # console-only logging preserves legacy behavior.
    log_to_file: bool = Field(default=False)
    log_file_max_mb: int = Field(default=50)
    log_file_backup_count: int = Field(default=5)
    # Disk-space readiness guard on the volume holding log_dir. The storage
    # component reports "warning" below the warning threshold and "error"
    # (flipping /ready to 503) below the error threshold.
    disk_check_enabled: bool = Field(default=True)
    disk_min_free_warning_gb: float = Field(default=5.0)
    disk_min_free_error_gb: float = Field(default=1.0)

    # Puter AI gateway (primary text-extraction path). Order text is
    # normalized via the ChatGPT text model (ai_text_model); Google Vision
    # remains the OCR provider for images/scanned PDFs. Requires
    # PUTER_AUTH_TOKEN from https://puter.com/dashboard.
    puter_auth_token: str = Field(default="")
    puter_base_url: str = Field(default="https://api.puter.com/puterai/openai/v1/")
    ai_text_model: str = Field(default="gpt-4.1")
    ai_vision_model: str = Field(default="gpt-4o")

    # AI-assisted matching (Phase 44): LLM tiebreaker for ambiguous lines.
    # Off by default; when enabled the model only SELECTS from bounded
    # candidate sets (never invents), batched to one call per order.
    ai_match_enabled: bool = Field(default=False)
    ai_match_model: str = Field(default="gpt-4.1-mini")
    ai_match_attempts: int = Field(default=2)
    ai_match_max_candidates: int = Field(default=8)
    # AI picks at/above this confidence resolve deterministically (gated
    # into DETERMINISTIC_METHODS via AI_MATCH); below it they only
    # pre-rank the pick buttons.
    ai_match_auto_threshold: float = Field(default=95.0)

    # AI gateway resilience (Phase 12). Gateway calls get a configurable
    # socket timeout and transient failures (network errors, retryable HTTP
    # statuses, malformed bodies) are retried with linear backoff. Parsers
    # additionally re-prompt when the model returns unparseable JSON — a
    # common LLM flake that usually resolves on a second attempt.
    ai_timeout_seconds: float = Field(default=120.0)
    ai_max_attempts: int = Field(default=3)
    ai_retry_backoff_seconds: float = Field(default=2.0)

    odoo_url: str = Field(default="http://localhost:8069")
    odoo_db: str = Field(default="")
    odoo_user: str = Field(default="")
    odoo_password: str = Field(default="")

    # Odoo resilience (Phase 11). XML-RPC calls get an explicit socket
    # timeout (the stdlib default is infinite) and transient network
    # failures are retried with linear backoff. xmlrpc.client.Fault is an
    # application-level answer from Odoo and is never retried.
    odoo_timeout_seconds: float = Field(default=30.0)
    odoo_max_attempts: int = Field(default=3)
    odoo_retry_backoff_seconds: float = Field(default=1.0)

    # Data retention (Phase 13). When enabled, a background sweeper deletes
    # terminal-status sessions (COMPLETED/FAILED/CANCELLED/EXPIRED) older
    # than the retention window together with their saved attachments, and
    # purges old rows from the idempotency history. Active sessions are
    # never deleted. Disabled by default so upgrades never delete data
    # unannounced.
    enable_retention_sweeper: bool = Field(default=False)
    session_retention_days: int = Field(default=30)
    retention_sweep_interval_seconds: int = Field(default=3600)
    # 0 keeps the audit archive forever; N deletes whole-day files older than
    # N days (safe: audit hash chains are daily-independent).
    audit_retention_days: int = Field(default=0)

    # Circuit breakers (Phase 15). When enabled, a dependency whose calls
    # keep failing exhaustively is marked "open" and further requests fail
    # fast without network attempts until the recovery window elapses, then
    # a single probe decides whether to close it again. Disabled by default
    # so failure modes stay unchanged unless opted in.
    enable_circuit_breakers: bool = Field(default=False)
    breaker_failure_threshold: int = Field(default=5)
    breaker_recovery_seconds: float = Field(default=60.0)

    telegram_bot_token: str = Field(default="")
    telegram_webhook_url: str = Field(default="")
    telegram_webhook_secret: str = Field(default="order-parser-secret")

    email_imap_host: str = Field(default="")
    email_imap_port: int = Field(default=993)
    email_username: str = Field(default="")
    email_password: str = Field(default="")
    email_poll_interval: int = Field(default=60)

    # Email ingestion hardening (Phase 9). Attachments over the size limit or
    # of unknown types are skipped with explicit error results instead of
    # being routed to processors. Already-ingested emails are recorded in a
    # local SQLite store keyed by Message-ID (or content hash) so re-delivery
    # never double-processes, even across restarts. IMAP failures back off
    # exponentially up to the cap.
    email_imap_timeout_seconds: float = Field(default=30.0)
    email_max_attachment_mb: int = Field(default=10)
    email_max_raw_mb: int = Field(default=25)
    email_state_db_path: str = Field(default="")
    email_seen_window_days: int = Field(default=7)
    email_max_backoff_seconds: int = Field(default=600)

    # Confidence decision engine thresholds (0-100)
    auto_create_threshold: float = Field(default=95.0)
    confirm_threshold: float = Field(default=80.0)

    # Product auto-creation: when an item is not found in the Odoo catalog,
    # create it (with its parsed price, or 0) so the order can proceed.
    auto_create_products: bool = Field(default=True)

    # Customer auto-creation: when a customer cannot be matched in Odoo,
    # create a new partner so the order can proceed instead of routing
    # to manual review. Requires auto_create_all_orders to also be true.
    auto_create_customers: bool = Field(default=False)

    # Force every valid order (text/image/pdf/excel) to be auto-created as a
    # Sales Order immediately, skipping the confirmation/manual-review flow.
    auto_create_all_orders: bool = Field(default=False)

    # Master data resolution layer (Phase 2). Odoo is authoritative; these
    # knobs tune deterministic matching and safety gating.
    resolution_fuzzy_cutoff: float = Field(default=72.0)
    resolution_ambiguity_gap: float = Field(default=2.0)
    resolution_fuzzy_confidence_cap: float = Field(default=89.0)
    price_mandatory: bool = Field(default=True)
    approved_price_fallback: bool = Field(default=False)
    price_deviation_tolerance_pct: float = Field(default=25.0)
    trust_explicit_taxes: bool = Field(default=False)
    duplicate_window_hours: int = Field(default=24)

    # Permissive resolution (Phase 18). When False (default), missing UOM,
    # price and tax are non-blocking warnings — the parser produces a
    # READY_FOR_ODOO payload and lets Odoo apply its own defaults. When
    # True, they revert to legacy blocking behaviour (orders with missing
    # UOM/price/tax are routed to review).
    strict_resolution: bool = Field(default=False)

    # Known order collectors / vendors that must NEVER resolve as the
    # customer (comma-separated, case-insensitive contains-match). E.g. a
    # distributor that forwards purchase orders issued by the real buyer.
    # When the extracted customer matches, resolution reroutes to the
    # deliver-to party when present, otherwise the order is routed to
    # review with reason "collector_as_customer".
    never_customer_names: str = Field(default="")

    # Staff order sessions (Phase 3). When enabled, staff collect multiple
    # Telegram messages/attachments into one session before a single order is
    # processed. AUTHORIZED_STAFF format: "12345:bob_ops,67890:Alice Sales".
    # Empty list disables enforcement (development only).
    enable_order_sessions: bool = Field(default=False)
    order_session_timeout_minutes: int = Field(default=60)
    authorized_staff: str = Field(default="")
    session_store_dir: str = Field(default="")

    # Google Vision OCR (only OCR provider; GPT-vision direct fallback removed).
    # OCR extracts raw text only; the semantic layer (TextParser via the
    # Puter ChatGPT text model) performs order interpretation. OCR is mandatory for images and scanned PDFs:
    # when unconfigured or failed, processors return a flagged ocr_failed
    # order for review - never auto-created. Enabled by default when an API
    # key is present.
    enable_google_vision: bool = Field(default=True)
    google_vision_api_key: str = Field(default="")
    google_vision_base_url: str = Field(default="https://vision.googleapis.com")
    google_vision_timeout_seconds: float = Field(default=30.0)
    google_vision_max_retries: int = Field(default=3)
    google_vision_backoff_seconds: float = Field(default=1.0)
    max_upload_mb: int = Field(default=10)
    upload_dir: str = Field(default="")
    pdf_min_chars_per_page: int = Field(default=15)

    # Persistent idempotency (Phase 6). When enabled, every successfully
    # ingested order fingerprint is recorded in a local SQLite database so
    # duplicates are caught across restarts - including orders that were
    # auto-created (which pending-record scanning cannot see). Creation is
    # additionally guarded by an atomic claim so retries/races cannot double-
    # create. Recommended: true in production.
    enable_idempotency: bool = Field(default=False)
    idempotency_db_path: str = Field(default="")

    # API security (Phase 7). When API_AUTH_TOKEN is set, every management /
    # ingestion endpoint (orders, aliases, email, telegram webhook setup)
    # requires "Authorization: Bearer <token>" or "X-API-Key: <token>".
    # Empty disables enforcement (development only). The Telegram delivery
    # webhook keeps its own dedicated secret regardless.
    api_auth_token: str = Field(default="")
    api_rate_limit_per_minute: int = Field(default=0)
    api_cors_origins: str = Field(default="")

    # Async job queue (Phase 33): lightweight asyncio workers, no external infra
    worker_count: int = Field(default=3)
    queue_max_size: int = Field(default=500)
    job_store_dir: str = Field(default="")
    job_timeout_seconds: float = Field(default=300.0)
    max_pages: int = Field(default=20)
    max_excel_rows: int = Field(default=5000)

    # Job-level transient retry (production hardening). run_job_sync retries
    # TRANSIENT-classified failures (AI/OCR/Odoo timeouts, SYS-001) with
    # exponential backoff: backoff * 2^attempt, capped. PERMANENT and
    # REVIEW_REQUIRED errors are never retried. Inner clients (AI/Odoo/Vision)
    # keep their own attempt budgets; this is the outer job guard.
    job_max_retries: int = Field(default=3)
    job_retry_backoff_seconds: float = Field(default=1.0)
    job_retry_max_backoff_seconds: float = Field(default=30.0)

    # Confidence thresholds (configurable, documented heuristic: AI 0-100)
    confidence_high_threshold: float = Field(default=95.0)
    confidence_medium_threshold: float = Field(default=80.0)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        env_prefix="",
    )

    @property
    def ai_base_url(self) -> str:
        """Puter gateway URL, falling back to the official default."""
        return self.puter_base_url or "https://api.puter.com/puterai/openai/v1/"

    @property
    def ai_drivers_url(self) -> str:
        """Puter driver interface URL (free tier), derived from the gateway host."""
        base = self.ai_base_url.rstrip("/")
        if "/puterai/" in base:
            base = base.split("/puterai/")[0]
        return f"{base}/drivers/call"


@lru_cache
def get_settings() -> Settings:
    return Settings()

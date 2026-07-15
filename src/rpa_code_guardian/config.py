"""Typed configuration loaded from environment / .env file."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Agent configuration.

    Values come from environment variables or a local ``.env`` file. CLI flags
    can override individual fields at runtime (see ``cli.py``).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM endpoint (OpenAI-compatible) ---
    openai_base_url: str = Field(
        default="http://localhost:8000/v1",
        description="Base URL of the OpenAI-compatible endpoint (must end in /v1).",
    )
    openai_api_key: str = Field(
        default="not-needed",
        description="API key; any non-empty string if the endpoint ignores it.",
    )
    worker_model: str = Field(
        default="",
        alias="GUARDIAN_WORKER_MODEL",
        description="Model for the per-workflow map phase (many small calls).",
    )
    lead_model: str = Field(
        default="",
        alias="GUARDIAN_LEAD_MODEL",
        description="Model for planning, narrative and compliance (fewer, harder calls).",
    )
    temperature: float = Field(
        default=0.0,
        alias="GUARDIAN_TEMPERATURE",
        description="Sampling temperature (keep 0 for reproducible documents).",
    )
    verify_ssl: bool = Field(
        default=True,
        alias="GUARDIAN_VERIFY_SSL",
        description="Verify the endpoint's TLS certificate. Set false only for a "
        "trusted local/corporate endpoint with a self-signed certificate.",
    )
    request_timeout: float = Field(
        default=120.0,
        alias="GUARDIAN_REQUEST_TIMEOUT",
        description="HTTP timeout in seconds for each LLM call (local models can be slow).",
    )
    max_tokens: int = Field(
        default=8192,
        alias="GUARDIAN_MAX_TOKENS",
        description="Max completion tokens per call. Reasoning models (e.g. gpt-oss) "
        "spend tokens thinking before answering; too low a budget truncates the JSON "
        "output and validation fails. Raise it if summaries still come back truncated.",
    )
    llm_retries: int = Field(
        default=3,
        alias="GUARDIAN_LLM_RETRIES",
        description="Retries per LLM call on transient endpoint errors "
        "(429 rate limit, 502/503/504 gateway errors, timeouts).",
    )
    llm_retry_base_delay: float = Field(
        default=2.0,
        alias="GUARDIAN_LLM_RETRY_BASE_DELAY",
        description="Initial backoff delay in seconds; doubles per retry (capped at 60s).",
    )

    # --- Pipeline limits ---
    max_concurrency: int = Field(
        default=4,
        alias="GUARDIAN_MAX_CONCURRENCY",
        description="Max parallel LLM calls during the map phase.",
    )
    agent_max_iterations: int = Field(
        default=8,
        alias="GUARDIAN_AGENT_MAX_ITERATIONS",
        description="Tool-loop cap for the gap-fill and compliance evidence agents.",
    )
    ir_max_chars: int = Field(
        default=12_000,
        alias="GUARDIAN_IR_MAX_CHARS",
        description="Character budget for one workflow IR handed to the worker model.",
    )
    use_cache: bool = Field(
        default=True,
        alias="GUARDIAN_USE_CACHE",
        description="Reuse cached per-workflow summaries when the file has not changed.",
    )

    # --- Agentic verification passes ---
    audit_narrative: bool = Field(
        default=True,
        alias="GUARDIAN_AUDIT_NARRATIVE",
        description="Run a critic pass over the narrative: unsupported claims are "
        "turned into open questions for the gap-fill agent (one extra lead call).",
    )
    verify_smells: bool = Field(
        default=True,
        alias="GUARDIAN_VERIFY_SMELLS",
        description="Adversarially verify model-reported code smells with the project "
        "tools before they become findings; unconfirmed smells are dropped.",
    )
    escalate_weak_summaries: bool = Field(
        default=True,
        alias="GUARDIAN_ESCALATE_WEAK_SUMMARIES",
        description="Retry a workflow summary with the lead model when the worker's "
        "output fails a cheap quality gate (empty key logic, generic purpose).",
    )

    def resolved_worker_model(self) -> str:
        return self.worker_model or self.lead_model or "gpt-oss"

    def resolved_lead_model(self) -> str:
        return self.lead_model or self.worker_model or "gpt-oss"


def load_settings(**overrides: object) -> Settings:
    """Load settings, applying any non-None CLI overrides on top of env/.env."""
    clean = {k: v for k, v in overrides.items() if v is not None}
    return Settings(**clean)


def cache_dir(project_root: Path) -> Path:
    """Per-project cache directory (summaries, checkpoints, run metadata)."""
    return project_root / ".guardian_cache"

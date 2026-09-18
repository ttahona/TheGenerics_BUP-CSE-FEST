"""Runtime configuration loaded from environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", "")).strip()
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-20241022").strip()
    llm_provider: str = os.getenv("LLM_PROVIDER", "auto").strip().lower()
    host: str = os.getenv("HOST", "0.0.0.0").strip()
    port: int = int(os.getenv("PORT", "8000").strip() or "8000")
    # Internal tunables
    llm_response_timeout_seconds: float = float(os.getenv("LLM_RESPONSE_TIMEOUT_SECONDS", "12").strip() or "12")
    request_timeout_seconds: float = float(os.getenv("REQUEST_TIMEOUT_SECONDS", "28").strip() or "28")
    enable_llm_cache: bool = _env_bool("ENABLE_LLM_CACHE", True)

    def active_llm_provider(self) -> str | None:
        if self.llm_provider == "off":
            return None
        if self.llm_provider in ("openai", "auto") and self.openai_api_key:
            return "openai"
        if self.llm_provider in ("gemini", "google", "auto") and self.gemini_api_key:
            return "gemini"
        if self.llm_provider in ("anthropic", "auto") and self.anthropic_api_key:
            return "anthropic"
        return None

    def using_llm(self) -> bool:
        return self.active_llm_provider() is not None


settings = Settings()


def active_llm_provider() -> str | None:
    """Detect which LLM provider is active."""
    if settings.llm_provider == "off":
        return None
    if settings.llm_provider in ("openai", "auto") and settings.openai_api_key:
        return "openai"
    if settings.llm_provider in ("gemini", "google", "auto") and settings.gemini_api_key:
        return "gemini"
    if settings.llm_provider in ("anthropic", "auto") and settings.anthropic_api_key:
        return "anthropic"
    return None


def using_llm() -> bool:
    """Return True when an LLM provider is configured and selected."""
    return active_llm_provider() is not None


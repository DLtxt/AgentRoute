"""Configuration, sourced entirely from the environment.

Portability contract rules 1-3: no hardcoded hostnames, all config via
environment, secrets only via env vars. Nothing here reads a file from a host
path or assumes a working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    redis_url: str
    ollama_url: str
    mock_tiers: bool
    cache_ttl_seconds: int
    cache_schema_version: str
    classifier: str
    log_level: str
    anthropic_api_key: str | None
    ollama_model: str
    ollama_timeout_seconds: float
    max_tokens: int
    sonnet_thinking: str
    gateway_api_keys: str
    rate_limit_per_minute: int

    @property
    def mode(self) -> str:
        """Cache namespace. Keeps mock-mode responses out of the live keyspace."""
        return "mock" if self.mock_tiers else "live"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        mock_tiers=_bool("MOCK_TIERS", True),
        cache_ttl_seconds=_int("CACHE_TTL_SECONDS", 3600),
        cache_schema_version=os.getenv("CACHE_SCHEMA_VERSION", "v1"),
        classifier=os.getenv("CLASSIFIER", "rules"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        ollama_model=os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
        ollama_timeout_seconds=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "120")),
        # A cap, not a target: you are billed for what is generated, not for
        # this number. Bounded because a router wants bounded answers.
        max_tokens=_int("MAX_TOKENS", 1024),
        # "adaptive" (the model default) or "disabled". See AnthropicTier.
        sonnet_thinking=os.getenv("SONNET_THINKING", "adaptive"),
        # Comma-separated. Empty disables auth entirely (local dev only).
        gateway_api_keys=os.getenv("GATEWAY_API_KEYS", ""),
        rate_limit_per_minute=_int("RATE_LIMIT_PER_MINUTE", 120),
    )

"""Settings loaded from environment variables.

Everything has a default that works offline, so the demo and the tests need
no configuration at all. Production must set APP_ENV=production and at least
one API key, otherwise startup fails on purpose.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

DEV_KEY_NAME = "dev"
DEV_KEY_VALUE = "dev-local-key"
DEFAULT_DATABASE_URL = "sqlite:///./data/ops.db"


class ConfigError(ValueError):
    """Raised when the environment holds an unusable configuration."""


def hash_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest used to store and compare API keys."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Settings:
    env: str = "dev"
    database_url: str = DEFAULT_DATABASE_URL
    # Maps key name to the SHA-256 hex digest of the key. Raw keys are never kept.
    api_keys: Mapping[str, str] = field(default_factory=dict)
    using_dev_key: bool = False
    log_level: str = "INFO"
    log_format: str = "json"
    worker_enabled: bool = True
    worker_poll_seconds: float = 1.0
    job_max_attempts: int = 4
    job_backoff_seconds: float = 5.0
    job_visibility_timeout_seconds: float = 300.0
    notify_webhook_url: str | None = None
    notify_webhook_token: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def is_production(self) -> bool:
        return self.env == "production"


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def parse_api_keys(raw: str | None) -> dict[str, str]:
    """Parse OPS_API_KEYS.

    Format: comma separated entries, each either

    * ``name:the-raw-key`` (hashed at load time), or
    * ``name:sha256:<64 hex chars>`` (already hashed, so the raw key never
      has to sit in the environment of the server).
    """
    keys: dict[str, str] = {}
    if not raw:
        return keys
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, rest = entry.partition(":")
        name = name.strip()
        if not sep or not name or not rest:
            raise ConfigError("OPS_API_KEYS entries must look like name:key")
        if name in keys:
            raise ConfigError(f"OPS_API_KEYS has a duplicate key name: {name}")
        if rest.startswith("sha256:"):
            digest = rest[len("sha256:"):].strip().lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ConfigError(f"OPS_API_KEYS entry {name} has a malformed sha256 digest")
            keys[name] = digest
        else:
            if len(rest) < 12:
                raise ConfigError(f"OPS_API_KEYS entry {name} is too short (12 characters minimum)")
            keys[name] = hash_key(rest)
    return keys


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from a mapping (defaults to os.environ)."""
    env = os.environ if env is None else env
    app_env = (env.get("APP_ENV") or "dev").strip().lower()
    api_keys = parse_api_keys(env.get("OPS_API_KEYS"))
    using_dev_key = False
    if not api_keys:
        if app_env == "production":
            raise ConfigError("OPS_API_KEYS must be set when APP_ENV=production")
        api_keys = {DEV_KEY_NAME: hash_key(DEV_KEY_VALUE)}
        using_dev_key = True

    log_format = (env.get("LOG_FORMAT") or "json").strip().lower()
    if log_format not in {"json", "text"}:
        raise ConfigError("LOG_FORMAT must be json or text")

    return Settings(
        env=app_env,
        database_url=(env.get("DATABASE_URL") or DEFAULT_DATABASE_URL).strip(),
        api_keys=api_keys,
        using_dev_key=using_dev_key,
        log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
        log_format=log_format,
        worker_enabled=_as_bool(env.get("WORKER_ENABLED"), True),
        worker_poll_seconds=_as_float(env, "WORKER_POLL_SECONDS", 1.0),
        job_max_attempts=int(_as_float(env, "JOB_MAX_ATTEMPTS", 4)),
        job_backoff_seconds=_as_float(env, "JOB_BACKOFF_SECONDS", 5.0),
        job_visibility_timeout_seconds=_as_float(env, "JOB_VISIBILITY_TIMEOUT_SECONDS", 300.0),
        notify_webhook_url=(env.get("NOTIFY_WEBHOOK_URL") or "").strip() or None,
        notify_webhook_token=(env.get("NOTIFY_WEBHOOK_TOKEN") or "").strip() or None,
        host=(env.get("HOST") or "127.0.0.1").strip(),
        port=int(_as_float(env, "PORT", 8000)),
    )


def load_dotenv(path: str | Path = ".env") -> list[str]:
    """Tiny .env reader for the command line entry points.

    Values already present in the process environment win. Returns the names
    that were set, which is handy for a startup log line.
    """
    file = Path(path)
    if not file.is_file():
        return []
    loaded: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if name and name not in os.environ:
            os.environ[name] = value
            loaded.append(name)
    return loaded

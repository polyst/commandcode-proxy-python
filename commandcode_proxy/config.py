"""Runtime configuration.

Precedence, highest first:
  1. explicit keyword override (used by the CLI parser)
  2. environment variable  (CC_PROXY_HOST, CC_PROXY_PORT, ...)
  3. built-in default

A .env file next to the project root is loaded into os.environ before anything
else reads it, so it sits below both CLI flags and already-exported variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_PREFIX = "CC_PROXY_"

TRUE_VALUES = {"1", "true", "yes", "on"}


def load_dotenv(path: str | os.PathLike | None = None) -> None:
    """Populate os.environ from a .env file without overriding existing values."""
    candidates = [Path(path)] if path else [
        Path(os.getcwd()) / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = _env_name(key)
            value = value.strip().strip('"').strip("'")
            if key and os.environ.get(key, None) is None:
                os.environ[key] = value
        return


def _env_name(raw_key: str) -> str:
    """A .env line may be bare (HOST=...) or already prefixed (CC_PROXY_HOST=...)."""
    raw_key = raw_key.strip()
    return raw_key if raw_key.startswith(ENV_PREFIX) else ENV_PREFIX + raw_key


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 55990
    api_key: str = ""
    base_url: str = "https://api.commandcode.ai"
    models_file: str = ""
    command_code_version: str = ""
    zdr: bool = False
    debug: bool = False

    def upstream_url(self) -> str:
        return self.base_url.rstrip("/") + "/alpha/generate"

    @property
    def models_path(self) -> Path:
        if self.models_file:
            return Path(self.models_file)
        return Path(__file__).resolve().parent.parent / "models.json"

    @classmethod
    def from_env(cls, **overrides: Any) -> "Config":
        load_dotenv()
        kwargs: dict[str, Any] = {
            "host": os.environ.get(ENV_PREFIX + "HOST", "127.0.0.1"),
            "port": _env_int("PORT", 55990),
            "api_key": os.environ.get(ENV_PREFIX + "API_KEY", ""),
            "base_url": os.environ.get(ENV_PREFIX + "BASE_URL", "https://api.commandcode.ai"),
            "models_file": os.environ.get(ENV_PREFIX + "MODELS_FILE", ""),
            "command_code_version": os.environ.get(ENV_PREFIX + "COMMAND_CODE_VERSION", ""),
            "zdr": _env_bool("ZDR", False),
            "debug": _env_bool("DEBUG", False),
        }
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**kwargs)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(ENV_PREFIX + name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default

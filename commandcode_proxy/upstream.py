"""CommandCode upstream side: request envelope, headers, and the version header.

Everything here is a straight port of internal/proxy/proxy.go (BuildRequest,
CreateUpstreamRequest) and internal/version/version.go. The CommandCode envelope
is fatter than the payload it carries — config/memory/taste/skills are
structural requirements of /alpha/generate, not client input.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import date
from typing import Any

import httpx

from .convert import convert_messages, convert_tools, extract_system

DEFAULT_BASE_URL = "https://api.commandcode.ai"
GENERATE_PATH = "/alpha/generate"
NPM_LATEST_URL = "https://registry.npmjs.org/command-code/latest"
VERSION_CACHE_TTL = 30 * 60
UNKNOWN_VERSION = "unknown"

DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 64000
# The upstream's hard validation cap on params.max_tokens, verified on three
# unrelated models: 200000 is accepted, 200001 rejects with BAD_REQUEST.
UPSTREAM_MAX_TOKENS = 200000
UPSTREAM_TIMEOUT = 300.0
VERSION_FETCH_TIMEOUT = 10.0

log = logging.getLogger("commandcode_proxy.upstream")


def normalize_finish_reason(reason: Any) -> str:
    value = reason if isinstance(reason, str) else ""
    if value in ("tool_calls", "tool-calls"):
        return "tool_calls"
    if value in ("length", "max_tokens"):
        return "length"
    if value in ("content_filter", "content-filter"):
        return "content_filter"
    return "stop"


def new_request_id() -> str:
    """Go emits "chatcmpl-" + the first 29 chars of a UUID string."""
    return "chatcmpl-" + str(uuid.uuid4())[:29]


def map_upstream_status(status_code: int) -> int:
    """4xx are forwarded as-is; anything 5xx becomes a 502 gateway error."""
    return status_code if 400 <= status_code < 500 else 502


def _number(value: Any, default: float, cast: type) -> float | int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return cast(value)
    return default


def _pick_temperature(request: dict[str, Any]) -> float:
    return _number(request.get("temperature"), DEFAULT_TEMPERATURE, float)


def _pick_max_tokens(request: dict[str, Any]) -> int:
    """max_completion_tokens wins over max_tokens, mirroring the Go precedence.

    Clamped to the upstream's hard validation cap. Clients derive max_tokens from
    the model's advertised output limit, and ZCode's own catalog advertises
    384000 for deepseek-v4-flash -- the upstream refuses anything above 200000,
    so the request 400s before it ever reaches the model.
    """
    requested = _number(
        request.get("max_completion_tokens", request.get("max_tokens")),
        DEFAULT_MAX_TOKENS,
        int,
    )
    if requested > UPSTREAM_MAX_TOKENS:
        log.warning("clamped max_tokens %d -> %d (upstream validation cap)",
                    requested, UPSTREAM_MAX_TOKENS)
        return UPSTREAM_MAX_TOKENS
    return requested


def build_envelope(request: dict[str, Any], model: str) -> dict[str, Any]:
    system, remaining = extract_system(request.get("messages") or [])
    return {
        "config": {
            "workingDir": ".",
            "date": date.today().isoformat(),
            "environment": "cli",
            "structure": [],
            "isGitRepo": False,
            "currentBranch": "",
            "mainBranch": "main",
            "gitStatus": "",
            "recentCommits": [],
        },
        "memory": "",
        "taste": "",
        "skills": "",
        "params": {
            "model": model,
            "messages": convert_messages(remaining),
            "tools": convert_tools(request.get("tools") or []),
            "system": system,
            "max_tokens": _pick_max_tokens(request),
            "temperature": _pick_temperature(request),
            # Always ask the upstream for a stream: a non-streaming client gets
            # the same stream read to the end and aggregated (see responses.py).
            "stream": True,
        },
        "threadId": str(uuid.uuid4()),
    }


def upstream_headers(api_key: str, command_code_version: str, zdr: bool = False) -> dict[str, str]:
    """The header set /alpha/generate expects.

    ``x-cmd-zdr`` is the wire form of the CLI's ``CMD_ZDR=1``: it asks the
    upstream for zero data retention and no prompt training on the request.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + api_key,
        "x-command-code-version": command_code_version,
        "x-cli-environment": "production",
        "Accept": "text/event-stream",
    }
    if zdr:
        headers["x-cmd-zdr"] = "1"
    return headers


class VersionCache:
    """Resolves the value for ``x-command-code-version``.

    The upstream expects the latest published ``command-code`` version. It is
    fetched from the npm registry and cached for 30 minutes; a pinned value
    (``CC_PROXY_COMMAND_CODE_VERSION``) always wins, which also makes the
    behaviour deterministic in tests. The lock never spans an await, so plain
    thread locking is enough and concurrent callers may issue duplicate fetches.
    """

    def __init__(self, pin: str = "") -> None:
        self._pin = pin
        self._version: str | None = None
        self._updated_at = 0.0
        self._lock = threading.Lock()

    async def get(self, client: httpx.AsyncClient) -> str:
        if self._pin:
            return self._pin
        if (cached := self._cached()) is not None:
            return cached

        try:
            response = await client.get(NPM_LATEST_URL, timeout=VERSION_FETCH_TIMEOUT)
            if response.status_code == 200:
                version = response.json().get("version")
                if isinstance(version, str) and version:
                    self.seed(version)
                    return version
            else:
                log.warning("npm registry returned %d for command-code version",
                            response.status_code)
        except Exception as exc:  # noqa: BLE001 - network failures must not break a chat call
            log.warning("failed to fetch command-code version: %s", exc)

        # A failure keeps whatever we had, even past the TTL.
        return self._stored() or UNKNOWN_VERSION

    def seed(self, version: str) -> None:
        with self._lock:
            self._version = version
            self._updated_at = time.monotonic()

    def _stored(self) -> str | None:
        with self._lock:
            return self._version

    def _cached(self) -> str | None:
        with self._lock:
            if self._version and time.monotonic() - self._updated_at < VERSION_CACHE_TTL:
                return self._version
            return None

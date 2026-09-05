"""Envelope construction, headers, and the x-command-code-version cache."""

import asyncio
import re
from datetime import date

import httpx
import pytest

from commandcode_proxy.upstream import (
    GENERATE_PATH,
    NPM_LATEST_URL,
    UNKNOWN_VERSION,
    VersionCache,
    build_envelope,
    map_upstream_status,
    new_request_id,
    upstream_headers,
)

BASE_MESSAGES = [
    {"role": "system", "content": "You are terse."},
    {"role": "user", "content": "hi"},
]


def test_envelope_shape_and_defaults():
    envelope = build_envelope({"model": "m", "messages": BASE_MESSAGES}, "Acme/M")

    assert list(envelope) == ["config", "memory", "taste", "skills", "params", "threadId"]
    assert envelope["memory"] == envelope["taste"] == envelope["skills"] == ""
    assert re.fullmatch(r"[0-9a-f-]{36}", envelope["threadId"])

    assert envelope["config"] == {
        "workingDir": ".",
        "date": date.today().isoformat(),
        "environment": "cli",
        "structure": [],
        "isGitRepo": False,
        "currentBranch": "",
        "mainBranch": "main",
        "gitStatus": "",
        "recentCommits": [],
    }

    params = envelope["params"]
    assert params["model"] == "Acme/M"
    assert params["system"] == "You are terse."
    assert params["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert params["tools"] == []
    assert params["temperature"] == 0.3
    assert params["max_tokens"] == 64000
    assert params["stream"] is True
    assert list(params) == ["model", "messages", "tools", "system", "max_tokens",
                            "temperature", "stream"]


def test_envelope_date_is_local_calendar_date():
    envelope = build_envelope({"messages": BASE_MESSAGES}, "m")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", envelope["config"]["date"])


def test_envelope_respects_client_overrides():
    envelope = build_envelope({
        "model": "m",
        "messages": BASE_MESSAGES,
        "temperature": 1.2,
        "max_tokens": 1000,
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}],
    }, "m")
    params = envelope["params"]
    assert params["temperature"] == 1.2
    assert params["max_tokens"] == 1000
    assert params["tools"] == [{"name": "f", "input_schema": {"type": "object"}}]


def test_max_completion_tokens_wins_over_max_tokens():
    envelope = build_envelope({
        "messages": BASE_MESSAGES,
        "max_tokens": 100,
        "max_completion_tokens": 999,
    }, "m")
    assert envelope["params"]["max_tokens"] == 999


def test_envelope_threads_tools_and_system_messages():
    envelope = build_envelope({
        "messages": [
            {"role": "system", "content": "A"},
            {"role": "system", "content": "B"},
            {"role": "user", "content": "hi"},
        ],
        "tools": [{"type": "function", "function": {"name": "f", "description": "d"}}],
    }, "m")
    params = envelope["params"]
    assert params["system"] == "A\nB"
    assert params["tools"] == [{"name": "f", "input_schema": {"type": "object", "properties": {}},
                                "description": "d"}]


def test_upstream_headers():
    assert upstream_headers("sk-abc", "0.1.2") == {
        "Content-Type": "application/json",
        "Authorization": "Bearer sk-abc",
        "x-command-code-version": "0.1.2",
        "x-cli-environment": "production",
        "Accept": "text/event-stream",
    }


def test_upstream_headers_zero_data_retention():
    assert upstream_headers("sk-abc", "0.1.2", zdr=True)["x-cmd-zdr"] == "1"
    assert "x-cmd-zdr" not in upstream_headers("sk-abc", "0.1.2", zdr=False)


def test_upstream_path_is_alpha_generate():
    assert GENERATE_PATH == "/alpha/generate"


@pytest.mark.parametrize(("status", "expected"), [
    (200, 502),
    (400, 400),
    (401, 401),
    (429, 429),
    (499, 499),
    (500, 502),
    (502, 502),
    (503, 502),
])
def test_map_upstream_status(status, expected):
    assert map_upstream_status(status) == expected


def test_new_request_id_matches_go_shape():
    request_id = new_request_id()
    assert re.fullmatch(r"chatcmpl-[0-9a-f-]{29}", request_id)
    assert new_request_id() != new_request_id()


def test_version_cache_pin_always_wins():
    cache = VersionCache(pin="9.9.9")
    assert asyncio.run(cache.get(httpx.AsyncClient())) == "9.9.9"


def _client_with_version(version, calls=None, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        assert str(request.url) == NPM_LATEST_URL
        if version is None:
            return httpx.Response(status, text="nope")
        return httpx.Response(status, json={"version": version})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_version_cache_fetches_then_serves_from_cache():
    calls: list[httpx.Request] = []
    cache = VersionCache()
    assert asyncio.run(cache.get(_client_with_version("1.2.3", calls))) == "1.2.3"
    assert asyncio.run(cache.get(_client_with_version("9.9.9", calls))) == "1.2.3"
    assert len(calls) == 1


def test_version_cache_refetches_after_the_ttl(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("commandcode_proxy.upstream.time.monotonic", lambda: clock[0])
    cache = VersionCache()

    assert asyncio.run(cache.get(_client_with_version("1.0.0"))) == "1.0.0"
    clock[0] += 30 * 60 + 1
    assert asyncio.run(cache.get(_client_with_version("1.0.1"))) == "1.0.1"


def test_version_cache_falls_back_to_unknown_on_failure():
    cache = VersionCache()
    assert asyncio.run(cache.get(_client_with_version(None, status=500))) == UNKNOWN_VERSION


def test_version_cache_keeps_the_last_good_value_on_failure(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("commandcode_proxy.upstream.time.monotonic", lambda: clock[0])
    cache = VersionCache()
    assert asyncio.run(cache.get(_client_with_version("1.0.0"))) == "1.0.0"

    clock[0] += 30 * 60 + 1
    assert asyncio.run(cache.get(_client_with_version(None, status=500))) == "1.0.0"


def test_version_cache_ignores_a_non_version_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": 123})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert asyncio.run(VersionCache().get(client)) == UNKNOWN_VERSION

"""End-to-end endpoint tests, with the upstream replaced by a MockTransport."""

import json
import re
from datetime import date

import httpx
import pytest

from commandcode_proxy.config import Config
from commandcode_proxy.upstream import NPM_LATEST_URL

API_KEY = "sk-test-command-code-key"
PINNED_VERSION = "0.1.2-test"


def ndjson_bytes(*events: str) -> bytes:
    """The upstream answers with one JSON event per line, not real SSE."""
    return ("\n".join(events) + "\n").encode("utf-8")


TEXT_EVENTS = ndjson_bytes(
    '{"type":"text-delta","text":"Hello"}',
    '{"type":"text-delta","text":" world"}',
    '{"type":"finish","finishReason":"stop","totalUsage":{"inputTokens":3,"outputTokens":5}}',
)

REQUEST = {
    "model": "deepseek-v4",
    "messages": [
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "hi"},
    ],
    "temperature": 0.7,
}


def bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


def decode_sse(response):
    blocks = [block for block in response.text.split("\n\n") if block.strip()]
    assert all(block.startswith("data: ") for block in blocks)
    return [json.loads(block[6:]) for block in blocks[:-1]], blocks[-1][6:]


def test_health(make_app):
    client, _ = make_app(lambda request: httpx.Response(200), config=Config())
    with client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_models_endpoint_serves_the_mapped_catalog(make_app):
    client, _ = make_app(lambda request: httpx.Response(200), config=Config())
    with client:
        response = client.get("/v1/models")

    body = response.json()
    assert response.status_code == 200
    assert body["object"] == "list"
    assert len(body["data"]) == 42
    for entry in body["data"]:
        assert set(entry) == {"id", "object", "created", "owned_by", "name"}
        assert entry["object"] == "model"
        assert entry["created"] == 0
        assert entry["name"]
    assert any(entry["id"] == "deepseek/deepseek-v4-pro" and entry["owned_by"] == "deepseek"
               and entry["name"] == "DeepSeek V4 Pro (latest)"
               for entry in body["data"])


def test_chat_non_stream_builds_the_upstream_envelope(make_app):
    client, seen = make_app(lambda request: httpx.Response(200, content=TEXT_EVENTS),
                            config=Config(command_code_version=PINNED_VERSION))
    with client:
        response = client.post("/v1/chat/completions", json=REQUEST, headers=bearer())

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "deepseek/deepseek-v4-pro"
    assert body["choices"] == [{"index": 0, "message": {"role": "assistant", "content": "Hello world"},
                                "finish_reason": "stop"}]
    assert body["usage"] == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    assert re.fullmatch(r"chatcmpl-[0-9a-f-]{29}", body["id"])

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url).endswith("/alpha/generate")
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["x-command-code-version"] == PINNED_VERSION
    assert request.headers["x-cli-environment"] == "production"
    assert request.headers["accept"] == "text/event-stream"
    assert request.headers["content-type"] == "application/json"

    envelope = json.loads(request.content)
    assert list(envelope) == ["config", "memory", "taste", "skills", "params", "threadId"]
    assert envelope["config"]["workingDir"] == "."
    assert envelope["config"]["environment"] == "cli"
    assert envelope["config"]["date"] == date.today().isoformat()
    assert envelope["config"]["isGitRepo"] is False
    assert re.fullmatch(r"[0-9a-f-]{36}", envelope["threadId"])

    params = envelope["params"]
    assert params["model"] == "deepseek/deepseek-v4-pro"
    assert params["system"] == "You are terse."
    assert params["temperature"] == 0.7
    assert params["max_tokens"] == 64000
    assert params["stream"] is True
    assert params["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert params["tools"] == []


def test_chat_stream_returns_sse_chunks(make_app):
    client, _ = make_app(lambda request: httpx.Response(200, content=TEXT_EVENTS),
                         config=Config(command_code_version=PINNED_VERSION))
    with client:
        response = client.post("/v1/chat/completions", json={**REQUEST, "stream": True},
                               headers=bearer())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"

    chunks, done = decode_sse(response)
    assert done == "[DONE]"
    assert len(chunks) == 3
    assert all(chunk["object"] == "chat.completion.chunk" for chunk in chunks)
    assert [chunk["choices"][0]["delta"] for chunk in chunks] == [
        {"role": "assistant", "content": "Hello"},
        {"content": " world"},
        {},
    ]
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


class ExplodingUpstream(httpx.Response):
    """A 200 that dies mid-stream, like a connection dropped after the headers."""

    async def aiter_lines(self):
        yield '{"type":"text-delta","text":"Hello"}'
        raise httpx.ReadError("connection reset by peer")

    async def aclose(self):
        pass


def test_mid_stream_transport_failure_returns_502(make_app):
    """UpstreamStreamError covers in-band error events; a transport-level failure
    while reading must map to the same 502 envelope instead of an unhandled 500
    with an empty body."""
    client, _ = make_app(lambda request: ExplodingUpstream(200),
                         config=Config(command_code_version=PINNED_VERSION))
    with client:
        response = client.post("/v1/chat/completions", json=REQUEST, headers=bearer())
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "api_error"
    assert "connection reset by peer" in response.json()["error"]["message"]


def test_chat_alias_route_is_registered(make_app):
    client, seen = make_app(lambda request: httpx.Response(200, content=TEXT_EVENTS),
                            config=Config(command_code_version=PINNED_VERSION))
    with client:
        response = client.post("/chat/completions", json=REQUEST, headers=bearer())
    assert response.status_code == 200
    assert str(seen[0].url).endswith("/alpha/generate")


def test_tool_calls_round_trip_in_non_stream_mode(make_app):
    tool_events = ndjson_bytes(
        '{"type":"tool-use","toolCallId":"c1","toolName":"get_weather"}',
        '{"type":"tool-delta","text":"{\\"city\\":\\"Paris\\"}"}',
        '{"type":"finish","finishReason":"tool-calls"}',
    )
    client, seen = make_app(lambda request: httpx.Response(200, content=tool_events),
                            config=Config(command_code_version=PINNED_VERSION))
    payload = {
        "model": "minimax",
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": [{"type": "function", "function": {
            "name": "get_weather",
            "description": "Weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        }}],
    }
    with client:
        response = client.post("/v1/chat/completions", json=payload, headers=bearer())

    body = response.json()
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}],
    }
    assert json.loads(seen[0].content)["params"]["model"] == "MiniMaxAI/MiniMax-M3"
    assert json.loads(seen[0].content)["params"]["tools"] == [
        {"name": "get_weather",
         "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
         "description": "Weather"},
    ]


def test_configured_key_is_used_even_when_the_client_supplies_its_own(make_app):
    """Agent clients force a value into the API key box; that placeholder must
    never reach the upstream, or the proxy 401s on every request."""
    client, seen = make_app(lambda request: httpx.Response(200, content=TEXT_EVENTS),
                            config=Config(api_key="sk-default", command_code_version=PINNED_VERSION))
    with client:
        response = client.post("/v1/chat/completions", json=REQUEST,
                               headers={"Authorization": "Bearer commandcode"})
    assert response.status_code == 200
    assert seen[0].headers["authorization"] == "Bearer sk-default"


def test_client_key_is_forwarded_when_no_default_key_is_configured(make_app):
    """With no default key the proxy authenticates nothing, so the client's real
    upstream key is required and passed through verbatim."""
    client, seen = make_app(lambda request: httpx.Response(200, content=TEXT_EVENTS),
                            config=Config(command_code_version=PINNED_VERSION))
    with client:
        response = client.post("/v1/chat/completions", json=REQUEST,
                               headers={"Authorization": "Bearer sk-real-user-key"})
    assert response.status_code == 200
    assert seen[0].headers["authorization"] == "Bearer sk-real-user-key"


def test_default_key_is_used_when_the_header_is_absent(make_app):
    client, seen = make_app(lambda request: httpx.Response(200, content=TEXT_EVENTS),
                            config=Config(api_key="sk-default", command_code_version=PINNED_VERSION))
    with client:
        response = client.post("/v1/chat/completions", json=REQUEST)
    assert response.status_code == 200
    assert seen[0].headers["authorization"] == "Bearer sk-default"


@pytest.mark.parametrize("header", [None, "", "Bearer   "])
def test_missing_api_key_returns_401(make_app, header):
    client, _ = make_app(lambda request: httpx.Response(200), config=Config())
    headers = {"Authorization": header} if header is not None else {}
    with client:
        response = client.post("/v1/chat/completions", json=REQUEST, headers=headers)
    assert response.status_code == 401
    assert response.json() == {"error": {
        "message": "API key required. Set Authorization header.",
        "type": "authentication_error",
        "param": None,
        "code": None,
    }}


def test_invalid_json_returns_400(make_app):
    client, _ = make_app(lambda request: httpx.Response(200), config=Config())
    with client:
        response = client.post("/v1/chat/completions", content="{ not json", headers=bearer())
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "Invalid JSON" in response.json()["error"]["message"]


@pytest.mark.parametrize("payload", [
    {"model": "m"},
    {"model": "m", "messages": []},
    {"model": "m", "messages": "hi"},
])
def test_missing_messages_returns_400(make_app, payload):
    client, _ = make_app(lambda request: httpx.Response(200), config=Config())
    with client:
        response = client.post("/v1/chat/completions", json=payload, headers=bearer())
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "messages array is required"


def test_upstream_4xx_is_forwarded(make_app):
    client, _ = make_app(lambda request: httpx.Response(401, text="invalid api key"),
                         config=Config(command_code_version=PINNED_VERSION))
    with client:
        response = client.post("/v1/chat/completions", json=REQUEST, headers=bearer())
    assert response.status_code == 401
    assert response.json()["error"] == {
        "message": "Upstream error: invalid api key",
        "type": "api_error",
        "param": None,
        "code": None,
    }


def test_upstream_5xx_becomes_502(make_app):
    client, _ = make_app(lambda request: httpx.Response(503, text="overloaded"),
                         config=Config(command_code_version=PINNED_VERSION))
    with client:
        response = client.post("/v1/chat/completions", json=REQUEST, headers=bearer())
    assert response.status_code == 502
    assert response.json()["error"]["message"] == "Upstream error: overloaded"


def test_upstream_connect_error_returns_502(make_app):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client, _ = make_app(handler, config=Config(command_code_version=PINNED_VERSION))
    with client:
        response = client.post("/v1/chat/completions", json=REQUEST, headers=bearer())
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "api_error"


def test_npm_version_is_used_when_nothing_is_pinned(make_app):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == NPM_LATEST_URL:
            return httpx.Response(200, json={"version": "4.5.6"})
        return httpx.Response(200, content=TEXT_EVENTS)

    client, seen = make_app(handler, config=Config())
    with client:
        response = client.post("/v1/chat/completions", json=REQUEST, headers=bearer())
    assert response.status_code == 200
    assert seen[-1].headers["x-command-code-version"] == "4.5.6"


def test_404_uses_the_openai_error_shape(make_app):
    client, _ = make_app(lambda request: httpx.Response(200), config=Config())
    with client:
        response = client.get("/v1/unknown")
    assert response.status_code == 404
    assert response.json() == {"error": {"message": "Not Found", "type": "invalid_request_error",
                                         "param": None, "code": None}}


def test_405_uses_the_openai_error_shape(make_app):
    client, _ = make_app(lambda request: httpx.Response(200), config=Config())
    with client:
        response = client.get("/v1/chat/completions")
    assert response.status_code == 405
    assert response.json() == {"error": {"message": "Method not allowed",
                                         "type": "invalid_request_error",
                                         "param": None, "code": None}}


def test_unknown_model_name_is_passed_through(make_app):
    client, seen = make_app(lambda request: httpx.Response(200, content=TEXT_EVENTS),
                            config=Config(command_code_version=PINNED_VERSION))
    payload = {"model": "some/brand-new-model", "messages": [{"role": "user", "content": "hi"}]}
    with client:
        response = client.post("/v1/chat/completions", json=payload, headers=bearer())
    assert response.status_code == 200
    assert json.loads(seen[0].content)["params"]["model"] == "some/brand-new-model"
    assert response.json()["model"] == "some/brand-new-model"


def test_non_stream_clients_still_get_an_upstream_stream(make_app):
    client, seen = make_app(lambda request: httpx.Response(200, content=TEXT_EVENTS),
                            config=Config(command_code_version=PINNED_VERSION))
    with client:
        client.post("/v1/chat/completions", json=REQUEST, headers=bearer())
    assert json.loads(seen[0].content)["params"]["stream"] is True


def test_access_line_is_ascii_and_carries_the_key_facts():
    from commandcode_proxy.api import access_line
    from commandcode_proxy.responses import RequestStats

    stats = RequestStats()
    stats.events = 74
    stats.finish_reason = "stop"
    stats.usage = {
        "prompt_tokens": 7574,
        "completion_tokens": 74,
        "total_tokens": 7648,
        "completion_tokens_details": {"reasoning_tokens": 79, "text_tokens": -5},
    }
    line = access_line(stats, "step3.5", "stepfun/Step-3.5-Flash", "chat", "200")
    line.encode("ascii")  # raises if a non-ASCII character sneaks in
    for fragment in ("[chat]", "step3.5 -> stepfun/Step-3.5-Flash", "HTTP 200",
                     "in=7574", "out=74", "total=7648", "reason=79", "text=-5",
                     "events=74", "finish=stop"):
        assert fragment in line


def test_access_line_survives_non_ascii_model_names():
    """cmd.exe consoles render GBK, so a model name must never reach the log raw."""
    from commandcode_proxy.api import access_line
    from commandcode_proxy.responses import RequestStats

    line = access_line(RequestStats(), "模型A", "供应商/模型B", "chat stream", "200")
    line.encode("ascii")
    assert "?" in line
    assert "模型" not in line


def test_access_line_marks_a_broken_stream():
    from commandcode_proxy.api import access_line
    from commandcode_proxy.responses import RequestStats

    line = access_line(RequestStats(), "m", "M", "chat stream", "stream-broken")
    assert "[chat stream]" in line and "stream-broken" in line
    assert "tokens=?" in line


def test_usage_summary_handles_missing_usage():
    from commandcode_proxy.api import usage_summary
    assert usage_summary(None) == "tokens=?"
    assert usage_summary({}) == "in=0 out=0 total=0"
    assert "prompt_tokens_details" not in usage_summary(
        {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
         "prompt_tokens_details": {"cached_tokens": 1}})


def test_ascii_only_replaces_every_non_ascii_character():
    from commandcode_proxy.api import ascii_only
    assert ascii_only("abc 123") == "abc 123"
    assert ascii_only("模型 你好") == "?? ??"

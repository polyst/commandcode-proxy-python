"""Upstream event stream -> OpenAI responses.

The upstream answers ``/alpha/generate`` with the Vercel AI SDK data-stream
vocabulary: one JSON object per line, regardless of the
``Accept: text/event-stream`` header.

    start, start-step
    reasoning-start / reasoning-delta / reasoning-end
    text-delta
    tool-input-start / tool-input-delta / tool-call     (provider dependent)
    finish-step, finish, provider-metadata
    error, abort

Two consumers sit on that stream:

  * ``stream_chunks``     — re-emits each event as an SSE ``chat.completion.chunk``
  * ``collect_completion`` — drains the stream and folds it into one JSON body

The event vocabulary was verified against the live upstream and against
``consumeStream`` in the command-code CLI bundle; it is *not* the vocabulary in
the Go reference proxy (``tool-use`` / ``tool-delta`` are no longer emitted, and
reasoning output was never supported there).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

from .upstream import normalize_finish_reason

log = logging.getLogger("commandcode_proxy.responses")

DONE_EVENT = b"data: [DONE]\n\n"


@dataclass
class RequestStats:
    """What flowed through the upstream event stream for one request.

    Filled in by ``instrument`` so the access log can report token counts,
    finish reason and time-to-first-event for both the streaming and the
    aggregated path without either of them having to expose internals.
    """

    started_at: float = field(default_factory=time.monotonic)
    first_event_at: float | None = None
    events: int = 0
    text_deltas: int = 0
    reasoning_deltas: int = 0
    tool_calls: int = 0
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None
    cost: float | None = None
    error: str | None = None

    @property
    def ttft(self) -> float | None:
        """Seconds from the request start to the first upstream event."""
        return None if self.first_event_at is None else self.first_event_at - self.started_at

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at


def extract_cost(event: dict[str, Any]) -> float | None:
    """Pull the gateway's usage.cost out of a provider-metadata event, if any."""
    metadata = event.get("providerMetadata")
    if not isinstance(metadata, dict):
        return None
    for value in metadata.values():
        if not isinstance(value, dict):
            continue
        usage_blob = value.get("usage")
        if isinstance(usage_blob, dict) and isinstance(usage_blob.get("cost"), (int, float)):
            return float(usage_blob["cost"])
    return None


async def instrument(events: AsyncIterator[dict[str, Any]],
                     stats: RequestStats) -> AsyncIterator[dict[str, Any]]:
    """Yield every event unchanged while recording counts and the final usage."""
    async for event in events:
        stats.events += 1
        if stats.first_event_at is None:
            stats.first_event_at = time.monotonic()

        event_type = event.get("type") if isinstance(event.get("type"), str) else ""
        if event_type == "text-delta":
            stats.text_deltas += 1
        elif event_type == "reasoning-delta":
            stats.reasoning_deltas += 1
        elif event_type in ("tool-call", "tool-input-start"):
            stats.tool_calls += 1
        elif event_type == "finish":
            stats.finish_reason = normalize_finish_reason(event.get("finishReason"))
            stats.usage = build_usage(event.get("totalUsage"), stats.cost)
        elif event_type == "error":
            stats.error = error_brief(event)
        elif event_type == "provider-metadata":
            stats.cost = extract_cost(event) or stats.cost

        yield event


def openai_error(message: str, error_type: str = "invalid_request_error") -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "param": None, "code": None}}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def encode_sse(data: dict[str, Any] | str) -> bytes:
    payload = _json_bytes(data) if isinstance(data, dict) else data.encode("utf-8")
    return b"data: " + payload + b"\n\n"


def _chunk(request_id: str, model: str, created: int, choice: dict[str, Any] | None,
           usage: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [] if choice is None else [choice],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _str(value: dict[str, Any], *keys: str) -> str:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str):
            return item
    return ""


def _input_json(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _with_role(delta: dict[str, Any], sent_role: bool) -> tuple[dict[str, Any], bool]:
    if sent_role:
        return delta, True
    return {"role": "assistant", **delta}, True


def build_usage(total_usage: dict[str, Any] | None, cost: float | None) -> dict[str, Any]:
    """Map the upstream usage blob onto OpenAI's CompletionUsage shape."""
    total_usage = total_usage if isinstance(total_usage, dict) else {}
    input_tokens = total_usage.get("inputTokens") or 0
    output_tokens = total_usage.get("outputTokens") or 0

    usage: dict[str, Any] = {
        "prompt_tokens": input_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": total_usage.get("totalTokens") or (input_tokens + output_tokens),
    }

    input_details = total_usage.get("inputTokenDetails")
    if isinstance(input_details, dict):
        prompt_details = {
            key: value for key, value in (
                ("cached_tokens", input_details.get("cacheReadTokens")),
                ("no_cache_tokens", input_details.get("noCacheTokens")),
                ("cache_creation_tokens", input_details.get("cacheWriteTokens")),
            ) if value is not None
        }
        if prompt_details:
            usage["prompt_tokens_details"] = prompt_details

    output_details = total_usage.get("outputTokenDetails")
    if isinstance(output_details, dict):
        completion_details = {
            key: value for key, value in (
                ("reasoning_tokens", output_details.get("reasoningTokens")),
                ("text_tokens", output_details.get("textTokens")),
            ) if value is not None
        }
        if completion_details:
            usage["completion_tokens_details"] = completion_details

    if cost is not None:
        usage["cost"] = cost
    return usage


async def iter_events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Yield one parsed event per non-blank line; malformed lines are dropped."""
    async for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            log.warning("dropping a malformed upstream line: %s", line[:200])
            continue
        if isinstance(event, dict):
            log.debug("[DEBUG] upstream event: %s", line[:20000])
            yield event


def wants_stream_usage(request: dict[str, Any]) -> bool:
    options = request.get("stream_options")
    return isinstance(options, dict) and options.get("include_usage") is True


async def stream_chunks(
    events: AsyncIterator[dict[str, Any]],
    request_id: str,
    model: str,
    created: int,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    include_usage: bool = False,
) -> AsyncIterator[bytes]:
    """Translate the upstream stream into SSE bytes, finishing with [DONE]."""
    sent_role = False
    tool_call_index = 0
    tool_call_indexes: dict[str, int] = {}
    usage: dict[str, Any] | None = None
    cost: float | None = None
    async for event in events:
        if is_disconnected is not None and await is_disconnected():
            log.info("client disconnected; aborting upstream stream")
            return

        event_type = event.get("type") if isinstance(event.get("type"), str) else ""

        if event_type == "text-delta":
            delta, sent_role = _with_role({"content": _str(event, "text")}, sent_role)
            yield encode_sse(_chunk(request_id, model, created, {"index": 0, "delta": delta}))

        elif event_type == "reasoning-delta":
            text = _str(event, "text")
            if text:
                delta, sent_role = _with_role({"reasoning": text}, sent_role)
                yield encode_sse(_chunk(request_id, model, created, {"index": 0, "delta": delta}))

        elif event_type == "tool-use":
            # Legacy vocabulary from the Go reference; no longer emitted upstream.
            tool_calls = [{"index": tool_call_index, "id": _str(event, "toolCallId"),
                           "type": "function", "function": {"name": _str(event, "toolName")}}]
            delta, sent_role = _with_role({"tool_calls": tool_calls}, sent_role)
            yield encode_sse(_chunk(request_id, model, created, {"index": 0, "delta": delta}))
            tool_call_index += 1

        elif event_type == "tool-delta":
            delta = {"tool_calls": [{"index": max(tool_call_index - 1, 0),
                                     "function": {"arguments": _str(event, "text")}}]}
            yield encode_sse(_chunk(request_id, model, created, {"index": 0, "delta": delta}))

        elif event_type == "tool-input-start":
            event_id = _str(event, "id")
            if event_id not in tool_call_indexes:
                tool_call_indexes[event_id] = tool_call_index
                tool_call_index += 1
            tool_calls = [{"index": tool_call_indexes[event_id], "id": event_id, "type": "function",
                           "function": {"name": _str(event, "toolName")}}]
            delta, sent_role = _with_role({"tool_calls": tool_calls}, sent_role)
            yield encode_sse(_chunk(request_id, model, created, {"index": 0, "delta": delta}))

        elif event_type == "tool-input-delta":
            event_id = _str(event, "id")
            index = tool_call_indexes.get(event_id)
            if index is None:
                index = tool_call_index
                tool_call_indexes[event_id] = index
                tool_call_index += 1
            delta = {"tool_calls": [{"index": index,
                                     "function": {"arguments": _str(event, "delta")}}]}
            yield encode_sse(_chunk(request_id, model, created, {"index": 0, "delta": delta}))

        elif event_type == "tool-call":
            call_id = _str(event, "toolCallId")
            if call_id and call_id in tool_call_indexes:
                continue
            index = tool_call_index
            if call_id:
                tool_call_indexes[call_id] = index
            tool_call_index += 1
            arguments = _input_json(event.get("input") if event.get("input") is not None
                                    else event.get("args"))
            tool_calls = [{"index": index, "id": call_id, "type": "function",
                           "function": {"name": _str(event, "toolName"), "arguments": arguments}}]
            delta, sent_role = _with_role({"tool_calls": tool_calls}, sent_role)
            yield encode_sse(_chunk(request_id, model, created, {"index": 0, "delta": delta}))

        elif event_type == "provider-metadata":
            cost = extract_cost(event) or cost

        elif event_type == "finish":
            reason = normalize_finish_reason(event.get("finishReason"))
            yield encode_sse(_chunk(request_id, model, created,
                                    {"index": 0, "delta": {}, "finish_reason": reason}))
            if include_usage:
                yield encode_sse(_chunk(request_id, model, created, None,
                                        build_usage(event.get("totalUsage"), cost)))
            yield DONE_EVENT

        elif event_type == "error":
            # Relay it. Swallowing this used to leave the client with a 200 and an
            # empty completion, which every agent renders as "model returned no
            # content" - far less useful than the upstream's own message.
            log.error("upstream stream error: %s", error_brief(event))
            yield encode_sse(upstream_error_payload(event))
            yield DONE_EVENT
            return

        elif event_type == "abort":
            log.warning("upstream aborted the stream")
            yield encode_sse(openai_error("Upstream aborted the request.", "api_error"))
            yield DONE_EVENT
            return

        # start, start-step, reasoning-start, reasoning-end, finish-step,
        # provider-metadata without usage, tool-result: nothing to relay.



async def collect_completion(
    events: AsyncIterator[dict[str, Any]],
    request_id: str,
    model: str,
    created: int,
) -> dict[str, Any]:
    """Drain the upstream stream into a single OpenAI chat.completion body."""
    content = ""
    reasoning = ""
    usage: dict[str, Any] | None = None
    cost: float | None = None
    has_tool_calls = False
    tool_calls: list[dict[str, Any]] = []
    tool_call_by_id: dict[str, int] = {}

    async for event in events:
        event_type = event.get("type") if isinstance(event.get("type"), str) else ""

        if event_type == "text-delta":
            content += _str(event, "text")

        elif event_type == "reasoning-delta":
            reasoning += _str(event, "text")

        elif event_type == "tool-use":
            has_tool_calls = True
            call_id = _str(event, "toolCallId")
            tool_call_by_id[call_id] = len(tool_calls)
            tool_calls.append({"id": call_id, "type": "function",
                               "function": {"name": _str(event, "toolName"), "arguments": ""}})

        elif event_type == "tool-delta":
            if tool_calls:
                tool_calls[-1]["function"]["arguments"] += _str(event, "text")

        elif event_type == "tool-input-start":
            has_tool_calls = True
            call_id = _str(event, "id")
            tool_call_by_id[call_id] = len(tool_calls)
            tool_calls.append({"id": call_id, "type": "function",
                               "function": {"name": _str(event, "toolName"), "arguments": ""}})

        elif event_type == "tool-input-delta":
            index = tool_call_by_id.get(_str(event, "id"))
            if index is not None:
                tool_calls[index]["function"]["arguments"] += _str(event, "delta")

        elif event_type == "tool-call":
            has_tool_calls = True
            call_id = _str(event, "toolCallId")
            arguments = _input_json(event.get("input") if event.get("input") is not None
                                    else event.get("args"))
            index = tool_call_by_id.get(call_id)
            if index is not None:
                tool_calls[index]["function"]["name"] = _str(event, "toolName")
                if arguments:
                    tool_calls[index]["function"]["arguments"] = arguments
            else:
                tool_call_by_id[call_id] = len(tool_calls)
                tool_calls.append({"id": call_id, "type": "function",
                                   "function": {"name": _str(event, "toolName"),
                                                "arguments": arguments}})

        elif event_type == "provider-metadata":
            cost = extract_cost(event) or cost

        elif event_type == "finish":
            usage = build_usage(event.get("totalUsage"), cost)
            finish_reason = normalize_finish_reason(event.get("finishReason"))
            if has_tool_calls:
                finish_reason = "tool_calls"
            return {
                "id": request_id,
                "object": "chat.completion",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "message": _message(content, reasoning, tool_calls),
                             "finish_reason": finish_reason}],
                "usage": usage,
            }

        elif event_type == "error":
            raise UpstreamStreamError(_error_message(event), _error_status(event))

        elif event_type == "abort":
            raise UpstreamStreamError("upstream aborted the request")

    return {
        "id": request_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": _message(content, reasoning, tool_calls),
                     "finish_reason": "tool_calls" if has_tool_calls else "stop"}],
        "usage": usage or build_usage(None, cost),
    }


def _message(content: str, reasoning: str,
             tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """No tool calls: content is always present (Go parity, even when empty)."""
    if tool_calls:
        message: dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
    else:
        message = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning"] = reasoning
    return message


def _parse_upstream_error(event: dict[str, Any]) -> tuple[str, str, int | None]:
    """Unpack the upstream's in-band error into (message, code, status).

    The upstream sends ``{"type": "error", "error": {message, statusCode, type,
    isRetryable}}``. Both branches of ``error`` (string or object) are handled.
    """
    error = event.get("error")
    if isinstance(error, str):
        return error, "api_error", None
    if isinstance(error, dict):
        message = error.get("message")
        message = message if isinstance(message, str) and message else "upstream error"
        code = error.get("type")
        code = code if isinstance(code, str) and code else "api_error"
        status = error.get("statusCode")
        return message, code, status if isinstance(status, int) else None
    return "upstream error", "api_error", None


def upstream_error_payload(event: dict[str, Any]) -> dict[str, Any]:
    """The upstream error as an OpenAI error envelope, for streaming responses.

    The status line is already on the wire as 200 by the time this is known, so
    the envelope carries the upstream's own status code instead. Emitting it as a
    ``data:`` payload is what lets a client tell "the model returned nothing"
    apart from "the upstream said 503" - previously this event was swallowed and
    the client just saw an empty completion.
    """
    message, code, status = _parse_upstream_error(event)
    body = openai_error(message, code)
    if status is not None:
        body["error"]["status"] = status
    return body


def error_brief(event: dict[str, Any]) -> str:
    """A one-line rendering of the upstream error, for the access log."""
    message, code, status = _parse_upstream_error(event)
    head = f"HTTP {status}" if status is not None else code
    return f"{head} {code}: {message}"[:300]


def _error_message(event: dict[str, Any]) -> str:
    return _parse_upstream_error(event)[0]


def _error_status(event: dict[str, Any]) -> int | None:
    return _parse_upstream_error(event)[2]


class UpstreamStreamError(Exception):
    """The upstream reported a terminal error inside the event stream.

    ``status`` is the upstream's own status code when it supplied one, so the
    proxy can surface it rather than flattening everything to a 502.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

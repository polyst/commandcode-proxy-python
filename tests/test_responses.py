"""Streaming and aggregation tests for the upstream -> OpenAI direction."""

import asyncio
import json

import pytest

from commandcode_proxy.responses import (
    DONE_EVENT,
    RequestStats,
    UpstreamStreamError,
    build_usage,
    collect_completion,
    extract_cost,
    instrument,
    iter_events,
    openai_error,
    stream_chunks,
    wants_stream_usage,
)


async def aiter(items):
    for item in items:
        yield item


def drain(generator):
    async def run():
        return [item async for item in generator]

    return asyncio.run(run())


def decode_sse(chunks):
    """Split SSE bytes into the ``data:`` payloads."""
    blocks = [block for block in b"".join(chunks).decode("utf-8").split("\n\n") if block.strip()]
    assert all(block.startswith("data: ") for block in blocks)
    return [block[len("data: "):] for block in blocks]


def parse_sse(chunks):
    """Return (parsed chunk payloads, [DONE] marker or None)."""
    payloads = decode_sse(chunks)
    if payloads and payloads[-1] == "[DONE]":
        return [json.loads(payload) for payload in payloads[:-1]], payloads[-1]
    return [json.loads(payload) for payload in payloads], None


def test_stream_text_then_finish():
    events = [
        {"type": "text-delta", "text": "Hello"},
        {"type": "text-delta", "text": " world"},
        {"type": "finish", "finishReason": "stop",
         "totalUsage": {"inputTokens": 3, "outputTokens": 5}},
    ]
    chunks = drain(stream_chunks(aiter(events), "chatcmpl-abc", "m", 123))

    data, done = parse_sse(chunks)
    assert len(chunks) == 4
    assert chunks[-1] == DONE_EVENT
    assert done == "[DONE]"

    assert data[0]["id"] == "chatcmpl-abc"
    assert data[0]["object"] == "chat.completion.chunk"
    assert data[0]["created"] == 123
    assert data[0]["model"] == "m"
    assert data[0]["choices"] == [{"index": 0, "delta": {"role": "assistant", "content": "Hello"}}]
    assert data[1]["choices"] == [{"index": 0, "delta": {"content": " world"}}]
    assert data[2]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]


def test_stream_tool_use_and_tool_delta():
    events = [
        {"type": "tool-use", "toolCallId": "c1", "toolName": "get_weather"},
        {"type": "tool-delta", "text": '{"city":'},
        {"type": "tool-delta", "text": '"Paris"}'},
        {"type": "finish", "finishReason": "tool-calls"},
    ]
    data, _ = parse_sse(drain(stream_chunks(aiter(events), "r", "m", 1)))

    assert data[0]["choices"] == [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
        {"index": 0, "id": "c1", "type": "function", "function": {"name": "get_weather"}}]}}]
    assert data[1]["choices"] == [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": '{"city":'}}]}}]
    assert data[2]["choices"] == [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": '"Paris"}'}}]}}]
    assert data[3]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]


def test_stream_tool_input_start_and_delta_number_tool_calls():
    events = [
        {"type": "tool-input-start", "id": "c1", "toolName": "f"},
        {"type": "tool-input-delta", "id": "c1", "delta": "abc"},
        {"type": "tool-input-delta", "id": "c2", "delta": "xyz"},
    ]
    data, _ = parse_sse(drain(stream_chunks(aiter(events), "r", "m", 1)))

    assert data[0]["choices"] == [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
        {"index": 0, "id": "c1", "type": "function", "function": {"name": "f"}}]}}]
    assert data[1]["choices"] == [{"index": 0, "delta": {"tool_calls": [
        {"index": 0, "function": {"arguments": "abc"}}]}}]
    assert data[2]["choices"] == [{"index": 0, "delta": {"tool_calls": [
        {"index": 1, "function": {"arguments": "xyz"}}]}}]


def test_stream_tool_call_is_deduplicated():
    event = {"type": "tool-call", "toolCallId": "c1", "toolName": "f", "input": {"a": 1}}
    data, _ = parse_sse(drain(stream_chunks(aiter([event, dict(event), {"type": "finish", "finishReason": ""}]),
                                            "r", "m", 1)))

    assert len(data) == 2
    assert data[0]["choices"] == [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
        {"index": 0, "id": "c1", "type": "function",
         "function": {"name": "f", "arguments": '{"a":1}'}}]}}]
    assert data[1]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]


def test_stream_without_finish_omits_done():
    chunks = drain(stream_chunks(aiter([{"type": "text-delta", "text": "x"}]), "r", "m", 1))
    assert DONE_EVENT not in chunks


@pytest.mark.parametrize(("reason", "expected"), [
    ("tool_calls", "tool_calls"),
    ("tool-calls", "tool_calls"),
    ("length", "length"),
    ("max_tokens", "length"),
    ("content_filter", "content_filter"),
    ("content-filter", "content_filter"),
    ("", "stop"),
    (None, "stop"),
    ("something-else", "stop"),
])
def test_normalize_finish_reason(reason, expected):
    from commandcode_proxy.upstream import normalize_finish_reason
    assert normalize_finish_reason(reason) == expected


def test_collect_completion_aggregates_text():
    events = [
        {"type": "text-delta", "text": "Hello"},
        {"type": "text-delta", "text": " world"},
        {"type": "finish", "totalUsage": {"inputTokens": 3, "outputTokens": 5}},
    ]
    body = asyncio.run(collect_completion(aiter(events), "chatcmpl-z", "Acme/M", 42))
    assert body == {
        "id": "chatcmpl-z",
        "object": "chat.completion",
        "created": 42,
        "model": "Acme/M",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello world"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
    }


def test_collect_completion_no_finish_event_reports_zero_usage():
    body = asyncio.run(collect_completion(aiter([{"type": "text-delta", "text": "Hi"}]), "r", "m", 1))
    assert body["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    assert body["choices"][0]["finish_reason"] == "stop"


def test_reasoning_is_streamed_as_a_separate_delta():
    events = [
        {"type": "reasoning-start", "id": "r1"},
        {"type": "reasoning-delta", "id": "r1", "text": "let me "},
        {"type": "reasoning-delta", "id": "r1", "text": "think"},
        {"type": "reasoning-end", "id": "r1", "providerMetadata": {"openrouter": {}}},
        {"type": "text-delta", "text": "42"},
        {"type": "finish-step", "finishReason": "stop", "usage": {}},
        {"type": "finish", "finishReason": "stop", "totalUsage": {"inputTokens": 1, "outputTokens": 1}},
    ]
    data, done = parse_sse(drain(stream_chunks(aiter(events), "r", "m", 1)))
    assert done == "[DONE]"
    assert [chunk["choices"][0]["delta"] for chunk in data[:-1]] == [
        {"role": "assistant", "reasoning": "let me "},
        {"reasoning": "think"},
        {"content": "42"},
    ]
    assert data[-1]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]


def test_reasoning_is_accumulated_in_non_stream_mode():
    events = [
        {"type": "reasoning-delta", "text": "hmm "},
        {"type": "reasoning-delta", "text": "yes"},
        {"type": "text-delta", "text": "42"},
        {"type": "finish", "finishReason": "stop",
         "totalUsage": {"inputTokens": 10, "outputTokens": 4}},
    ]
    body = asyncio.run(collect_completion(aiter(events), "r", "m", 1))
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "42",
        "reasoning": "hmm yes",
    }


def test_build_usage_maps_the_upstream_blob():
    usage = {
        "inputTokens": 7584,
        "outputTokens": 40,
        "totalTokens": 7624,
        "inputTokenDetails": {"noCacheTokens": 7584, "cacheReadTokens": 0, "cacheWriteTokens": 12},
        "outputTokenDetails": {"textTokens": 5, "reasoningTokens": 35},
    }
    assert build_usage(usage, 0.00312) == {
        "prompt_tokens": 7584,
        "completion_tokens": 40,
        "total_tokens": 7624,
        "prompt_tokens_details": {
            "cached_tokens": 0,
            "no_cache_tokens": 7584,
            "cache_creation_tokens": 12,
        },
        "completion_tokens_details": {"reasoning_tokens": 35, "text_tokens": 5},
        "cost": 0.00312,
    }


def test_build_usage_is_bare_when_there_is_no_detail_block():
    assert build_usage({"inputTokens": 3, "outputTokens": 5}, None) == {
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "total_tokens": 8,
    }


def test_cost_is_read_from_provider_metadata():
    events = [
        {"type": "text-delta", "text": "hi"},
        {"type": "provider-metadata",
         "providerMetadata": {"openrouter": {"usage": {"cost": 0.0007609}}}},
        {"type": "finish", "finishReason": "stop",
         "totalUsage": {"inputTokens": 7573, "outputTokens": 12}},
    ]
    body = asyncio.run(collect_completion(aiter(events), "r", "m", 1))
    assert body["usage"]["cost"] == 0.0007609
    assert body["usage"]["prompt_tokens"] == 7573


def test_stream_usage_chunk_is_only_emitted_when_asked():
    events = [
        {"type": "text-delta", "text": "hi"},
        {"type": "finish", "finishReason": "stop", "totalUsage": {"inputTokens": 1, "outputTokens": 1}},
    ]
    data, _ = parse_sse(drain(stream_chunks(aiter(events), "r", "m", 1, include_usage=False)))
    assert all("usage" not in chunk for chunk in data)

    data, _ = parse_sse(drain(stream_chunks(aiter(events), "r", "m", 1, include_usage=True)))
    assert data[-1]["choices"] == []
    assert data[-1]["usage"] == {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}


def test_wants_stream_usage():
    assert wants_stream_usage({"stream_options": {"include_usage": True}}) is True
    for payload in ({}, {"stream_options": {}}, {"stream_options": {"include_usage": False}},
                    {"stream_options": "yes"}):
        assert wants_stream_usage(payload) is False


def test_upstream_error_event_raises_in_non_stream_mode():
    events = [{"type": "error", "error": {"message": "model_not_in_plan", "statusCode": 403}}]
    with pytest.raises(UpstreamStreamError, match="model_not_in_plan"):
        asyncio.run(collect_completion(aiter(events), "r", "m", 1))


def test_upstream_abort_raises_in_non_stream_mode():
    with pytest.raises(UpstreamStreamError, match="aborted"):
        asyncio.run(collect_completion(aiter([{"type": "abort"}]), "r", "m", 1))


def test_stream_stops_on_abort_without_a_done_marker():
    chunks = drain(stream_chunks(aiter([{"type": "text-delta", "text": "x"}, {"type": "abort"}]),
                                 "r", "m", 1))
    assert DONE_EVENT not in chunks


def test_finish_reason_comes_from_the_upstream_finish_reason():
    body = asyncio.run(collect_completion(
        aiter([{"type": "text-delta", "text": "x"},
               {"type": "finish", "finishReason": "length", "rawFinishReason": "max_tokens"}]),
        "r", "m", 1))
    assert body["choices"][0]["finish_reason"] == "length"


def test_tool_calls_override_the_finish_reason_in_non_stream_mode():
    body = asyncio.run(collect_completion(
        aiter([{"type": "tool-call", "toolCallId": "c1", "toolName": "f", "input": {}},
               {"type": "finish", "finishReason": "stop"}]),
        "r", "m", 1))
    assert body["choices"][0]["finish_reason"] == "tool_calls"


def test_collect_completion_tool_use_and_delta():
    events = [
        {"type": "tool-use", "toolCallId": "c1", "toolName": "get_weather"},
        {"type": "tool-delta", "text": '{"city":"Paris"}'},
        {"type": "finish", "finishReason": "tool-calls"},
    ]
    body = asyncio.run(collect_completion(aiter(events), "r", "m", 1))
    assert body["choices"] == [{"index": 0, "message": {
        "role": "assistant",
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}],
    }, "finish_reason": "tool_calls"}]
    assert "content" not in body["choices"][0]["message"]


def test_collect_completion_tool_input_stream():
    events = [
        {"type": "tool-input-start", "id": "c1", "toolName": "f"},
        {"type": "tool-input-delta", "id": "c1", "delta": '{"a":'},
        {"type": "tool-input-delta", "id": "c1", "delta": "1}"},
    ]
    body = asyncio.run(collect_completion(aiter(events), "r", "m", 1))
    assert body["choices"][0]["message"]["tool_calls"][0]["function"] == {
        "name": "f",
        "arguments": '{"a":1}',
    }


def test_collect_completion_tool_call_merges_into_existing_id():
    events = [
        {"type": "tool-use", "toolCallId": "c1", "toolName": "placeholder"},
        {"type": "tool-delta", "text": "partial"},
        {"type": "tool-call", "toolCallId": "c1", "toolName": "get_weather",
         "input": {"city": "Paris"}},
    ]
    body = asyncio.run(collect_completion(aiter(events), "r", "m", 1))
    assert body["choices"][0]["message"]["tool_calls"] == [{
        "id": "c1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
    }]


def test_collect_completion_tool_call_appends_unknown_id():
    events = [{"type": "tool-call", "toolCallId": "c9", "toolName": "f", "input": {"a": 1}}]
    body = asyncio.run(collect_completion(aiter(events), "r", "m", 1))
    assert body["choices"][0]["message"]["tool_calls"] == [{
        "id": "c9", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}},
    ]


def test_iter_events_drops_blank_and_malformed_lines():
    async def lines():
        yield '{"type": "text-delta", "text": "a"}\n'
        yield ''
        yield '   '
        yield 'not json'
        yield '[1, 2]'
        yield '{"type": "other"}'

    events = drain(iter_events(lines()))
    assert events == [{"type": "text-delta", "text": "a"}, {"type": "other"}]


def test_openai_error_shape():
    assert openai_error("boom", "api_error") == {
        "error": {"message": "boom", "type": "api_error", "param": None, "code": None},
    }


def test_instrument_passes_events_through_unchanged():
    events = [{"type": "text-delta", "text": "a"}, {"type": "finish", "finishReason": "stop"}]
    stats = RequestStats()
    seen = drain(instrument(aiter(events), stats))
    assert seen == events


def test_instrument_records_counts_usage_and_finish_reason():
    events = [
        {"type": "start"},
        {"type": "reasoning-delta", "text": "hmm"},
        {"type": "text-delta", "text": "hi"},
        {"type": "tool-input-start", "id": "t1", "toolName": "now"},
        {"type": "provider-metadata",
         "providerMetadata": {"gateway": {"usage": {"cost": 0.01}}}},
        {"type": "finish", "finishReason": "stop",
         "totalUsage": {"inputTokens": 10, "outputTokens": 3,
                        "outputTokenDetails": {"reasoningTokens": 2, "textTokens": 1}}},
    ]
    stats = RequestStats()
    drain(instrument(aiter(events), stats))

    assert stats.events == 6
    assert stats.reasoning_deltas == 1
    assert stats.text_deltas == 1
    assert stats.tool_calls == 1
    assert stats.finish_reason == "stop"
    assert stats.cost == 0.01
    assert stats.usage["prompt_tokens"] == 10
    assert stats.usage["completion_tokens"] == 3
    assert stats.usage["completion_tokens_details"] == {"reasoning_tokens": 2, "text_tokens": 1}
    assert stats.ttft is not None and stats.ttft >= 0
    assert stats.elapsed >= stats.ttft


def test_instrument_reports_no_ttft_before_any_event():
    stats = RequestStats()
    assert stats.ttft is None
    assert stats.events == 0


@pytest.mark.parametrize(("event", "expected"), [
    ({"type": "provider-metadata",
      "providerMetadata": {"openrouter": {"usage": {"cost": 0.0007609}}}}, 0.0007609),
    ({"type": "provider-metadata", "providerMetadata": {"a": {"usage": {}}}}, None),
    ({"type": "provider-metadata", "providerMetadata": "not a dict"}, None),
    ({"type": "text-delta", "text": "x"}, None),
    ({}, None),
])
def test_extract_cost_only_reads_a_numeric_cost(event, expected):
    assert extract_cost(event) == expected

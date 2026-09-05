"""Message and tool conversion tests, mirrored from internal/proxy/convert.go."""

from commandcode_proxy.convert import (
    content_part_to_string,
    content_to_string,
    convert_messages,
    convert_tools,
    extract_system,
    parse_content,
    parse_tool_input,
)


def test_extract_system_joins_chunks_with_newlines():
    messages = [
        {"role": "system", "content": "A"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "B"},
    ]
    system, rest = extract_system(messages)
    assert system == "A\nB"
    assert rest == [{"role": "user", "content": "hi"}]


def test_extract_system_is_empty_when_absent():
    messages = [{"role": "user", "content": "hi"}]
    assert extract_system(messages) == ("", messages)


def test_extract_system_flattens_array_content():
    system, rest = extract_system([
        {"role": "system", "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]},
        {"role": "user", "content": "hi"},
    ])
    assert system == "onetwo"
    assert len(rest) == 1


def test_simple_text_message():
    assert convert_messages([{"role": "user", "content": "hello"}]) == [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
    ]


def test_empty_or_absent_content_serialises_as_null():
    assert convert_messages([{"role": "user", "content": ""}]) == [{"role": "user", "content": None}]
    assert convert_messages([{"role": "user"}]) == [{"role": "user", "content": None}]
    assert convert_messages([{"role": "user", "content": None}]) == [{"role": "user", "content": None}]


def test_thinking_becomes_a_reasoning_part():
    parts = parse_content([
        {"type": "text", "text": "final"},
        {"type": "thinking", "thinking": "hmm"},
        {"type": "reasoning", "reasoning": "also"},
        {"type": "redacted_thinking", "redacted_thinking": "red"},
        {"type": "refusal", "refusal": "no"},
        {"type": "output_text", "output_text": "tail"},
    ], {})
    assert parts == [
        {"type": "text", "text": "final"},
        {"type": "reasoning", "text": "hmm"},
        {"type": "reasoning", "text": "also"},
        {"type": "reasoning", "text": "red"},
        {"type": "text", "text": "no"},
        {"type": "text", "text": "tail"},
    ]


def test_image_part_becomes_an_image_part():
    parts = parse_content([
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64,AAA", "detail": "high"}},
        {"type": "image_url", "image_url": "https://x/y.png"},
        {"type": "image", "image": "https://x/z.webp"},
        {"type": "input_image", "image_url": {"url": "https://x/bare.png"}},
    ], {})
    assert parts == [
        {"type": "image", "image": "data:image/png;base64,AAA", "mimeType": "image/png"},
        {"type": "image", "image": "https://x/y.png"},
        {"type": "image", "image": "https://x/z.webp"},
        {"type": "image", "image": "https://x/bare.png"},
    ]


def test_image_part_to_string_is_flattened_for_text_contexts():
    assert content_part_to_string({"type": "image_url", "image_url": {"url": "https://x/y.png"}}) \
        == "[Image URL: https://x/y.png]"
    assert content_part_to_string({"type": "image_url", "image_url": "https://x/y.png"}) \
        == "[Image URL: https://x/y.png]"


def test_text_key_wins_over_content_key():
    assert content_part_to_string({"content": "first", "text": "second"}) == "second"


def test_unknown_part_types_are_dropped():
    parts = parse_content([
        {"type": "input_audio", "input_audio": {"data": "abc"}},
        "not-a-dict",
        {"type": "image"},                      # image with no url
        {"type": "video_url", "video_url": "x"},
    ], {})
    assert parts == []


def test_tool_message_uses_explicit_name():
    assert convert_messages([
        {"role": "tool", "tool_call_id": "call_1", "name": "get_weather", "content": "sunny"},
    ]) == [{
        "role": "tool",
        "content": [{
            "type": "tool-result",
            "toolName": "get_weather",
            "toolCallId": "call_1",
            "output": {"type": "text", "value": "sunny"},
        }],
    }]


def test_tool_message_resolves_name_from_earlier_tool_call():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_9", "type": "function",
             "function": {"name": "get_weather", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call_9", "content": "sunny"},
    ]
    converted = convert_messages(messages)
    assert converted[1]["content"][0]["toolName"] == "get_weather"
    assert converted[1]["content"][0]["toolCallId"] == "call_9"


def test_tool_message_defaults_to_unknown_and_omits_empty_id():
    assert convert_messages([{"role": "tool", "content": "boom"}]) == [{
        "role": "tool",
        "content": [{"type": "tool-result", "toolName": "unknown",
                     "output": {"type": "text", "value": "boom"}}],
    }]


def test_tool_message_marks_error_output():
    converted = convert_messages([
        {"role": "tool", "tool_call_id": "c", "name": "n", "content": "Error: file not found"},
    ])
    assert converted[0]["content"][0]["output"]["type"] == "error-text"


def test_tool_message_flattens_array_content():
    converted = convert_messages([
        {"role": "tool", "name": "n",
         "content": [{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}]},
    ])
    assert converted[0]["content"][0]["output"]["value"] == "part1part2"


def test_assistant_tool_calls_become_tool_call_parts():
    assert convert_messages([
        {"role": "assistant", "content": "let me check", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'}},
        ]},
    ]) == [{
        "role": "assistant",
        "content": [
            {"type": "text", "text": "let me check"},
            {"type": "tool-call", "toolCallId": "call_1", "toolName": "get_weather",
             "input": {"city": "Paris"}},
        ],
    }]


def test_assistant_tool_call_is_not_duplicated():
    messages = [
        {"role": "assistant",
         "content": [{"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"a": 1}}],
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "get_weather", "arguments": '{"a": 1}'}}]},
    ]
    parts = convert_messages(messages)[0]["content"]
    assert len(parts) == 1
    assert parts[0] == {"type": "tool-call", "toolCallId": "call_1", "toolName": "get_weather",
                        "input": {"a": 1}}


def test_assistant_tool_call_accepts_alternate_id_keys():
    parts = parse_content([{"type": "tool-call", "toolCallId": "c2", "toolName": "n", "input": {}}], {})
    assert parts[0]["toolCallId"] == "c2"


def test_malformed_tool_arguments_are_wrapped():
    assert parse_tool_input("{not json") == {"arguments": "{not json"}
    parts = parse_content([{"type": "tool_use", "id": "c", "name": "n", "arguments": "{not json"}], {})
    assert parts[0]["input"] == {"arguments": "{not json"}


def test_empty_tool_arguments_become_empty_object():
    assert parse_tool_input("") == {}
    parts = parse_content([{"type": "tool_use", "id": "c", "name": "n", "arguments": ""}], {})
    assert parts[0]["input"] == {}


def test_convert_tools_function_shape():
    tool = {"type": "function", "function": {
        "name": "get_weather",
        "description": "Look up weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    }}
    assert convert_tools([tool]) == [{
        "name": "get_weather",
        "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        "description": "Look up weather",
    }]


def test_convert_tools_defaults_schema():
    assert convert_tools([{"type": "function", "function": {"name": "n"}}])[0]["input_schema"] == {
        "type": "object",
        "properties": {},
    }


def test_convert_tools_non_function_passes_through():
    tool = {"type": "custom", "foo": 1}
    assert convert_tools([tool]) == [tool]


def test_convert_tools_drops_nameless_functions():
    assert convert_tools([{"type": "function", "function": {"name": "", "parameters": {}}}]) == []
    assert convert_tools([{"type": "function"}]) == []


def test_convert_tools_empty_input_is_empty_list():
    assert convert_tools([]) == []
    assert convert_tools(None) == []


def test_content_to_string_handles_scalar_and_dict():
    assert content_to_string(None) == ""
    assert content_to_string("hi") == "hi"
    # A bare mapping is serialised whole; only a list of parts gets flattened.
    assert content_to_string({"type": "text", "text": "x"}) == '{"type":"text","text":"x"}'
    assert content_to_string(42) == "42"
    assert content_to_string([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"

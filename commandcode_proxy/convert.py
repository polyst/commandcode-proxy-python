"""OpenAI -> CommandCode message and tool conversion.

Field names mirror internal/proxy/convert.go (``toolCallId``, ``toolName``,
``output: {type, value}``, the ``Error:`` -> ``error-text`` tag), but two things
are deliberate departures from it:

  * Images are sent as real upstream ``{type: "image", image, mimeType}`` parts
    instead of the Go proxy's ``[Image URL: ...]`` text stub, so vision models
    actually see pixels rather than a URL string.
  * ``thinking`` / ``reasoning`` parts are preserved as ``{type: "reasoning"}``
    parts, which is how the upstream reads thinking back in.
"""

from __future__ import annotations

import json
from typing import Any

_TEXT_KEYS = (
    "text",
    "content",
    "output_text",
    "input_text",
    "refusal",
    "thinking",
    "reasoning",
    "redacted_thinking",
)
_TEXT_PART_TYPES = {
    "text",
    "input_text",
    "output_text",
    "refusal",
    "document",
    "search_result",
}
_REASONING_PART_TYPES = {"thinking", "reasoning", "redacted_thinking"}
_IMAGE_PART_TYPES = {"image_url", "input_image", "image"}
_ERROR_PREFIX = "Error:"
_EMPTY_SCHEMA = {"type": "object", "properties": {}}


def _dump(value: Any) -> str:
    """Compact JSON, matching Go's json.Marshal shape closely enough."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _str(mapping: dict[str, Any], *keys: str) -> str:
    """First string-valued key, else ''."""
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str):
            return value
    return ""


def _image_part(part: dict[str, Any]) -> dict[str, Any] | None:
    """Map an OpenAI image part to the upstream {type, image, mimeType} part."""
    if isinstance(part.get("image_url"), dict):
        url = part["image_url"].get("url")
    elif isinstance(part.get("image_url"), str):
        url = part["image_url"]
    elif isinstance(part.get("image"), str):
        url = part["image"]
    else:
        return None
    if not isinstance(url, str) or not url:
        return None

    mime_type = ""
    if url.startswith("data:") and ";" in url:
        mime_type = url[len("data:"):].split(";", 1)[0]
    image: dict[str, Any] = {"type": "image", "image": url}
    if mime_type:
        image["mimeType"] = mime_type
    return image


def _message(msg: Any) -> dict[str, Any] | None:
    return msg if isinstance(msg, dict) else None


def content_part_to_string(part: Any) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part
    if isinstance(part, list):
        return "".join(content_part_to_string(item) for item in part if isinstance(item, dict))
    if isinstance(part, dict):
        for key in _TEXT_KEYS:
            value = part.get(key)
            if isinstance(value, str):
                return value
        image = part.get("image_url")
        if isinstance(image, dict):
            url = image.get("url")
            if isinstance(url, str):
                return "[Image URL: " + url + "]"
        elif isinstance(image, str):
            return "[Image URL: " + image + "]"
        return _dump(part)
    return str(part)


def content_to_string(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(content_part_to_string(part) for part in content if isinstance(part, dict))
    return _dump(content)


def parse_tool_input(arguments: Any) -> Any:
    if not isinstance(arguments, str) or arguments == "":
        return {}
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return {"arguments": arguments}


def _output_type(text: str) -> str:
    return "error-text" if text.startswith(_ERROR_PREFIX) else "text"


def parse_content(content: Any, tool_names: dict[str, str]) -> list[dict[str, Any]] | None:
    """Turn one OpenAI message body into CommandCode content parts.

    Returns None for an absent or empty body; CommandCode serialises that as
    ``"content": null``.
    """
    if content is None:
        return None
    if isinstance(content, str):
        if content == "":
            return None
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return [{"type": "text", "text": content_to_string(content)}]

    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type") if isinstance(part.get("type"), str) else ""

        if part_type in _TEXT_PART_TYPES:
            text = content_part_to_string(part)
            if text:
                parts.append({"type": "text", "text": text})
            continue

        if part_type in _REASONING_PART_TYPES:
            text = content_part_to_string(part)
            if text:
                parts.append({"type": "reasoning", "text": text})
            continue

        if part_type in _IMAGE_PART_TYPES:
            image = _image_part(part)
            if image is not None:
                parts.append(image)
            continue

        if part_type in ("tool_use", "tool-call"):
            call_id = _str(part, "id", "toolCallId", "tool_use_id")
            name = _str(part, "name", "toolName")
            if call_id and name:
                tool_names[call_id] = name
            if part.get("input") is not None:
                tool_input = part["input"]
            else:
                # Go passes the raw string through here; a string argument is
                # the OpenAI spelling of the same field, so decode it.
                arguments = part.get("arguments")
                tool_input = parse_tool_input(arguments) if isinstance(arguments, str) else arguments
            entry: dict[str, Any] = {"type": "tool-call"}
            if call_id:
                entry["toolCallId"] = call_id
            if name:
                entry["toolName"] = name
            if tool_input is not None:
                entry["input"] = tool_input
            parts.append(entry)
            continue

        if part_type in ("tool_result", "tool-result"):
            result_id = _str(part, "tool_use_id", "toolCallId")
            tool_name = _str(part, "toolName") or tool_names.get(result_id, "") or "unknown"
            value = content_part_to_string(part.get("content"))
            if not value:
                value = content_part_to_string(part.get("output"))
            entry = {
                "type": "tool-result",
                "toolName": tool_name,
                "output": {"type": _output_type(value), "value": value},
            }
            if result_id:
                entry["toolCallId"] = result_id
            parts.append(entry)
            continue

    return parts


def convert_messages(messages: list[Any]) -> list[dict[str, Any]]:
    cc_messages: list[dict[str, Any]] = []
    tool_names: dict[str, str] = {}

    for raw in messages or []:
        msg = _message(raw)
        if msg is None:
            continue

        for tc in msg.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            call_id = tc.get("id") if isinstance(tc.get("id"), str) else ""
            function = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = function.get("name") if isinstance(function.get("name"), str) else ""
            if call_id and name:
                tool_names[call_id] = name

        role = msg.get("role", "")

        if role == "tool":
            call_id = _str(msg, "tool_call_id")
            name = _str(msg, "name") or tool_names.get(call_id, "") or "unknown"
            text = content_to_string(msg.get("content"))
            entry: dict[str, Any] = {
                "type": "tool-result",
                "toolName": name,
                "output": {"type": _output_type(text), "value": text},
            }
            if call_id:
                entry["toolCallId"] = call_id
            cc_messages.append({"role": "tool", "content": [entry]})
            continue

        if role == "assistant" and msg.get("tool_calls"):
            parts = parse_content(msg.get("content"), tool_names) or []
            added = {p["toolCallId"] for p in parts if p.get("type") == "tool-call" and p.get("toolCallId")}
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                call_id = tc.get("id") if isinstance(tc.get("id"), str) else ""
                if call_id and call_id in added:
                    continue
                function = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = function.get("name") if isinstance(function.get("name"), str) else ""
                entry: dict[str, Any] = {"type": "tool-call"}
                if call_id:
                    entry["toolCallId"] = call_id
                if name:
                    entry["toolName"] = name
                tool_input = parse_tool_input(function.get("arguments", ""))
                if tool_input is not None:
                    entry["input"] = tool_input
                parts.append(entry)
                added.add(call_id)
            cc_messages.append({"role": role, "content": parts})
            continue

        cc_messages.append({"role": role, "content": parse_content(msg.get("content"), tool_names)})

    return cc_messages


def convert_tools(tools: list[Any]) -> list[Any]:
    if not tools:
        return []

    converted: list[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue

        if tool.get("type") != "function":
            converted.append(tool)
            continue

        function = tool.get("function")
        if not isinstance(function, dict):
            continue

        name = function.get("name") if isinstance(function.get("name"), str) else ""
        if not name:
            continue

        schema = function.get("parameters")
        if not isinstance(schema, dict):
            schema = dict(_EMPTY_SCHEMA)

        cc_tool: dict[str, Any] = {"name": name, "input_schema": schema}
        description = function.get("description")
        if isinstance(description, str) and description:
            cc_tool["description"] = description
        converted.append(cc_tool)

    return converted


def extract_system(messages: list[Any]) -> tuple[str, list[Any]]:
    """Split messages into (joined system prompt, remaining messages)."""
    chunks: list[str] = []
    rest: list[Any] = []
    for raw in messages or []:
        msg = _message(raw)
        if msg is None:
            continue
        if msg.get("role") == "system":
            chunks.append(content_to_string(msg.get("content")))
        else:
            rest.append(msg)
    return "\n".join(chunks), rest

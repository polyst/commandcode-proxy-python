"""HTTP surface: /health, /v1/models, /v1/chat/completions."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from time import time
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .config import Config
from .models import ModelRegistry
from .responses import (
    RequestStats,
    UpstreamStreamError,
    collect_completion,
    instrument,
    iter_events,
    openai_error,
    stream_chunks,
    wants_stream_usage,
)
from .upstream import (
    UPSTREAM_TIMEOUT,
    VersionCache,
    build_envelope,
    map_upstream_status,
    new_request_id,
    upstream_headers,
)

log = logging.getLogger("commandcode_proxy.api")

DEBUG_LOG_LIMIT = 20000


def _truncate(text: str) -> str:
    if len(text) <= DEBUG_LOG_LIMIT:
        return text
    return text[:DEBUG_LOG_LIMIT] + f"... [truncated {len(text) - DEBUG_LOG_LIMIT} bytes]"


def ascii_only(text: object) -> str:
    """Keep log lines ASCII. cmd.exe consoles render GBK, so anything else shows as junk."""
    return str(text).encode("ascii", "replace").decode("ascii")


def usage_summary(usage: dict[str, Any] | None) -> str:
    """One compact token breakdown for the access log."""
    if not isinstance(usage, dict):
        return "tokens=?"
    details = usage.get("completion_tokens_details")
    details = details if isinstance(details, dict) else {}
    bits = [
        f"in={usage.get('prompt_tokens', 0)}",
        f"out={usage.get('completion_tokens', 0)}",
        f"total={usage.get('total_tokens', 0)}",
    ]
    if details:
        bits.append(f"reason={details.get('reasoning_tokens', '?')}")
        bits.append(f"text={details.get('text_tokens', '?')}")
    return " ".join(bits)


def access_line(stats: RequestStats, requested: str, mapped: str, mode: str, status: str) -> str:
    """One line per chat request: model mapping, tokens, timing, upstream events."""
    ttft = f" ttft={stats.ttft:.2f}s" if stats.ttft is not None else ""
    finish = f" finish={stats.finish_reason}" if stats.finish_reason else ""
    tools = f" tools={stats.tool_calls}" if stats.tool_calls else ""
    return (f"[{mode}] {ascii_only(requested)} -> {ascii_only(mapped)}  HTTP {status}  "
            f"{usage_summary(stats.usage)}  {stats.elapsed:.2f}s{ttft}  "
            f"events={stats.events}{finish}{tools}")


def resolve_api_key(authorization: str, default_key: str) -> str | None:
    """Decide which key authenticates the upstream request.

    With a configured default key the proxy owns the credential and always uses
    it, treating the client's Authorization header as a local gate only. This is
    what makes agent clients usable: they force you to type something in the API
    key box, and forwarding that placeholder upstream yields a 401.

    Without a default key the proxy authenticates nothing itself, so the client
    must send a real upstream key and it is forwarded verbatim - one proxy per
    account, the Go reference behaviour.
    """
    if default_key:
        return default_key
    supplied = authorization[7:] if authorization.startswith("Bearer ") else authorization
    return supplied.strip() or None


def json_error(status: int, message: str, error_type: str) -> JSONResponse:
    return JSONResponse(openai_error(message, error_type), status_code=status)


def create_app(
    config: Config | None = None,
    client: httpx.AsyncClient | None = None,
    registry: ModelRegistry | None = None,
    warm_version: bool = True,
) -> FastAPI:
    """Build the FastAPI app.

    ``client`` and ``registry`` are injectable so tests can drive the whole
    endpoint against an httpx MockTransport.
    """
    config = config or Config()
    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, follow_redirects=True)
    model_registry = registry or ModelRegistry(config.models_path)
    version_cache = VersionCache(config.command_code_version)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if warm_version:
            await version_cache.get(http_client)
        yield
        if owns_client:
            await http_client.aclose()

    app = FastAPI(title="CommandCode Proxy", version=__version__, lifespan=lifespan)
    app.state.http_client = http_client
    app.state.model_registry = model_registry
    app.state.version_cache = version_cache

    @app.middleware("http")
    async def openai_error_shape(request: Request, call_next):
        """Rewrite 404/405 into the OpenAI error envelope.

        Starlette raises these inside ExceptionMiddleware, so a normal
        exception_handler never sees them.
        """
        response = await call_next(request)
        if response.status_code not in (404, 405):
            return response

        async for _ in response.body_iterator:
            pass

        message = "Method not allowed" if response.status_code == 405 else "Not Found"
        return JSONResponse(openai_error(message, "invalid_request_error"),
                            status_code=response.status_code)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        table = model_registry.reload_if_changed()
        data = []
        for model in table.model_list():
            entry = {"id": model["id"], "object": "model", "created": 0,
                     "owned_by": model["owned_by"]}
            if model.get("name"):
                entry["name"] = model["name"]
            data.append(entry)
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions")
    @app.post("/chat/completions")
    async def chat_completions(request: Request) -> Response:
        api_key = resolve_api_key(request.headers.get("authorization", ""), config.api_key)
        if not api_key:
            return json_error(401, "API key required. Set Authorization header.",
                              "authentication_error")

        raw = await request.body()
        log.debug("[DEBUG] client request body: %s", _truncate(raw.decode("utf-8", "replace")))

        try:
            payload = json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return json_error(400, f"Invalid JSON: {exc}", "invalid_request_error")
        if not isinstance(payload, dict):
            return json_error(400, "Invalid JSON: request body must be an object",
                              "invalid_request_error")

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return json_error(400, "messages array is required", "invalid_request_error")

        table = model_registry.reload_if_changed()
        requested = payload.get("model") if isinstance(payload.get("model"), str) else ""
        model = table.map_model(requested)
        stats = RequestStats()
        envelope = build_envelope(payload, model)
        request_id = new_request_id()
        created = int(time())

        log.debug("[DEBUG] commandcode request body: %s", _truncate(
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))))

        version = await version_cache.get(http_client)
        headers = upstream_headers(api_key, version, config.zdr)

        try:
            upstream = await http_client.send(
                http_client.build_request(
                    "POST",
                    config.upstream_url(),
                    content=json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                    headers=headers,
                ),
                stream=True,
            )
        except httpx.HTTPError as exc:
            log.error("[%s] model=%s upstream unreachable after %.2fs: %s",
                      requested, model, stats.elapsed, ascii_only(exc))
            return json_error(502, str(exc), "api_error")

        if upstream.status_code != 200:
            detail = (await upstream.aread()).decode("utf-8", "replace")
            await upstream.aclose()
            log.error("[%s] model=%s upstream HTTP %d after %.2fs: %s",
                      requested, model, upstream.status_code, stats.elapsed, ascii_only(detail))
            return json_error(map_upstream_status(upstream.status_code),
                              f"Upstream error: {detail}", "api_error")

        include_usage = wants_stream_usage(payload)

        if payload.get("stream") is True:
            events = instrument(iter_events(upstream.aiter_lines()), stats)

            async def generate():
                status = "200"
                try:
                    async for chunk in stream_chunks(events, request_id, model, created,
                                                     request.is_disconnected,
                                                     include_usage=include_usage):
                        yield chunk
                except (UpstreamStreamError, httpx.HTTPError) as exc:
                    # The 200 and content-type are already on the wire, so there is
                    # no error envelope to send - the client just sees a truncated
                    # SSE stream. Log it, because nothing else records this.
                    status = "stream-broken"
                    log.error("stream failed mid-response: %s", ascii_only(exc))
                finally:
                    await upstream.aclose()
                    log.info(access_line(stats, requested, model, "chat stream", status))

            return StreamingResponse(
                generate(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        try:
            body = await collect_completion(instrument(iter_events(upstream.aiter_lines()), stats),
                                            request_id, model, created)
        except (UpstreamStreamError, httpx.HTTPError) as exc:
            log.error("upstream stream error: %s", ascii_only(exc))
            return json_error(502, f"Upstream error: {exc}", "api_error")
        finally:
            await upstream.aclose()
        log.info(access_line(stats, requested, model, "chat", "200"))
        return JSONResponse(body)

    return app

"""CLI entry point.

Run directly:   python -m commandcode_proxy.main --api-key sk-...
Or install:     commandcode-proxy --api-key sk-...
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys

import uvicorn

from . import __version__
from .api import create_app
from .config import Config
from .models import SHIPPED_MODELS_FILE, ModelRegistry
from .upstream import DEFAULT_BASE_URL

# Config fields the CLI can override; order matches the parser flags above.
CLI_FIELDS = (
    "host",
    "port",
    "api_key",
    "base_url",
    "models_file",
    "command_code_version",
    "zdr",
    "debug",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="commandcode-proxy",
        description="OpenAI-compatible proxy for the CommandCode API",
    )
    parser.add_argument("--host", default=None, help="Host to bind to (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Port to run on (default: 55990)")
    parser.add_argument("--api-key", default=None,
                        help="Default CommandCode API key; a per-request Authorization header overrides it")
    parser.add_argument("--base-url", default=None,
                        help=f"Upstream base URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--models-file", default=None,
                        help=f"Path to the model mapping file (default: {SHIPPED_MODELS_FILE})")
    parser.add_argument("--command-code-version", default=None,
                        help="Pin x-command-code-version instead of querying the npm registry")
    parser.add_argument("--zdr", action="store_true", default=None,
                        help="Send x-cmd-zdr: 1 for zero data retention on every request")
    parser.add_argument("--debug", action="store_true", default=None,
                        help="Log full request and response bodies")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    return parser


def print_startup_info(config: Config, registry: ModelRegistry) -> None:
    table = registry.table
    models = [model["id"] for model in table.model_list()]
    key_mode = ("local - proxy's key used upstream; any client key accepted"
                if config.api_key else
                "passthrough - the client must send a real upstream key")

    print("")
    print("==============================================")
    print("  CommandCode Proxy Server")
    print("==============================================")
    print("")
    print(f"  Version:    {__version__}")
    print(f"  Listening:  http://{config.host}:{config.port}")
    print(f"  Upstream:   {config.upstream_url()}")
    print(f"  Models:     {len(models)} models, {len(table.aliases)} aliases")
    print(f"  Models file:{config.models_path}")
    print(f"  Key mode:   {key_mode}")
    print("")
    print("  Endpoints:")
    print("    POST /v1/chat/completions   POST /chat/completions")
    print("    GET  /v1/models             GET  /health")
    print("")
    print("  Models (full ids pass through; short aliases are also accepted):")
    # Fit as many columns as the console can hold; model ids run to 45 chars, so a
    # fixed 3-column layout wraps on a normal 80-column window.
    console_width = shutil.get_terminal_size((120, 24)).columns
    padding = max((len(item) for item in models), default=0) + 2
    columns = max(1, (console_width - 6) // padding)
    for start in range(0, len(models), columns):
        row = " ".join(item.ljust(padding) for item in models[start:start + columns]).rstrip()
        print(f"    {row}")
    print("")
    print("  Each chat request logs one line, for example:")
    print("    [chat] step3.5 -> stepfun/Step-3.5-Flash  HTTP 200  in=7574 out=74")
    print("           total=7648 reason=79 text=-5  4.55s ttft=1.81s  events=74 finish=stop")
    print("")
    print("  The models file is re-read when it changes - no restart needed.")
    print("  Start with --debug to also print full request bodies and every")
    print("  upstream event line.")
    print("")
    print("  Press Ctrl+C to stop")
    print("==============================================")
    # stdout is line-buffered on a console but block-buffered when piped or
    # redirected, which would leave the banner sitting unseen in a buffer.
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    config = Config.from_env(**{
        field: getattr(args, field) for field in CLI_FIELDS
    })

    registry = ModelRegistry(config.models_path)

    logging.basicConfig(
        level=logging.DEBUG if config.debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # httpx logs one INFO line per outbound request, which is pure noise here.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    print_startup_info(config, registry)

    # access_log=False: the per-request line in api.py already reports the model,
    # token counts and timing, so uvicorn's copy of the same request is noise.
    uvicorn.run(create_app(config, registry=registry),
                host=config.host, port=config.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

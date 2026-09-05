import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from commandcode_proxy.api import create_app  # noqa: E402
from commandcode_proxy.config import Config  # noqa: E402

API_KEY = "sk-test-command-code-key"
PINNED_VERSION = "0.1.2-test"


def ndjson(*events: str) -> bytes:
    """Upstream replies with one JSON event per line, not real SSE."""
    return ("\n".join(events) + "\n").encode("utf-8")


@pytest.fixture
def make_app():
    """Build a TestClient whose upstream is an httpx MockTransport.

    Returns ``(client, seen_requests)`` so a test can assert on exactly what
    would have been sent to api.commandcode.ai.
    """

    def _make(handler, **config_kwargs):
        seen: list[httpx.Request] = []

        def recording_handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        transport = httpx.MockTransport(recording_handler)
        config = config_kwargs.pop("config", None) or Config(**config_kwargs)
        app = create_app(config, client=httpx.AsyncClient(transport=transport), warm_version=False)
        return TestClient(app), seen

    return _make

"""Model name mapping.

Two things live here:

  * ``map_model`` — turns a client-supplied name into the upstream model id.
    Full ids always pass through unchanged, so a new upstream model needs no
    code change; only short aliases need an entry.
  * the model list served by ``GET /v1/models``.

Both come from one file: ``models.json`` next to the project root (override with
``CC_PROXY_MODELS_FILE``). The file is the source of truth. Edit it and the next
request picks the change up — no restart, the mtime is checked per request.

File format::

    {
      "aliases": { "short-name": "Vendor/Model-Id" },
      "models": [
        "Vendor/Model-Id",
        { "id": "zai-org/GLM-5.1", "owned_by": "zhipuai", "name": "GLM-5.1" }
      ]
    }

``owned_by`` is optional (derived from the id prefix when omitted) and ``name``
is an optional display name. ``models`` may be left out entirely, in which case
the list is derived from the alias targets.

There is deliberately no second copy of the catalog in this module: the
fallback is seeded from ``models.json`` at import time, so the two cannot drift
apart. If that read also fails, aliases resolve to identity — which is already
safe, since unknown names are passed through to the upstream untouched.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("commandcode_proxy.models")

SHIPPED_MODELS_FILE = str(Path(__file__).resolve().parent.parent / "models.json")


def owned_by_of(model_id: str) -> str:
    """Derive ``owned_by`` from the vendor segment of an id."""
    prefix = model_id.split("/", 1)[0]
    return prefix.lower() if prefix else ""


@dataclass(frozen=True)
class ModelTable:
    """An immutable snapshot of the alias table and the model list."""

    aliases: dict[str, str] = field(default_factory=dict)
    models: list[dict[str, str]] = field(default_factory=list)

    def map_model(self, name: str) -> str:
        if not name:
            return name
        return self.aliases.get(name.strip().lower(), name)

    def model_list(self) -> list[dict[str, str]]:
        return list(self.models)


def build_table(data: dict[str, Any]) -> ModelTable:
    """Validate and normalise raw config-file content into a ModelTable."""
    aliases_raw = data.get("aliases")
    if not isinstance(aliases_raw, dict):
        raise ValueError("missing or invalid 'aliases' object")
    aliases: dict[str, str] = {}
    for key, target in aliases_raw.items():
        if not isinstance(key, str) or not isinstance(target, str):
            raise ValueError(f"alias {key!r} is not a string mapping to a string")
        key = key.strip()
        if key:
            aliases[key.lower()] = target
    if not aliases:
        raise ValueError("'aliases' is empty")

    models_raw = data.get("models")
    models: list[dict[str, str]]
    if models_raw is None:
        models = []
        for target in sorted({value for value in aliases.values() if "/" in value}):
            models.append({"id": target, "owned_by": owned_by_of(target), "name": ""})
    elif isinstance(models_raw, list):
        models = []
        for entry in models_raw:
            if isinstance(entry, str):
                model_id, owner, name = entry, owned_by_of(entry), ""
            elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
                model_id = entry["id"]
                owner = entry.get("owned_by") or owned_by_of(model_id)
                name = entry.get("name") if isinstance(entry.get("name"), str) else ""
            else:
                raise ValueError(f"invalid model entry: {entry!r}")
            models.append({"id": model_id, "owned_by": owner, "name": name})
    else:
        raise ValueError("'models' must be a list")

    return ModelTable(aliases=aliases, models=models)


def seed_table() -> ModelTable:
    """Seed the fallback from models.json so no second copy of it exists.

    Returns an empty table (identity mapping) if even that read fails.
    """
    try:
        data = json.loads(Path(SHIPPED_MODELS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        log.warning("could not seed the model table from %s", SHIPPED_MODELS_FILE)
        return ModelTable()
    try:
        return build_table(data)
    except ValueError as exc:
        log.warning("invalid model table in %s: %s", SHIPPED_MODELS_FILE, exc)
        return ModelTable()


class ModelRegistry:
    """Holds the current ModelTable and reloads it when the file changes."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self.path = Path(path) if path else Path(SHIPPED_MODELS_FILE)
        self._lock = threading.Lock()
        self._table = seed_table()
        self._mtime: float | None = None
        self._last_error: str | None = None
        self.reload()

    def reload(self) -> ModelTable:
        """Force a reload; keeps the previous table on any failure."""
        try:
            stat = self.path.stat()
            table = build_table(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("could not load model table from %s: %s", self.path, exc)
            with self._lock:
                return self._table
        with self._lock:
            self._table = table
            self._mtime = stat.st_mtime
            self._last_error = None
            log.info("loaded %d aliases and %d models from %s",
                     len(table.aliases), len(table.models), self.path)
            return table

    def reload_if_changed(self) -> ModelTable:
        """Cheap per-request check: only re-reads when mtime moved."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return self._table
        with self._lock:
            if mtime == self._mtime and self._last_error is None:
                return self._table
        return self.reload()

    @property
    def table(self) -> ModelTable:
        with self._lock:
            return self._table

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

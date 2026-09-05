"""Register this proxy's models with the ZCode client.

ZCode keeps its provider table in ~/.zcode/v2/config.json, shaped like:

    { "provider": { "<provider-id>": { "name", "kind", "source",
                                       "options": {"baseURL", "apiKey", "apiKeyRequired"},
                                       "models": { "<model-id>": { "limit", "modalities" } } } } }

This script adds (or refreshes) one entry for this proxy. It is idempotent, so
re-run it whenever models.json changes.

It reads ZCode's own bundled model catalog to fill in real context/output limits
where the model is known there; otherwise it uses the defaults below. Context and
output limits matter - they drive when ZCode compacts a conversation, so guessing
them wrong either compacts far too early or overflows the window.

Usage:
    python install_to_zcode.py              # write to ~/.zcode/v2/config.json
    python install_to_zcode.py --dry-run    # print what would be written
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

PROVIDER_ID = "commandcode"
PROVIDER_NAME = "CommandCode (local proxy)"
BASE_URL = "http://127.0.0.1:55990/v1"
PLACEHOLDER_KEY = "commandcode"

# Fallbacks for models ZCode's catalog knows nothing about. Conservative on
# purpose: a limit that is too small only costs an earlier compaction.
DEFAULT_CONTEXT = 262144
DEFAULT_OUTPUT = 16384
# Hard cap the upstream enforces on params.max_tokens (see
# commandcode_proxy.upstream.UPSTREAM_MAX_TOKENS).
UPSTREAM_MAX_OUTPUT = 200000

CONFIG = Path.home() / ".zcode" / "v2" / "config.json"
CATALOG = Path(r"C:\gongju\ZCode\resources\model-providers") / (
    "models_catalog_china_llm_zcode_2026-06-03.json"
)


def catalog_limits() -> dict[str, tuple[int, int, list[str]]]:
    """Map bare model id -> (context, output, input modalities) from ZCode's catalog."""
    if not CATALOG.is_file():
        return {}
    table: dict[str, tuple[int, int, list[str]]] = {}
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    for provider in catalog.get("providers", []):
        for model in provider.get("models", []):
            context = model.get("contextWindow") or DEFAULT_CONTEXT
            output = model.get("maxOutputTokens") or DEFAULT_OUTPUT
            inputs = model.get("modalities", {}).get("input") or ["text"]
            table[model["id"].lower()] = (context, output, inputs)
    return table


def lookup(catalog: dict[str, tuple[int, int, list[str]]], model_id: str) -> tuple[int, int, list[str]]:
    """Try the full id, then the part after the last '/', then with ':' stripped."""
    tail = model_id.rsplit("/", 1)[-1]
    for candidate in (model_id, tail, tail.replace(":", "-")):
        if candidate.lower() in catalog:
            return catalog[candidate.lower()]
    return DEFAULT_CONTEXT, DEFAULT_OUTPUT, ["text"]


def build_entry(models_json: Path, base_url: str) -> dict:
    data = json.loads(models_json.read_text(encoding="utf-8"))
    catalog = catalog_limits()
    models: dict[str, dict] = {}
    for entry in data["models"]:
        model_id = entry["id"] if isinstance(entry, dict) else entry
        context, output, inputs = lookup(catalog, model_id)
        # The upstream rejects params.max_tokens above 200000, so an output limit
        # ZCode cannot honour is worse than a smaller one.
        output = min(output, UPSTREAM_MAX_OUTPUT)
        # Only the *-vision model in this catalog actually accepts images; the
        # upstream tells a text-only model it got no image. Keep any other
        # modalities the catalog records, such as Kimi's video input.
        extras = [i for i in inputs if i not in ("text", "image")]
        inputs = ["text"] + (["image"] if "vision" in model_id.lower() else []) + extras
        models[model_id] = {
            "limit": {"context": context, "output": output},
            "modalities": {"input": inputs, "output": ["text"]},
            "zcode": {"modalitiesConfigured": True},
        }
    return {
        "name": PROVIDER_NAME,
        "kind": "openai-compatible",
        "source": "custom",
        "options": {
            "baseURL": base_url,
            "apiKey": PLACEHOLDER_KEY,
            "apiKeyRequired": True,
        },
        "models": models,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print the entry instead of writing")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--models-file", default=str(Path(__file__).with_name("models.json")))
    args = parser.parse_args()

    entry = build_entry(Path(args.models_file), args.base_url)
    print(f"provider: {PROVIDER_ID}  models: {len(entry['models'])}  base_url: {args.base_url}")
    if args.dry_run:
        print(json.dumps(entry, indent=2, ensure_ascii=False)[:2000])
        return 0

    if not CONFIG.is_file():
        print(f"error: {CONFIG} not found - is ZCode installed?", file=sys.stderr)
        return 1

    backup = CONFIG.with_name(f"{CONFIG.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(CONFIG, backup)
    print(f"backed up {CONFIG.name} -> {backup.name}")

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    providers = config.setdefault("provider", {})
    replaced = PROVIDER_ID in providers
    providers[PROVIDER_ID] = entry
    CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    check = json.loads(CONFIG.read_text(encoding="utf-8"))
    assert PROVIDER_ID in check["provider"]
    assert len(check["provider"][PROVIDER_ID]["models"]) == len(entry["models"])
    print(f"wrote {PROVIDER_ID} into {CONFIG} ({'replaced' if replaced else 'added'})")
    print("\nRestart ZCode, then pick the model from the model selector as")
    print(f"  {PROVIDER_ID}/<model-id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

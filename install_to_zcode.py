"""Register this proxy's models with the ZCode client.

ZCode keeps its provider table in ~/.zcode/v2/config.json, shaped like:

    { "provider": { "<provider-id>": { "name", "kind", "source",
                                       "options": {"baseURL", "apiKey", "apiKeyRequired"},
                                       "modelDisplayNames": {"<id>": "<label>"},
                                       "models": {"<model-id>": {"name", "reasoning",
                                                                 "limit", "modalities"}} } } }

This script adds (or refreshes) one entry for this proxy. It is idempotent, so
re-run it whenever models.json changes.

Each model gets a context window, an output limit, modalities, a display name
and a reasoning switch:

- Context comes from models.json's contextWindow (vendor-published, so it beats
  any catalog guess), else the fallback below. It drives when ZCode compacts a
  conversation.
- Output comes from OUTPUT_OVERRIDES, else ZCode's bundled catalog if one
  ships, else the fallback. The upstream hard-rejects params.max_tokens above
  UPSTREAM_MAX_OUTPUT, so nothing is ever written higher than that.
- Name comes from models.json's name and is written twice: per model (`name`)
  and provider-wide (`modelDisplayNames`). Both are additive keys a strict
  reader drops, so whichever ZCode honours is the one that ends up showing.
- Reasoning is written only for models models.json marks as reasoning, and the
  level list comes from its efforts field (the command-code CLI's own
  per-model reasoningEfforts). It is intentionally cosmetic: /alpha/generate
  has no reasoning-effort knob, so the switch cannot affect anything upstream.
  It is present so ZCode shows the model as reasoning rather than treating it
  as a plain model.

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

# Fallbacks for models nothing else says anything about. Conservative on
# purpose: a limit that is too small only costs an earlier compaction.
DEFAULT_CONTEXT = 262144

# Reasoning models spend max_tokens on thinking before they spend it on the
# answer, so a small ceiling can produce a finished-but-empty reply. 65536 is
# the value ZCode itself ships for a 1M-context model, which is the floor it
# appears to use.
DEFAULT_OUTPUT = 65536

# Hard cap the upstream enforces on params.max_tokens (see
# commandcode_proxy.upstream.UPSTREAM_MAX_TOKENS): 200000 is accepted, 200001
# rejects with BAD_REQUEST. Verified on three unrelated models.
UPSTREAM_MAX_OUTPUT = 200000

# Output limits that are better than the fallback. Entries above the upstream
# cap are recorded as the model's real ceiling and clamped at build time, so
# the table stays honest about what the model can do.
OUTPUT_OVERRIDES = {
    "deepseek/deepseek-v4-pro": 384000,
    "deepseek/deepseek-v4-flash": 384000,
    "deepseek/deepseek-v4-flash-vision-exp": 384000,
    "deepseek/deepseek-v4-flash-fast": 384000,
    "deepseek/deepseek-v4.1-flash": 384000,
    "zai-org/GLM-5.2": 128000,
    "moonshotai/Kimi-K3": 131072,
}

# Fallback level list for a model models.json marks reasoning without giving
# levels. The CLI's per-model reasoningEfforts wins when present.
DEFAULT_EFFORTS = ["low", "high", "max"]

CONFIG = Path.home() / ".zcode" / "v2" / "config.json"
# ZCode used to ship a date-stamped catalog JSON here. That filename changes on
# every update, and the file is gone in the 2026-09 rebuild (app.asar now only
# carries the Zod schemas zcode.model-providers.v1/v2), so it is globbed rather
# than hard-coded. With no catalog, models.json's vendor-published contextWindow
# still wins and only the output limit falls back to the conservative default.
CATALOG_DIR = Path(r"C:\gongju\ZCode\resources\model-providers")


def find_catalog() -> Path | None:
    """Newest ZCode catalog file, or None when ZCode ships none at all."""
    if not CATALOG_DIR.is_dir():
        return None
    matches = sorted(CATALOG_DIR.glob("models_catalog*.json"))
    return matches[-1] if matches else None


def catalog_limits() -> dict[str, tuple[int, int, list[str]]]:
    """Map bare model id -> (context, output, input modalities) from ZCode's catalog."""
    catalog_path = find_catalog()
    if catalog_path is None:
        return {}
    table: dict[str, tuple[int, int, list[str]]] = {}
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
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
    display_names: dict[str, str] = {}
    for entry in data["models"]:
        model_id = entry["id"] if isinstance(entry, dict) else entry
        label = entry.get("name") if isinstance(entry, dict) else None
        context, output, inputs = lookup(catalog, model_id)
        # A contextWindow in models.json is vendor-published and beats both the
        # ZCode catalog guess and DEFAULT_CONTEXT. Without it, gpt-6-luna would
        # get 262144 while the upstream actually gives it 1050000.
        published = entry.get("contextWindow") if isinstance(entry, dict) else None
        if isinstance(published, int) and published > 0:
            context = published
        # Per-model override beats the catalog, which beats the fallback. The
        # upstream rejects params.max_tokens above UPSTREAM_MAX_OUTPUT, so an
        # output limit ZCode cannot honour is worse than a smaller one.
        output = min(OUTPUT_OVERRIDES.get(model_id, output), UPSTREAM_MAX_OUTPUT)
        # Only the *-vision model in this catalog actually accepts images; the
        # upstream tells a text-only model it got no image. Keep any other
        # modalities the catalog records, such as Kimi's video input.
        published_inputs = entry.get("inputs") if isinstance(entry, dict) else None
        if isinstance(published_inputs, list) and all(
                isinstance(i, str) for i in published_inputs):
            # models.json records the command-code CLI's own inputModalities, so
            # this replaces the old "is 'vision' in the id" guess - which missed
            # every multimodal model whose name omits the word.
            extras = [i for i in published_inputs if i not in ("text", "image")]
            inputs = ["text"] + (["image"] if "image" in published_inputs else []) + extras
        else:
            extras = [i for i in inputs if i not in ("text", "image")]
            inputs = ["text"] + extras
        reasoning = None
        if entry.get("reasoning"):
            efforts = entry.get("efforts") or DEFAULT_EFFORTS
            reasoning = {"enabled": True, "variants": list(efforts),
                         "defaultVariant": list(efforts)[-1]}
        model = {
            "limit": {"context": context, "output": output},
            "modalities": {"input": inputs, "output": ["text"]},
            "zcode": {"modalitiesConfigured": True},
        }
        if reasoning is not None:
            model["reasoning"] = reasoning
        if isinstance(label, str) and label:
            model["name"] = label
            display_names[model_id] = label
        models[model_id] = model
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
        "modelDisplayNames": display_names,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print the entry instead of writing")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--models-file", default=str(Path(__file__).with_name("models.json")))
    parser.add_argument("--if-changed", action="store_true",
                        help="Skip the write (and the backup) when the config already holds this entry")
    args = parser.parse_args()

    entry = build_entry(Path(args.models_file), args.base_url)
    print(f"provider: {PROVIDER_ID}  models: {len(entry['models'])}  base_url: {args.base_url}")
    print(f"named: {len(entry['modelDisplayNames'])}  "
          f"reasoning: {sum(1 for m in entry['models'].values() if 'reasoning' in m)}  "
          f"output overrides: {len(OUTPUT_OVERRIDES)}  "
          f"upstream cap: {UPSTREAM_MAX_OUTPUT}")
    if find_catalog() is None:
        print(f"note: no ZCode model catalog under {CATALOG_DIR}; contextWindow comes "
              "from models.json, the output limit falls back to the conservative default")
    if args.dry_run:
        print(json.dumps(entry, indent=2, ensure_ascii=False)[:2000])
        return 0

    if not CONFIG.is_file():
        print(f"error: {CONFIG} not found - is ZCode installed?", file=sys.stderr)
        return 1

    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    if args.if_changed and config.get("provider", {}).get(PROVIDER_ID) == entry:
        print("config already holds this entry - nothing written")
        return 0

    backup = CONFIG.with_name(f"{CONFIG.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(CONFIG, backup)
    print(f"backed up {CONFIG.name} -> {backup.name}")

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

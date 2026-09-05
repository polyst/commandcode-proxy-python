"""Model mapping tests.

Model ids must come from the command-code CLI bundle's canonicalId
table, not from its flat display arrays: those also contain the
provider-internal slugs, and tencent/hy3 (a slug) 403s upstream while
the real id tencent/hy3-paid works. gpt-5.6-luna is canonical with no
vendor segment; the upstream maps it to openai/gpt-5.6-luna itself.
"""

import json
import os
from pathlib import Path
import time

import pytest

from commandcode_proxy.models import ModelRegistry, ModelTable, build_table, seed_table

CASES = [
    # Free-tier deals.
    ("laguna-s-2.1-free", "poolside/laguna-s-2.1-free"),
    ("laguna-s-2.1", "poolside/laguna-s-2.1-free"),
    ("LAGUNA", "poolside/laguna-s-2.1-free"),
    ("longcat-2.0", "meituan/LongCat-2.0:free"),
    ("longcat", "meituan/LongCat-2.0:free"),
    # Tencent.
    ("hy4-preview", "tencent/hy4-preview"),
    ("hy4", "tencent/hy4-preview"),
    ("hy3", "tencent/hy3-paid"),
    # Moonshot - bare aliases point at the newest model of the family.
    ("kimi-k3", "moonshotai/Kimi-K3"),
    ("kimi3", "moonshotai/Kimi-K3"),
    ("KIMI", "moonshotai/Kimi-K3"),
    ("kimi-k2.7-code", "moonshotai/Kimi-K2.7-Code"),
    ("kimi-code", "moonshotai/Kimi-K2.7-Code"),
    ("kimi-k2.7-code-highspeed", "moonshotai/Kimi-K2.7-Code-Highspeed"),
    ("kimi-k2.6", "moonshotai/Kimi-K2.6"),
    ("kimi2.6", "moonshotai/Kimi-K2.6"),
    ("kimi-k2.5", "moonshotai/Kimi-K2.5"),
    # Zhipu - note glm-5.3-flash lives in the z-ai namespace, not zai-org.
    ("glm-5.3-flash", "z-ai/glm-5.3-flash"),
    ("glm5.3-flash", "z-ai/glm-5.3-flash"),
    ("glm-5.3", "zai-org/GLM-5.3"),
    ("glm-5.2-fast", "zai-org/GLM-5.2-Fast"),
    ("glm-5.1", "zai-org/GLM-5.1"),
    ("glm-5", "zai-org/GLM-5"),
    # MiniMax.
    ("minimax-m3", "MiniMaxAI/MiniMax-M3"),
    ("minimax3", "MiniMaxAI/MiniMax-M3"),
    ("minimax", "MiniMaxAI/MiniMax-M3"),
    ("minimax-m2.7", "MiniMaxAI/MiniMax-M2.7"),
    ("minimax-m2.5", "MiniMaxAI/MiniMax-M2.5"),
    # DeepSeek.
    ("deepseek-v4-pro", "deepseek/deepseek-v4-pro"),
    ("deepseek-v4", "deepseek/deepseek-v4-pro"),
    ("DEEPSEEK-PRO", "deepseek/deepseek-v4-pro"),
    ("deepseek", "deepseek/deepseek-v4-pro"),
    ("deepseek-v4-flash", "deepseek/deepseek-v4-flash"),
    ("deepseek-flash", "deepseek/deepseek-v4-flash"),
    ("deepseek-v4-flash-vision", "deepseek/deepseek-v4-flash-vision-exp"),
    ("deepseek-v4-flash-fast", "deepseek/deepseek-v4-flash-fast"),
    # Qwen.
    ("qwen-3.8-max-0902", "Qwen/Qwen3.8-Max-0902"),
    ("qwen-3.8-max", "Qwen/Qwen3.8-Max"),
    ("qwen3.8-max", "Qwen/Qwen3.8-Max"),
    ("qwen-max", "Qwen/Qwen3.8-Max"),
    ("qwen", "Qwen/Qwen3.8-Max"),
    ("qwen-3.8-27b", "Qwen/Qwen3.8-27B"),
    ("qwen-3.8-flash", "Qwen/Qwen3.8-Flash"),
    ("qwen-3.7-flash", "Qwen/Qwen3.7-Flash"),
    ("qwen-3.7-max", "Qwen/Qwen3.7-Max"),
    ("qwen3.7-max", "Qwen/Qwen3.7-Max"),
    ("qwen-3.7-plus", "Qwen/Qwen3.7-Plus"),
    ("qwen-3.6-max-preview", "Qwen/Qwen3.6-Max-Preview"),
    ("qwen3.6-max", "Qwen/Qwen3.6-Max-Preview"),
    ("qwen-3.6-plus", "Qwen/Qwen3.6-Plus"),
    ("qwen3.6", "Qwen/Qwen3.6-Plus"),
    # StepFun.
    ("step-3.7-flash", "stepfun/Step-3.7-Flash"),
    ("step3.7", "stepfun/Step-3.7-Flash"),
    ("step-3.5-flash", "stepfun/Step-3.5-Flash"),
    ("step3.5", "stepfun/Step-3.5-Flash"),
    # Xiaomi MiMo.
    ("mimo-v2.5-pro", "xiaomi/mimo-v2.5-pro"),
    ("mimo-pro", "xiaomi/mimo-v2.5-pro"),
    ("mimo", "xiaomi/mimo-v2.5-pro"),
    ("mimo-v2.5", "xiaomi/mimo-v2.5"),
    # NVIDIA.
    ("nemotron-3-ultra", "nvidia/nemotron-3-ultra-550b-a55b"),
    ("nemotron", "nvidia/nemotron-3-ultra-550b-a55b"),
    # Premium closed-source models included on the Go plan.
    ("gpt-luna", "gpt-5.6-luna"),
    ("tencent/hy3", "tencent/hy3-paid"),
    ("openai/gpt-5.6-luna", "gpt-5.6-luna"),
    ("gpt-5.6", "gpt-5.6-luna"),
    ("muse-spark-1.3-contributor", "meta/muse-spark-1.3-contributor"),
    ("muse-1.3", "meta/muse-spark-1.3-contributor"),
    ("muse-spark-1.2-contributor", "meta/muse-spark-1.2-contributor"),
    ("muse-1.2", "meta/muse-spark-1.2-contributor"),
    ("grok-4.5", "xai/grok-4.5"),
    ("grok", "xai/grok-4.5"),
    ("inkling", "thinkingmachines/inkling"),
    ("inkling-small", "thinkingmachines/inkling-small"),
    # Full ids pass through.
    ("MiniMaxAI/MiniMax-M3", "MiniMaxAI/MiniMax-M3"),
    ("Qwen/Qwen3.8-Max", "Qwen/Qwen3.8-Max"),
    ("stepfun/Step-3.7-Flash", "stepfun/Step-3.7-Flash"),
    ("xiaomi/mimo-v2.5", "xiaomi/mimo-v2.5"),
    # Unknown names pass through unchanged.
    ("some/unknown-model", "some/unknown-model"),
    ("claude-sonnet-4-6", "claude-sonnet-4-6"),
    ("", ""),
]


@pytest.mark.parametrize(("alias", "expected"), CASES)
def test_alias_table(alias, expected):
    assert seed_table().map_model(alias) == expected


def test_no_alias_points_at_a_missing_model():
    """Every alias target must be a model the upstream actually recognises;
    tencent/hy3 was an internal provider slug and 403d on every request."""
    data = json.loads(Path("models.json").read_text(encoding="utf-8"))
    ids = {m["id"] if isinstance(m, dict) else m for m in data["models"]}
    dangling = {k: v for k, v in data["aliases"].items() if v not in ids}
    assert not dangling, f"aliases with no matching model: {dangling}"


def test_no_identity_aliases():
    data = json.loads(Path("models.json").read_text(encoding="utf-8"))
    assert not [k for k, v in data["aliases"].items() if k == v]


def test_alias_lookup_is_case_insensitive():
    assert seed_table().map_model("MiniMax") == "MiniMaxAI/MiniMax-M3"
    assert seed_table().map_model("  KIMI-K2.6  ") == "moonshotai/Kimi-K2.6"


def test_shipped_file_is_a_complete_go_plan_catalog():
    table = ModelRegistry().table
    assert len(table.model_list()) == 42
    assert len(table.aliases) == 87

    models = table.model_list()
    ids = {model["id"] for model in models}
    assert len(ids) == 42
    for model in models:
        assert set(model) == {"id", "owned_by", "name"}
        assert model["id"] and model["owned_by"] and model["name"]
    # Every alias must point at a model in the catalog.
    assert set(table.aliases.values()) <= ids
    # Every model must be reachable by at least one alias.
    assert ids <= set(table.aliases.values())


def test_config_file_replaces_the_defaults(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({
        "aliases": {"new-model": "Acme/New-Model"},
        "models": [{"id": "Acme/New-Model", "owned_by": "acme"}],
    }), encoding="utf-8")

    table = ModelRegistry(path).table
    assert table.map_model("new-model") == "Acme/New-Model"
    assert table.map_model("mimo") == "mimo"          # old aliases are gone
    assert table.model_list() == [{"id": "Acme/New-Model", "owned_by": "acme", "name": ""}]


def test_models_may_be_left_out():
    table = build_table({"aliases": {"a": "V/A", "b": "V/A", "c": "W/B"}})
    assert table.models == [
        {"id": "V/A", "owned_by": "v", "name": ""},
        {"id": "W/B", "owned_by": "w", "name": ""},
    ]


def test_string_model_entries_get_a_derived_owner():
    table = build_table({"aliases": {"a": "zai-org/GLM-5"}, "models": ["zai-org/GLM-5"]})
    assert table.models == [{"id": "zai-org/GLM-5", "owned_by": "zai-org", "name": ""}]


def test_missing_file_falls_back_to_the_seeded_table(tmp_path):
    table = ModelRegistry(tmp_path / "nope.json").table
    assert table.map_model("minimax") == "MiniMaxAI/MiniMax-M3"
    assert len(table.model_list()) == 42


def test_broken_file_falls_back_to_the_seeded_table(tmp_path):
    path = tmp_path / "models.json"
    path.write_text("{ not json", encoding="utf-8")

    registry = ModelRegistry(path)
    assert registry.table.map_model("minimax") == "MiniMaxAI/MiniMax-M3"
    assert "JSONDecodeError" in (registry.last_error or "")


def test_invalid_shape_is_rejected():
    for data in ({}, {"aliases": []}, {"aliases": {}}, {"aliases": {"a": 1}}, {"models": {}}):
        with pytest.raises(ValueError):
            build_table(data)


def test_hot_reload_picks_up_edits(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"aliases": {"v1": "V/One"}}), encoding="utf-8")
    registry = ModelRegistry(path)
    assert registry.table.map_model("v1") == "V/One"

    path.write_text(json.dumps({"aliases": {"v2": "V/Two"}}), encoding="utf-8")
    # An unchanged mtime is a no-op; nudge it forward to simulate a real save.
    future = time.time() + 60
    os.utime(path, (future, future))

    assert registry.reload_if_changed().map_model("v2") == "V/Two"
    assert registry.table.map_model("v1") == "v1"


def test_reload_if_changed_is_a_noop_when_untouched(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"aliases": {"v1": "V/One"}}), encoding="utf-8")
    registry = ModelRegistry(path)

    reloads = 0

    def counted_reload():
        nonlocal reloads
        reloads += 1
        return ModelRegistry.reload(registry)

    monkeypatch.setattr(registry, "reload", counted_reload)
    before = registry.table

    for _ in range(50):
        assert registry.reload_if_changed() is before
    assert reloads == 0

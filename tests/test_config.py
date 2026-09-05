"""Configuration precedence: CLI override > environment > default."""

import pytest

from commandcode_proxy import config as config_module
from commandcode_proxy.config import Config, load_dotenv

CLEAR_ENV = ("CC_PROXY_HOST", "CC_PROXY_PORT", "CC_PROXY_API_KEY", "CC_PROXY_BASE_URL",
             "CC_PROXY_MODELS_FILE", "CC_PROXY_COMMAND_CODE_VERSION", "CC_PROXY_ZDR",
             "CC_PROXY_DEBUG")


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """These tests must not read the developer's real .env out of the repo root."""
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: None)


def _reset_env(monkeypatch, **values):
    for name in CLEAR_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(f"CC_PROXY_{name}", value)


def test_defaults(monkeypatch):
    _reset_env(monkeypatch)
    config = Config.from_env()
    assert config == Config()
    assert (config.host, config.port, config.api_key) == ("127.0.0.1", 55990, "")
    assert config.base_url == "https://api.commandcode.ai"
    assert config.debug is False


def test_environment_is_read(monkeypatch):
    _reset_env(monkeypatch, HOST="0.0.0.0", PORT="9000", API_KEY="sk-env", DEBUG="true")
    config = Config.from_env()
    assert (config.host, config.port, config.api_key, config.debug) == ("0.0.0.0", 9000, "sk-env", True)


def test_cli_overrides_environment(monkeypatch):
    _reset_env(monkeypatch, HOST="0.0.0.0", PORT="9000", API_KEY="sk-env")
    config = Config.from_env(host="127.0.0.1", port=1234)
    assert (config.host, config.port) == ("127.0.0.1", 1234)
    assert config.api_key == "sk-env"      # not overridden, falls back to env


def test_debug_accepts_the_usual_truthy_spellings(monkeypatch):
    for value in ("1", "true", "True", "YES", "on"):
        _reset_env(monkeypatch, DEBUG=value)
        assert Config.from_env().debug is True
    for value in ("0", "false", "no", "", "off"):
        _reset_env(monkeypatch, DEBUG=value)
        assert Config.from_env().debug is False


def test_invalid_values_fall_back_to_the_default(monkeypatch):
    _reset_env(monkeypatch, PORT="not-a-port", DEBUG="maybe")
    config = Config.from_env()
    assert config.port == 55990
    assert config.debug is False


def test_upstream_url_has_no_double_slash(monkeypatch):
    _reset_env(monkeypatch, BASE_URL="https://api.commandcode.ai/")
    assert Config.from_env().upstream_url() == "https://api.commandcode.ai/alpha/generate"


def test_models_path_defaults_to_the_repo_file(monkeypatch):
    _reset_env(monkeypatch)
    assert Config.from_env().models_path.name == "models.json"


def test_models_file_overrides_the_default(monkeypatch, tmp_path):
    _reset_env(monkeypatch, MODELS_FILE=str(tmp_path / "custom.json"))
    assert Config.from_env().models_path == tmp_path / "custom.json"


def test_dotenv_is_loaded_without_overriding_existing_values(monkeypatch, tmp_path):
    _reset_env(monkeypatch, HOST="from-env")
    (tmp_path / ".env").write_text(
        "# comment\n"
        "CC_PROXY_PORT=7000\n"          # prefixed
        'CC_PROXY_API_KEY="sk-quoted"\n'
        "BASE_URL= https://upstream.test \n"   # bare key, surrounded by spaces
        "\n"
        "CC_PROXY_HOST=from-dotenv\n",
        encoding="utf-8",
    )
    load_dotenv(tmp_path / ".env")

    assert Config.from_env().port == 7000
    assert Config.from_env().api_key == "sk-quoted"
    assert Config.from_env().base_url == "https://upstream.test"
    assert Config.from_env().host == "from-env"     # real env wins over .env


def test_missing_dotenv_is_a_noop(monkeypatch, tmp_path):
    _reset_env(monkeypatch)
    load_dotenv(tmp_path / "does-not-exist.env")
    assert Config.from_env().port == 55990

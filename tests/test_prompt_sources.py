"""Prompt upgrades preserve custom text, empty-value semantics and other settings."""

import copy
import json

import pytest

from _loader import PLUGIN_DIR, load_plugin_module


settings = load_plugin_module("prompt_settings")
defaults = load_plugin_module("config")
PROMPTS = [
    ("usage_prompt", defaults.DEFAULT_USAGE_PROMPT),
    ("autonomous_usage_prompt", defaults.DEFAULT_AUTONOMOUS_USAGE_PROMPT),
]


@pytest.mark.parametrize("key,shipped", PROMPTS)
def test_auto_follows_new_default_without_rewriting_config(key, shipped):
    config = {key: shipped.replace("\n", "\r\n"), f"{key}_source": "auto", "admin_users": ["QQ:1"]}
    before = copy.deepcopy(config)
    assert settings.resolve_prompt(config, key, "new release default") == "new release default"
    assert config == before


@pytest.mark.parametrize("key,shipped", PROMPTS)
def test_auto_preserves_unknown_and_edited_defaults(key, shipped):
    for value in ("my custom rule", shipped + "\nMy additional rule."):
        assert settings.resolve_prompt({key: value}, key, "new default") == value


@pytest.mark.parametrize("key,shipped", PROMPTS)
def test_explicit_custom_can_pin_old_default_and_disable_prompt(key, shipped):
    config = {key: shipped, f"{key}_source": "custom"}
    assert settings.resolve_prompt(config, key, "new default") == shipped
    config[key] = ""
    assert settings.resolve_prompt(config, key, "new default") == ""


@pytest.mark.parametrize("key,shipped", PROMPTS)
def test_switching_sources_restores_custom_text(key, shipped):
    config = {key: "my saved rule", "unrelated": {"keep": True}}
    config[f"{key}_source"] = "default"
    assert settings.resolve_prompt(config, key, shipped) == shipped
    config[f"{key}_source"] = "custom"
    assert settings.resolve_prompt(config, key, shipped) == "my saved rule"
    assert config["unrelated"] == {"keep": True}


@pytest.mark.parametrize("key,shipped", PROMPTS)
def test_auto_missing_and_legacy_empty_values(key, shipped):
    assert settings.resolve_prompt({}, key, shipped) == shipped
    assert settings.resolve_prompt({key: ""}, key, shipped) == (shipped if key == "autonomous_usage_prompt" else "")
    assert settings.resolve_prompt({key: "saved", f"{key}_source": "unknown"}, key, shipped) == "saved"


@pytest.mark.parametrize("nested", [False, True])
def test_plugin_resolves_sources_independently(reminder_main, nested):
    plugin = reminder_main.ReminderPlugin.__new__(reminder_main.ReminderPlugin)
    plugin._default_usage_prompt = "latest reminder default"
    config = {
        "usage_prompt_source": "default", "usage_prompt": "saved reminder rule",
        "autonomous_usage_prompt_source": "custom", "autonomous_usage_prompt": "custom autonomous rule",
    }
    plugin.plugin_cfg = {"advanced_config": config} if nested else config
    before = copy.deepcopy(plugin.plugin_cfg)
    assert plugin._get_usage_prompt() == "latest reminder default"
    assert plugin._get_autonomous_usage_prompt() == "custom autonomous rule"
    assert plugin.plugin_cfg == before
    config["autonomous_usage_prompt"] = ""
    assert plugin._get_autonomous_usage_prompt() == ""


def test_schema_works_with_core_and_has_matching_defaults():
    from core.config.config_field import create_field_from_schema

    schema = json.loads((PLUGIN_DIR / "schema.json").read_text(encoding="utf-8"))
    for key, spec in schema.items():
        create_field_from_schema(key, spec)
    fields = schema["advanced_config"]["fields"]
    for key, shipped in PROMPTS:
        source = fields[f"{key}_source"]
        assert source["default"] == defaults.ADVANCED_CONFIG_DEFAULTS[f"{key}_source"] == "auto"
        assert source["options"] == ["auto", "default", "custom"]
        assert source["locales"]["en"]["hint"]
        assert fields[key]["default"] == shipped
        assert settings.resolve_prompt({key: fields[key]["default"]}, key, "future default") == "future default"

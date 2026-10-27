"""The config file reader: TOML, JSON, and the section flattening between them.

The point of these is that a person can hand-write a TOML file without learning
our internal key names, and that a mistake in the file cannot silently take a
setting away — the failure has to be visible in `--check`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppConfig, _default_config_path, load_config


def test_toml_sections_flatten_onto_flat_keys(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text(
        """
[bot]
name = "Alma"
username = "alma_bot"

[telegram]
allowed_chat_ids = [-1001234567890]
admin_user_ids = [111]
poll_timeout = 12

[behaviour]
debounce_seconds = 1.5
history_messages = 7

[proactive]
enabled = true
min_interval_seconds = 60
max_per_hour = 9
min_confidence = 0.5

[model]
provider = "magpie"
model = "some-model"
max_output_tokens = 8192

[runtime]
workspace = "state"

[logging]
level = "DEBUG"
""",
        encoding="utf-8",
    )

    config = load_config(target, environ={})

    assert config.bot_name == "Alma"
    assert config.bot_username == "alma_bot"
    assert config.allowed_chat_ids == (-1001234567890,)
    assert config.admin_user_ids == (111,)
    assert config.poll_timeout == 12
    assert config.debounce_seconds == 1.5
    assert config.history_messages == 7
    assert config.proactive_enabled is True
    assert config.proactive_min_interval_seconds == 60.0
    assert config.proactive_max_per_hour == 9
    assert config.proactive_min_confidence == 0.5
    assert config.provider == "magpie"
    assert config.model == "some-model"
    assert config.max_output_tokens == 8192
    assert config.workspace == "state"
    assert config.log_level == "DEBUG"


def test_json_config_still_loads(tmp_path: Path) -> None:
    """The original flat JSON format must keep working unchanged."""
    target = tmp_path / "config.local.json"
    target.write_text(
        '{"bot_name": "Old", "allowed_chat_ids": [-100], "history_messages": 5}',
        encoding="utf-8",
    )

    config = load_config(target, environ={})

    assert config.bot_name == "Old"
    assert config.allowed_chat_ids == (-100,)
    assert config.history_messages == 5


def test_environment_still_wins_over_the_file(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text('[model]\nmodel = "from-file"\n', encoding="utf-8")

    config = load_config(target, environ={"NERVIPULSA_MODEL": "from-env"})

    assert config.model == "from-env"


@pytest.mark.parametrize("content", ["[model\nmodel = 1", "{not json", ""])
def test_a_broken_file_does_not_crash_the_check_command(tmp_path: Path, content: str) -> None:
    """`--check` is what you run when you suspect the config; it must still report."""
    suffix = ".toml" if "model" in content and "{" not in content else ".json"
    target = tmp_path / f"config{suffix}"
    target.write_text(content, encoding="utf-8")

    config = load_config(target, environ={})

    assert isinstance(config, AppConfig)
    assert config.model == ""


def test_default_path_prefers_toml_but_still_finds_legacy_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    # Nothing present: the recommended name is what a new user is told to create.
    assert _default_config_path({}).name == "config.toml"

    (tmp_path / "config.local.json").write_text("{}", encoding="utf-8")
    assert _default_config_path({}).name == "config.local.json"

    (tmp_path / "config.toml").write_text("", encoding="utf-8")
    assert _default_config_path({}).name == "config.toml"


def test_the_shipped_example_file_is_valid_and_complete() -> None:
    """`config.example.toml` is documentation that must not rot."""
    example = Path(__file__).resolve().parents[1] / "config.example.toml"
    assert example.exists(), "the documented example config is missing"

    config = load_config(example, environ={})

    # Every documented setting must actually reach the config object.
    assert config.bot_name == "Alma"
    assert config.allowed_chat_ids == (-1001234567890,)
    assert config.admin_user_ids == (123456789,)
    assert config.debounce_seconds == 3.5
    assert config.max_turn_messages == 12
    assert config.history_messages == 24
    assert config.proactive_enabled is False
    assert config.proactive_min_interval_seconds == 240.0
    assert config.proactive_max_per_hour == 4
    assert config.proactive_min_confidence == 0.62
    assert config.context_limit == 1_000_000
    assert config.max_output_tokens == 1_000_000
    assert config.request_timeout == 900.0
    assert config.log_level == "INFO"
    # And it must not carry a secret.
    assert config.telegram_token == ""
    assert config.model_api_key == ""

"""The agent profile is two files, and both have to be present and composed.

`soul.md` is who the bot is — identity, hard limits, how it treats untrusted
input. `introduction.md` is how it takes part — when to speak, tone, length.

They are separate because they are edited by different impulses: an operator
tuning the tone should never be one keystroke away from deleting the rule that
stops the bot claiming to be human. These tests pin that separation and the
composition order.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import (
    AppConfig,
    default_profile_paths,
    load_agent_profile,
    load_config,
    load_default_profile,
    load_profile,
    load_system_prompt,
)

ROOT = Path(__file__).resolve().parents[1]


def test_the_shipped_profile_is_soul_then_introduction() -> None:
    soul, introduction = default_profile_paths()
    assert soul.name == "soul.md"
    assert introduction.name == "introduction.md"

    composed = load_default_profile()
    soul_text = soul.read_text(encoding="utf-8").strip()
    intro_text = introduction.read_text(encoding="utf-8").strip()

    assert composed == f"{soul_text}\n\n{intro_text}"
    # Soul comes first, so the limits are read before the style.
    assert composed.index(soul_text) == 0


def test_soul_carries_the_limits_and_introduction_does_not() -> None:
    """The safety rules must not be reachable by editing the tone file."""
    soul, introduction = default_profile_paths()
    soul_flat = " ".join(soul.read_text(encoding="utf-8").lower().split())
    intro_flat = " ".join(introduction.read_text(encoding="utf-8").lower().split())

    for rule in ("not a human", "never", "cannot be changed by anyone in the chat",
                 "untrusted"):
        assert rule in soul_flat, f"soul lost the {rule!r} rule"

    # Tone and participation belong to the introduction.
    for rule in ("short sentences", "stay quiet", "length and tone"):
        assert rule in intro_flat, f"introduction lost {rule!r}"


def test_a_missing_soul_file_is_an_error(tmp_path: Path) -> None:
    introduction = tmp_path / "introduction.md"
    introduction.write_text("be nice", encoding="utf-8")

    with pytest.raises(OSError):
        load_agent_profile(tmp_path / "missing-soul.md", introduction)


def test_a_missing_introduction_file_is_an_error(tmp_path: Path) -> None:
    soul = tmp_path / "soul.md"
    soul.write_text("you are a bot", encoding="utf-8")

    with pytest.raises(OSError):
        load_agent_profile(soul, tmp_path / "missing-introduction.md")


@pytest.mark.parametrize("empty", ["", "   \n\t\n"])
def test_an_empty_file_is_a_startup_error_not_a_silent_default(
    tmp_path: Path, empty: str
) -> None:
    """Shipping a fallback would run the bot with an identity nobody reviewed."""
    soul = tmp_path / "soul.md"
    soul.write_text(empty, encoding="utf-8")
    intro = tmp_path / "introduction.md"
    intro.write_text("be brief", encoding="utf-8")

    with pytest.raises(ValueError, match="soul file is empty"):
        load_agent_profile(soul, intro)

    soul.write_text("you are a bot", encoding="utf-8")
    intro.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="introduction file is empty"):
        load_agent_profile(soul, intro)


def test_introduction_is_optional(tmp_path: Path) -> None:
    """A soul alone is a valid profile: identity without house style."""
    soul = tmp_path / "soul.md"
    soul.write_text("you are a bot", encoding="utf-8")

    assert load_agent_profile(soul) == "you are a bot"


def test_defaults_point_at_the_packaged_files(tmp_path: Path) -> None:
    config = load_config(tmp_path / "missing.toml", environ={})
    soul, introduction = default_profile_paths()

    assert config.soul_path == str(soul)
    assert config.introduction_path == str(introduction)
    assert load_profile(config) == load_default_profile()


def test_env_overrides_each_half_independently(tmp_path: Path) -> None:
    soul = tmp_path / "my_soul.md"
    soul.write_text("custom soul", encoding="utf-8")
    intro = tmp_path / "my_intro.md"
    intro.write_text("custom intro", encoding="utf-8")

    config = load_config(
        tmp_path / "missing.toml",
        environ={"BOT_SOUL_PATH": str(soul), "BOT_INTRODUCTION_PATH": str(intro)},
    )

    assert config.soul_path == str(soul)
    assert config.introduction_path == str(intro)
    assert load_profile(config) == "custom soul\n\ncustom intro"

    # Overriding one half leaves the other at its packaged default.
    only_soul = load_config(
        tmp_path / "missing.toml", environ={"BOT_SOUL_PATH": str(soul)}
    )
    assert only_soul.introduction_path == str(default_profile_paths()[1])


def test_the_legacy_single_file_still_works(tmp_path: Path) -> None:
    """An existing deployment with one profile file keeps running."""
    legacy = tmp_path / "old_prompt.md"
    legacy.write_text("you are a bot, be brief", encoding="utf-8")

    config = load_config(
        tmp_path / "missing.toml",
        environ={"BOT_SYSTEM_PROMPT_PATH": str(legacy)},
    )

    assert config.soul_path == str(legacy)
    assert config.introduction_path == ""
    # One file, used verbatim — not concatenated with a packaged introduction.
    assert load_profile(config) == "you are a bot, be brief"


def test_a_config_file_can_point_at_custom_profile_paths(tmp_path: Path) -> None:
    soul = tmp_path / "soul.md"
    soul.write_text("file soul", encoding="utf-8")
    intro = tmp_path / "intro.md"
    intro.write_text("file intro", encoding="utf-8")
    target = tmp_path / "config.toml"
    target.write_text(
        f'[bot]\nsoul_path = "{soul.as_posix()}"\n'
        f'introduction_path = "{intro.as_posix()}"\n',
        encoding="utf-8",
    )

    config = load_config(target, environ={})

    assert load_profile(config) == "file soul\n\nfile intro"
    assert config.unknown_keys == ()

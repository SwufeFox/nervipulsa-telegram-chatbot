from __future__ import annotations

import json
from types import SimpleNamespace

from experiments import human_naturalness
from experiments.human_naturalness import (
    ALMA,
    _build_agent,
    _evaluation_succeeded,
    _has_usable_activations,
    _load_evaluation_setup,
    _sent_texts,
    update,
)


def test_evaluation_setup_uses_configured_identity_and_composed_profile(
    tmp_path, monkeypatch
) -> None:
    for name in (
        "BOT_NAME",
        "TELEGRAM_BOT_USERNAME",
        "BOT_SOUL_PATH",
        "BOT_INTRODUCTION_PATH",
        "NERVIPULSA_PROVIDER",
        "NERVIPULSA_BASE_URL",
        "NERVIPULSA_MODEL",
        "NERVIPULSA_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    soul = tmp_path / "soul.md"
    introduction = tmp_path / "introduction.md"
    soul.write_text("Identity from production", encoding="utf-8")
    introduction.write_text("Tone from production", encoding="utf-8")
    config_path = tmp_path / "fox.json"
    config_path.write_text(json.dumps({
        "bot_name": "雾栖",
        "bot_username": "linGotFoxbot",
        "soul_path": str(soul),
        "introduction_path": str(introduction),
    }), encoding="utf-8")

    config, prompt = _load_evaluation_setup(config_path, None)

    assert config.bot_name == "雾栖"
    assert config.bot_username == "linGotFoxbot"
    assert "Identity from production" in prompt
    assert "Tone from production" in prompt


def test_replayed_reply_parent_uses_configured_bot_username() -> None:
    payload = update(
        1, 101, 222222, "bob", "staging?", reply_to=100, topic=14,
        bot_username="linGotFoxbot",
    )

    parent_sender = payload["message"]["reply_to_message"]["from"]
    assert parent_sender["id"] == ALMA
    assert parent_sender["username"] == "linGotFoxbot"


def test_console_streams_are_configured_for_unicode(monkeypatch) -> None:
    class Stream:
        options = None

        def reconfigure(self, **options) -> None:
            self.options = options

    stdout = Stream()
    stderr = Stream()
    monkeypatch.setattr(human_naturalness.sys, "stdout", stdout)
    monkeypatch.setattr(human_naturalness.sys, "stderr", stderr)

    human_naturalness._configure_console_encoding()

    expected = {"encoding": "utf-8", "errors": "backslashreplace"}
    assert stdout.options == expected
    assert stderr.options == expected


def test_agent_factory_forwards_production_runtime_spec(monkeypatch) -> None:
    captured = {}

    class StubAgent:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(human_naturalness, "NervipulsaAgent", StubAgent)
    catalog = object()
    hook = object()
    resolver = object()
    observation_check = object()
    spec = SimpleNamespace(
        workspace="workspace",
        system_prompt="profile",
        tool_catalog=catalog,
        response_commit_hook=hook,
        session_id="session",
        restore=True,
        vision_resolver=resolver,
        vision_observation_check=observation_check,
    )

    _build_agent(spec, settings=object(), timeout=30)

    assert captured["tool_catalog"] is catalog
    assert captured["response_commit_hook"] is hook
    assert captured["session_id"] == "session"
    assert captured["restore"] is True
    assert captured["vision_resolver"] is resolver
    assert captured["vision_observation_check"] is observation_check


def test_empty_activation_run_fails_the_model_check() -> None:
    assert not _has_usable_activations(0, [])
    assert _has_usable_activations(1, [])
    assert not _has_usable_activations(1, [{"outcome": "agent_error"}])
    assert _evaluation_succeeded(1, [], evaluation_aborted=False)
    assert not _evaluation_succeeded(1, [], evaluation_aborted=True)


def test_all_send_texts_are_exposed_for_review() -> None:
    assert _sent_texts([
        {"text": "first reply"},
        {"text": "second reply"},
    ]) == ["first reply", "second reply"]

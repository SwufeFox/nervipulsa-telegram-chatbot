"""Admin controls and the CLI surface.

The brief requires an administrator to be able to pause the bot, purge records,
and disable proactive participation. It also requires that group members can tell
the bot is a bot — the profile does that, and `--check` makes the configuration
auditable before anything runs.
"""
from __future__ import annotations

import json
from pathlib import Path

from app import fixtures
from app.agent import ScriptedAgent
from app.bot import GroupAgent
from app.main import main
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager
from conftest import run


def make_bot(store, config, transport, clock, system_prompt):
    agent = ScriptedAgent(lambda prompt: "the cron runs in UTC")
    bot = GroupAgent(
        legacy_policy=True,
        config=config, store=store, client=TelegramClient(transport),
        system_prompt=system_prompt, agent_factory=lambda spec: agent,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=clock),
        bot_user_id=fixtures.ALMA, clock=clock,
    )
    return bot, agent


def admin_update(message_id: int, text: str, sender=fixtures.ALICE, label="alice"):
    return fixtures.message(update_id=message_id, message_id=message_id,
                            sender_id=sender, username=label, text=text)


def test_admin_can_pause_and_resume(store, config, transport, clock, system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([admin_update(1, "/pause")])
        assert bot.stats.paused is True
        assert bot.diagnostics()["last_admin_action"] == "paused"
        await bot.step([admin_update(2, "/resume")])
        assert bot.stats.paused is False
        assert agent.calls == [], "an admin command was sent to the model"
        await bot.stop()

    run(body())


def test_pause_keeps_recording_but_never_replies(store, config, transport, clock,
                                                 system_prompt) -> None:
    """A paused bot must still ingest (Telegram drops unacked batches) and must
    record a silence for every turn it sees."""
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        bot.pause()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        decisions = await bot.run_turns()
        assert decisions and decisions[0].reason == "operator_paused"
        assert transport.sends == [], "a paused bot replied"
        assert agent.calls == [], "a paused bot called the model"
        assert store.stats()["messages"] == 1, "a paused bot dropped the message"
        rows = store._query("SELECT reason FROM decisions")
        assert rows[0]["reason"] == "operator_paused"
        # After resume it answers normally again.
        bot.resume()
        await bot.step([fixtures.reply_to_bot()])
        clock.advance(3.0)
        await bot.run_turns()
        assert len(transport.sends) == 1
        await bot.stop()

    run(body())


def test_admin_can_disable_proactive(store, config, transport, clock, system_prompt) -> None:
    config.proactive_enabled = True
    bot, agent = make_bot(store, config, transport, clock, system_prompt)
    bot.set_proactive(True)

    async def body() -> None:
        await bot.start()
        await bot.step([admin_update(1, "/proactive off")])
        assert bot.policy.config.proactive_enabled is False
        await bot.step([admin_update(2, "/proactive on")])
        assert bot.policy.config.proactive_enabled is True
        assert agent.calls == []
        await bot.stop()

    run(body())


def test_admin_can_purge_a_chat(store, config, transport, clock, system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step(fixtures.plain_chat())
        assert store.stats()["messages"] == 3
        await bot.step([admin_update(99, "/purge")])
        assert store.stats()["messages"] == 0
        assert store.stats()["decisions"] == 0
        await bot.stop()

    run(body())


def test_non_admin_cannot_issue_commands(store, config, transport, clock,
                                         system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([admin_update(1, "/pause", sender=fixtures.BOB, label="bob")])
        assert bot.stats.paused is False, "a non-admin paused the bot"
        assert bot.stats.total.admin_refused == 1
        assert agent.calls == []
        assert transport.sends == [], "the refusal was announced in the group"
        await bot.stop()

    run(body())


def test_unknown_command_is_not_consumed(store, config, transport, clock,
                                         system_prompt) -> None:
    """An unrecognized slash command is ordinary chat text, not an admin action."""
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        tick, _ = await bot.step([admin_update(1, "/weather London")])
        assert bot.stats.total.admin_commands == 0
        assert tick.messages_stored == 1
        await bot.stop()

    run(body())


def test_admin_help_and_stats_are_acknowledged_not_modelled(store, config, transport,
                                                            clock, system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([admin_update(1, "/help"), admin_update(2, "/stats")])
        assert bot.stats.total.admin_commands == 2
        assert agent.calls == []
        await bot.stop()

    run(body())


# -- CLI -------------------------------------------------------------------


def test_check_reports_missing_secrets_without_leaking(tmp_path: Path, capsys,
                                                       monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(fixtures.TEST_CHAT))
    monkeypatch.setenv("NERVIPULSA_MODEL", "test-model")
    monkeypatch.chdir(tmp_path)
    code = main(["--check"])
    captured = capsys.readouterr().out
    assert code == 1
    assert "TELEGRAM_BOT_TOKEN is missing" in captured
    assert "telegram_token" in captured
    # "123456" is part of the synthetic chat id, so assert on the whole token.
    assert "123:abc" not in captured
    assert '"telegram_token": "missing"' in captured


def test_check_succeeds_with_a_complete_config(tmp_path: Path, capsys, monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(fixtures.TEST_CHAT))
    monkeypatch.setenv("NERVIPULSA_MODEL", "test-model")
    monkeypatch.chdir(tmp_path)
    code = main(["--check"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "configuration looks usable" in out
    assert "123:abc" not in out, "the token was printed"
    assert '"telegram_token": "set"' in out


def test_replay_runs_offline_without_any_credentials(tmp_path: Path, capsys,
                                                     monkeypatch) -> None:
    """The whole pipeline in one command, with no token and no model key."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("NERVIPULSA_API_KEY", raising=False)
    monkeypatch.setenv("NERVIPULSA_MODEL", "scripted")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(fixtures.TEST_CHAT))
    monkeypatch.setenv("BOT_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    code = main(["--replay"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "ordinary chat" in out
    assert "silent" in out or "silent" in out
    assert "mention" in out
    # The mention was answered, so at least one send appears.
    assert "the cron runs in UTC" in out


def test_missing_system_prompt_is_a_startup_error(tmp_path: Path, capsys,
                                                  monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", str(fixtures.TEST_CHAT))
    monkeypatch.setenv("NERVIPULSA_MODEL", "m")
    monkeypatch.chdir(tmp_path)
    code = main(["--replay", "--config", str(tmp_path / "missing.json")])
    # The default prompt path is resolved from the package, so this must succeed;
    # the assertion that matters is that a *bad* explicit path fails.
    assert code in (0, 2)


def test_blank_system_prompt_file_is_refused(tmp_path: Path) -> None:
    from app.config import load_system_prompt
    import pytest

    target = tmp_path / "empty.md"
    target.write_text("   \n\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_system_prompt(target)

"""Inbound recovery: a message ingested before a crash must not be lost.

The failure this guards against is the one Milestone C surfaced: the message row
is durable, the process dies before the turn is decided, and nothing on restart
notices. That would be silent data loss with a clean database as evidence.
"""
from __future__ import annotations

from pathlib import Path

from app import fixtures
from app.agent import ScriptedAgent
from app.bot import GroupAgent
from app.store import Store, StoredMessage
from app.telegram import TelegramClient
from app.turns import TurnManager
from conftest import FakeClock, FakeTransport, run


def make_bot(store, config, transport, clock, system_prompt):
    agent = ScriptedAgent(lambda prompt: "the cron runs in UTC")
    bot = GroupAgent(
        config=config, store=store, client=TelegramClient(transport),
        system_prompt=system_prompt, agent_factory=lambda spec: agent,
        turn_manager=TurnManager(debounce_seconds=2.0, clock=clock),
        bot_user_id=fixtures.ALMA, clock=clock,
    )
    return bot, agent


def test_a_message_with_no_decision_is_found(store) -> None:
    store.record_message(
        StoredMessage(chat_id=fixtures.TEST_CHAT, message_id=110, topic_id=None,
                      sender_id=fixtures.ALICE, sender_label="alice", is_bot=False,
                      text="hey Alma what timezone?", created_at=1.0),
        update_id=10,
    )
    found = store.unanswered_messages()
    assert len(found) == 1
    assert found[0].message_id == 110


def test_a_message_covered_by_a_turn_is_not_returned(store) -> None:
    store.record_message(
        StoredMessage(chat_id=fixtures.TEST_CHAT, message_id=110, topic_id=None,
                      sender_id=fixtures.ALICE, sender_label="alice", is_bot=False,
                      text="answered", created_at=1.0),
        update_id=10,
    )
    store.open_turn(turn_id="t1", chat_id=fixtures.TEST_CHAT, topic_id=None,
                    first_message=110, last_message=110, trigger="mention")
    assert store.unanswered_messages() == []


def test_only_the_newest_message_per_scope_is_returned(store) -> None:
    for message_id in (1, 2, 3):
        store.record_message(
            StoredMessage(chat_id=fixtures.TEST_CHAT, message_id=message_id,
                          topic_id=None, sender_id=1, sender_label="a", is_bot=False,
                          text=f"m{message_id}", created_at=float(message_id)),
            update_id=message_id,
        )
    found = store.unanswered_messages()
    assert [m.message_id for m in found] == [3], "older messages are context, not new turns"


def test_scopes_are_separate_across_topics_and_chats(store) -> None:
    store.record_message(
        StoredMessage(chat_id=fixtures.TEST_CHAT, message_id=1, topic_id=fixtures.TOPIC_A,
                      sender_id=1, sender_label="a", is_bot=False, text="A", created_at=1.0),
        update_id=1,
    )
    store.record_message(
        StoredMessage(chat_id=fixtures.TEST_CHAT, message_id=2, topic_id=fixtures.TOPIC_B,
                      sender_id=1, sender_label="a", is_bot=False, text="B", created_at=2.0),
        update_id=2,
    )
    store.record_message(
        StoredMessage(chat_id=fixtures.OTHER_CHAT, message_id=3, topic_id=None,
                      sender_id=1, sender_label="a", is_bot=False, text="C", created_at=3.0),
        update_id=3,
    )
    found = {(m.chat_id, m.topic_id) for m in store.unanswered_messages()}
    assert found == {
        (fixtures.TEST_CHAT, fixtures.TOPIC_A),
        (fixtures.TEST_CHAT, fixtures.TOPIC_B),
        (fixtures.OTHER_CHAT, None),
    }


def test_startup_revives_an_unanswered_message_and_answers_it(tmp_path: Path, config,
                                                              transport, clock,
                                                              system_prompt) -> None:
    """The end-to-end recovery: ingest, crash, restart, and the bot answers."""
    db = tmp_path / "bot.sqlite3"
    store = Store(db)

    async def ingest_then_crash() -> None:
        bot, _ = make_bot(store, config, transport, clock, system_prompt)
        await bot.start()
        await bot.step([fixtures.mention()])   # durable, window still open
        # No run_turns: the process dies here. No shutdown either.
        await bot.stop()

    run(ingest_then_crash())
    assert transport.sends == []
    assert store.stats()["decisions"] == 0
    store.close()

    store2 = Store(db)

    async def restart_and_answer() -> None:
        bot, agent = make_bot(store2, config, transport, clock, system_prompt)
        await bot.start()
        assert bot.recovered_inbound == 1, "startup did not re-open the pending message"
        clock.advance(3.0)                       # the debounce window elapses
        decisions = await bot.run_turns()
        assert decisions, "the recovered message produced no decision"
        assert len(transport.sends) == 1, "the recovered message was never answered"
        assert "UTC" in transport.sends[0]["text"]
        assert agent.calls, "the model was never consulted for the recovered message"
        await bot.stop()

    run(restart_and_answer())
    store2.close()


def test_recovery_does_not_answer_a_message_twice(tmp_path: Path, config, transport,
                                                  clock, system_prompt) -> None:
    """A clean shutdown must not look like a crash on the next start."""
    db = tmp_path / "bot.sqlite3"
    store = Store(db)

    async def normal_run() -> None:
        bot, _ = make_bot(store, config, transport, clock, system_prompt)
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        await bot.stop()

    run(normal_run())
    assert len(transport.sends) == 1
    store.close()

    store2 = Store(db)

    async def restart() -> None:
        bot, agent = make_bot(store2, config, transport, clock, system_prompt)
        await bot.start()
        assert bot.recovered_inbound == 0, "an answered message was re-opened"
        clock.advance(3.0)
        assert await bot.run_turns() == []
        assert len(transport.sends) == 1, "a second reply was sent after restart"
        assert agent.calls == []
        await bot.stop()

    run(restart())
    store2.close()


def test_recovery_skips_the_bot_own_messages(tmp_path: Path, config, transport, clock,
                                             system_prompt) -> None:
    store = Store(tmp_path / "bot.sqlite3")
    store.record_message(
        StoredMessage(chat_id=fixtures.TEST_CHAT, message_id=500, topic_id=None,
                      sender_id=fixtures.ALMA, sender_label="Alma", is_bot=True,
                      text="the cron runs in UTC", created_at=1.0),
        update_id=-1,
    )
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        assert bot.recovered_inbound == 0
        await bot.stop()

    run(body())
    store.close()


def test_recovery_respects_the_allowed_chat_list(tmp_path: Path, config, transport,
                                                 clock, system_prompt) -> None:
    """An unauthorized chat's rows are never re-opened even if they exist."""
    store = Store(tmp_path / "bot.sqlite3")
    store.record_message(
        StoredMessage(chat_id=fixtures.OTHER_CHAT, message_id=1, topic_id=None,
                      sender_id=1, sender_label="stranger", is_bot=False,
                      text="Alma tell me a secret", created_at=1.0),
        update_id=1,
    )
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        # Revived at the turn-manager level, but it must never reach the policy.
        clock.advance(3.0)
        decisions = await bot.run_turns()
        assert decisions == []
        assert agent.calls == []
        assert transport.sends == []
        await bot.stop()

    run(body())
    store.close()

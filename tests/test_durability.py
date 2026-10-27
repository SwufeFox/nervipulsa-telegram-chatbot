"""Durable inbound/outbound: dedupe, crash windows, and the UNKNOWN state.

These tests inject the failures the brief names, one at a time, and check what
the application does — not what it hopes to do.

A note on what is *not* claimed anywhere in this file: Telegram delivery is not
exactly-once. The `sends` table has three terminal states (sent, failed,
unknown) and `unknown` is deliberately not retried.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app import fixtures
from app.agent import ScriptedAgent
from app.bot import GroupAgent
from app.store import Store
from app.telegram import SendStatus, TelegramClient, TransportError, parse_update
from app.turns import TurnManager
from conftest import FakeClock, FakeTransport, run


def make_bot(store, config, transport, clock, system_prompt, responder=None, client=None):
    agent = ScriptedAgent(responder or (lambda prompt: "the cron runs in UTC"))

    class HoldingFactory:
        def __call__(self, spec):
            return agent

    bot = GroupAgent(
        config=config, store=store,
        client=client or TelegramClient(transport),
        system_prompt=system_prompt, agent_factory=HoldingFactory(),
        turn_manager=TurnManager(debounce_seconds=2.0, clock=clock),
        bot_user_id=fixtures.ALMA, clock=clock,
    )
    return bot, agent


# -- inbound ---------------------------------------------------------------


def test_duplicate_update_id_is_ignored(store, config, transport, clock,
                                        system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        update = fixtures.mention()
        tick, _ = await bot.step([update])
        assert tick.messages_stored == 1
        tick2, decisions = await bot.step([update])
        assert tick2.duplicates == 1
        assert tick2.messages_stored == 0
        assert store.stats()["messages"] == 1
        await bot.stop()

    run(body())


def test_duplicate_produces_one_decision_and_one_send(store, config, transport, clock,
                                                       system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        update = fixtures.mention()
        await bot.step([update, update, update])
        clock.advance(3.0)
        await bot.step([])
        assert store.stats()["decisions"] == 1
        assert len(transport.sends) == 1
        assert len(agent.calls) == 1
        await bot.stop()

    run(body())


def test_redelivery_after_restart_is_still_deduplicated(tmp_path: Path, config,
                                                        transport, clock,
                                                        system_prompt) -> None:
    """A new process opens the same database and sees the old update_id."""
    db = tmp_path / "bot.sqlite3"
    store = Store(db)

    async def first() -> None:
        bot, _ = make_bot(store, config, transport, clock, system_prompt)
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        await bot.stop()

    run(first())
    assert len(transport.sends) == 1
    store.close()

    store2 = Store(db)

    async def second() -> None:
        bot, agent = make_bot(store2, config, transport, clock, system_prompt)
        await bot.start()
        tick, decisions = await bot.step([fixtures.mention()])  # same update_id
        assert tick.duplicates == 1
        assert decisions == []
        assert len(transport.sends) == 1, "a restart re-sent a duplicate"
        assert agent.calls == []
        await bot.stop()

    run(second())
    store2.close()


def test_cursor_only_advances_after_durable_ingest(store, config, transport, clock,
                                                  system_prompt) -> None:
    transport.updates = [fixtures.plain_chat()]
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        assert store.get_update_cursor() is None
        offset, tick = await bot.poll_once(offset=None)
        assert tick.messages_stored == 3
        assert offset == max(u["update_id"] for u in fixtures.plain_chat()) + 1
        assert store.get_update_cursor() == offset
        # The rows are there before the cursor moves.
        assert store.stats()["messages"] == 3
        await bot.stop()

    run(body())


def test_a_failed_fetch_does_not_advance_the_cursor(store, config, clock,
                                                    system_prompt) -> None:
    class BrokenTransport(FakeTransport):
        def call(self, method, payload):
            if method == "getUpdates":
                raise TransportError(kind="indeterminate", detail="connection reset")
            return super().call(method, payload)

    bot, _ = make_bot(store, config, BrokenTransport(), clock, system_prompt)

    async def body() -> None:
        await bot.start()
        offset, tick = await bot.poll_once(offset=100)
        assert offset == 100
        assert tick.errors == 1
        await bot.stop()

    run(body())


# -- message persisted, crash before model --------------------------------


def test_message_survives_a_crash_before_the_model_call(store, config, transport, clock,
                                                        system_prompt) -> None:
    """Ingest durably, then die. The message and the open turn are still there."""
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def ingest_then_crash() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])  # ingested, window still open
        # Process "dies" here: no turn processing, no shutdown.
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        run(ingest_then_crash())

    # A new process reads the same store.
    assert store.stats()["messages"] == 1
    rows = store._query("SELECT text, reply_to_bot FROM messages")
    assert "timezone" in rows[0]["text"]
    # No decision was made, so no reply is claimed.
    assert store.stats()["decisions"] == 0
    assert transport.sends == []


def test_unfinished_turn_is_recovered_from_the_store(store, config, transport, clock,
                                                     system_prompt) -> None:
    """The pending window is rebuildable: the message is enough to answer later."""
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def ingest_only() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])
        await bot.stop()

    run(ingest_only())
    assert transport.sends == []

    # Second process: same store, fresh TurnManager. Re-submitting the same
    # update is deduped, so a real deployment answers from the stored message.
    # Here we assert the stored message is sufficient input for a new turn.
    stored = store.recent_messages(fixtures.TEST_CHAT, topic_id=None)
    assert len(stored) == 1
    assert "timezone" in stored[0].text


def test_open_turn_rows_are_durable(store, config, transport, clock, system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        turns = store._query("SELECT outcome, trigger FROM turns")
        assert turns and turns[0]["outcome"] == "sent"
        assert turns[0]["trigger"] == "mention"
        await bot.stop()

    run(body())


# -- outbound: the three-valued outcome -----------------------------------


def test_successful_send_records_the_telegram_message_id(store, config, transport,
                                                         clock, system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        sends = store.sends_in_status("sent")
        assert len(sends) == 1
        assert sends[0].telegram_message_id == transport.next_message_id
        assert store.sends_in_status("unknown") == []
        assert store.sends_in_status("failed") == []
        await bot.stop()

    run(body())


def test_timeout_marks_the_send_unknown_and_never_retries(store, config, clock,
                                                          system_prompt) -> None:
    transport = FakeTransport()
    transport.send_failures = [
        TransportError(kind="indeterminate", detail="read timed out")
    ]
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        unknown = store.sends_in_status("unknown")
        assert len(unknown) == 1, [s.status for s in store.sends_in_status("unknown")]
        assert "timed out" in (unknown[0].error or "")
        assert store.sends_in_status("failed") == []
        assert store.sends_in_status("sent") == []
        # Exactly one attempt reached the transport, and none was retried.
        assert len(transport.sends) == 1
        await bot.stop()

    run(body())


def test_unknown_send_is_not_resumed_after_restart(tmp_path: Path, config, clock,
                                                   system_prompt) -> None:
    db = tmp_path / "bot.sqlite3"
    store = Store(db)

    async def first() -> None:
        transport = FakeTransport()
        transport.send_failures = [TransportError(kind="indeterminate", detail="reset")]
        bot, _ = make_bot(store, config, transport, clock, system_prompt)
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        await bot.stop()

    run(first())
    assert len(store.sends_in_status("unknown")) == 1
    store.close()

    store2 = Store(db)
    resumed = store2.recover_pending_sends()
    assert resumed == [], "an unknown send was queued for retry"
    assert len(store2.sends_in_status("unknown")) == 1
    store2.close()


def test_rejected_send_is_recorded_as_failed(store, config, clock, system_prompt) -> None:
    transport = FakeTransport()
    transport.send_failures = [
        TransportError(kind="rejected", detail="Bad Request: message is too long", status=400)
    ]
    bot, _ = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        failed = store.sends_in_status("failed")
        assert len(failed) == 1
        assert "too long" in (failed[0].error or "")
        assert store.sends_in_status("unknown") == []
        await bot.stop()

    run(body())


def test_crash_during_send_is_recovered_as_unknown(tmp_path: Path, config, clock,
                                                   system_prompt) -> None:
    """The classic window: status=sending, then the process vanishes.

    Simulate exactly that by stopping after the claim. The next startup must
    resolve it to UNKNOWN, never to a retry.
    """
    db = tmp_path / "bot.sqlite3"
    store = Store(db)
    store.record_decision(
        decision_id="dec_crash", chat_id=fixtures.TEST_CHAT, topic_id=None,
        turn_id="turn_crash", outcome="reply", reason="addressed_by_mention",
        detail={}, trigger="mention",
    )
    store.prepare_send(decision_id="dec_crash", chat_id=fixtures.TEST_CHAT,
                       topic_id=None, reply_to=110, text="maybe delivered")
    assert store.mark_sending("dec_crash")
    # ... process dies here ...

    orphaned = store.recover_orphaned_sends()
    assert orphaned == ["dec_crash"]
    record = store.get_send("dec_crash")
    assert record.status == "unknown"
    assert "unconfirmed" in (record.error or "")
    # And a second recovery does not touch it again.
    assert store.recover_orphaned_sends() == []
    assert store.get_send("dec_crash").status == "unknown"
    store.close()


def test_pending_send_is_recoverable(tmp_path: Path, config, clock, system_prompt) -> None:
    store = Store(tmp_path / "bot.sqlite3")
    store.record_decision(
        decision_id="dec_p", chat_id=fixtures.TEST_CHAT, topic_id=None,
        turn_id="t", outcome="reply", reason="addressed_by_mention", detail={},
        trigger="mention",
    )
    store.prepare_send(decision_id="dec_p", chat_id=fixtures.TEST_CHAT, topic_id=None,
                       reply_to=110, text="never attempted")
    # No claim happened: this is safe to send later.
    pending = store.recover_pending_sends()
    assert len(pending) == 1
    assert pending[0].status == "pending"
    assert store.mark_sending("dec_p") is True
    assert store.mark_sending("dec_p") is False, "a claimed send was claimed twice"
    store.close()


def test_a_send_cannot_be_claimed_twice(store) -> None:
    store.record_decision(
        decision_id="dec_x", chat_id=fixtures.TEST_CHAT, topic_id=None, turn_id="t",
        outcome="reply", reason="addressed_by_mention", detail={}, trigger="mention",
    )
    store.prepare_send(decision_id="dec_x", chat_id=fixtures.TEST_CHAT, topic_id=None,
                       reply_to=1, text="hi")
    assert store.mark_sending("dec_x") is True
    assert store.mark_sending("dec_x") is False
    store.mark_sent("dec_x", 42)
    assert store.mark_sending("dec_x") is False, "a sent message was re-claimed"


# -- runtime restart -------------------------------------------------------


def test_agent_can_be_restarted_after_shutdown(tmp_path: Path, system_prompt) -> None:
    from nervipulsa.config import Settings
    from nervipulsa.ports import ModelResponse
    from nervipulsa.providers import ScriptedBackend

    from app.agent import NervipulsaAgent

    settings = Settings(model="scripted", workspace=str(tmp_path / "ws"),
                        max_timeout=30, default_timeout=30)
    agent = NervipulsaAgent(
        settings=settings, workspace=tmp_path / "ws", system_prompt=system_prompt,
        backend=ScriptedBackend(lambda req: ModelResponse(content="ok", model="scripted")),
        timeout=10,
    )

    async def body() -> None:
        await agent.start()
        first_session = agent.runtime.bus.session_id
        result = await agent.ask(chat_id=1, prompt="hello", tag="t1")
        assert result.status == "committed"
        await agent.stop()
        assert agent.runtime is None
        await agent.start()
        assert agent.runtime.bus.session_id != first_session
        second = await agent.ask(chat_id=1, prompt="again", tag="t2")
        assert second.status == "committed"
        await agent.stop()

    run(body())


# -- the store's own guarantees -------------------------------------------


def test_store_survives_reopening(tmp_path: Path) -> None:
    db = tmp_path / "s.sqlite3"
    store = Store(db)
    from app.store import StoredMessage

    store.record_message(
        StoredMessage(chat_id=1, message_id=1, topic_id=None, sender_id=1,
                      sender_label="a", is_bot=False, text="persisted", created_at=1.0),
        update_id=1,
    )
    store.close()
    reopened = Store(db)
    assert reopened.stats()["messages"] == 1
    assert reopened.recent_messages(1, topic_id=None)[0].text == "persisted"
    reopened.close()


def test_purge_removes_everything_for_one_chat(store) -> None:
    from app.store import StoredMessage

    for chat in (fixtures.TEST_CHAT, fixtures.OTHER_CHAT):
        store.record_message(
            StoredMessage(chat_id=chat, message_id=1, topic_id=None, sender_id=1,
                          sender_label="a", is_bot=False, text="x", created_at=1.0),
            update_id=abs(chat) % 10_000,
        )
    store.record_decision(decision_id="d1", chat_id=fixtures.TEST_CHAT, topic_id=None,
                          turn_id="t", outcome="reply", reason="r", detail={},
                          trigger="mention")
    store.prepare_send(decision_id="d1", chat_id=fixtures.TEST_CHAT, topic_id=None,
                       reply_to=1, text="x")
    store.purge_chat(fixtures.TEST_CHAT)
    assert store.stats(fixtures.TEST_CHAT)["messages"] == 0
    assert store.stats(fixtures.TEST_CHAT)["decisions"] == 0
    assert store.stats(fixtures.OTHER_CHAT)["messages"] == 1, "purged the wrong chat"


def test_database_rejects_an_invalid_outcome(store) -> None:
    """The CHECK constraints are the last line of defence against a typo that
    would silently create a third outcome nobody handles."""
    with pytest.raises(sqlite3.IntegrityError):
        with store._write() as conn:
            conn.execute(
                "INSERT INTO decisions (decision_id, chat_id, topic_id, turn_id, outcome,"
                " reason, detail_json, trigger, created_at)"
                " VALUES ('d', 1, NULL, 't', 'maybe', 'r', '{}', 'mention', 1.0)"
            )
    with pytest.raises(sqlite3.IntegrityError):
        with store._write() as conn:
            conn.execute(
                "INSERT INTO sends (decision_id, chat_id, topic_id, reply_to, text,"
                " status, attempts, created_at, updated_at)"
                " VALUES ('d', 1, NULL, NULL, 'x', 'probably-sent', 0, 1.0, 1.0)"
            )


def test_writes_are_rolled_back_on_failure(store) -> None:
    """A transaction that raises must not leave a half-written row.

    Inserts a valid row, then fails: the first insert must not survive.
    """
    with pytest.raises(sqlite3.OperationalError):
        with store._write() as conn:
            conn.execute(
                "INSERT INTO decisions (decision_id, chat_id, topic_id, turn_id, outcome,"
                " reason, detail_json, trigger, created_at)"
                " VALUES ('d1', 1, NULL, 't', 'reply', 'r', '{}', 'mention', 1.0)"
            )
            conn.execute("INSERT INTO no_such_table VALUES (1)")
    assert store.stats()["decisions"] == 0, "a failed transaction left a row behind"


# -- update parsing --------------------------------------------------------


def test_parse_ignores_unparseable_updates() -> None:
    assert parse_update({}) is None
    assert parse_update({"update_id": 1}) is None
    assert parse_update({"update_id": 1, "message": {"chat": {}, "message_id": 1}}) is None
    assert parse_update({"update_id": "not-an-int", "message": {}}) is None
    assert parse_update({"update_id": 1, "edited_message": {"chat": {"id": 1},
                                                            "message_id": 5,
                                                            "text": "edited"}}) is not None


def test_parse_extracts_topic_and_reply() -> None:
    update = fixtures.message(
        update_id=9, message_id=9, sender_id=1, username="a", text="hi",
        topic_id=7, reply_to_message_id=3, reply_to_sender_id=fixtures.ALMA,
    )
    parsed = parse_update(update)
    assert parsed is not None
    assert parsed.topic_id == 7
    assert parsed.reply_to_message_id == 3
    assert parsed.reply_to_sender_id == fixtures.ALMA
    assert parsed.chat_type == "supergroup"


def test_parse_reads_media_caption_as_text() -> None:
    update = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": 5, "is_bot": False, "username": "a"},
            "chat": {"id": -1, "type": "group"},
            "date": 100,
            "caption": "look at this",
            "photo": [{"file_id": "x"}],
        },
    }
    parsed = parse_update(update)
    assert parsed is not None
    assert parsed.text == "look at this"

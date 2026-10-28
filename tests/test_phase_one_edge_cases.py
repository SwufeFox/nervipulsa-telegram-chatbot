"""Phase-one failure boundaries; all Telegram and model I/O is simulated."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import subprocess
import sys
import time
from pathlib import Path

from app import fixtures
from app.agent import AgentResult, ScriptedAgent
from app.bot import GroupAgent
from app.config import AppConfig, load_default_profile
from app.store import Store, StoredMessage
from app.telegram import TelegramClient
from app.turns import TurnManager
from conftest import FakeClock, FakeTransport

ROOT = Path(__file__).resolve().parents[1]


def message(update_id, text):
    return fixtures.message(update_id=update_id, message_id=update_id,
                            sender_id=fixtures.ALICE, username="alice",
                            topic_id=fixtures.TOPIC_A, text=text)


def make_bot(db, clock, transport, agent):
    store = Store(db)
    config = AppConfig(bot_name="Alma", bot_username="alma_bot",
                       allowed_chat_ids=(fixtures.TEST_CHAT,), dry_run=False,
                       proactive_enabled=False, debounce_seconds=2,
                       workspace=str(db.parent), model="scripted")
    bot = GroupAgent(legacy_policy=True,
        config=config, store=store, client=TelegramClient(transport),
                     system_prompt=load_default_profile(), agent_factory=lambda spec: agent,
                     turn_manager=TurnManager(debounce_seconds=2, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)
    return bot, store


def test_hard_exit_inside_inbound_transaction_redelivery_and_cursor(tmp_path):
    db = tmp_path / "crash.db"
    code = '''
import os, sys
from app.store import Store, StoredMessage
from app import fixtures
s = Store(sys.argv[1])
def crash():
    # The same connection must see the uncommitted update before exiting.
    found = s._conn.execute("SELECT COUNT(*) FROM updates WHERE update_id=701").fetchone()[0]
    os._exit(73 if found == 1 else 74)
s._conn.create_function("crash_now", 0, crash)
s._conn.execute("CREATE TRIGGER crash_message BEFORE INSERT ON messages BEGIN SELECT crash_now(); END")
s._conn.commit()
s.record_inbound_message(StoredMessage(fixtures.TEST_CHAT, 701, fixtures.TOPIC_A,
    fixtures.ALICE, "alice", False, "@alma_bot original request", 1),
    update_id=701, payload={"update_id": 701})
os._exit(75)
'''
    child = subprocess.run([sys.executable, "-c", code, str(db)], cwd=ROOT, timeout=20)
    assert child.returncode == 73, "trigger must see the update inside the transaction"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM updates WHERE update_id=701").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages WHERE message_id=701").fetchone()[0] == 0
        cursor = conn.execute("SELECT value FROM meta WHERE key='poll_offset'").fetchone()
        assert cursor is None or int(cursor[0]) <= 701
        conn.execute("DROP TRIGGER crash_message")

    async def restart():
        clock, transport = FakeClock(), FakeTransport()
        bot, store = make_bot(db, clock, transport, ScriptedAgent(lambda prompt: "answer to original request"))
        update = message(701, "@alma_bot original request")
        class Poll:
            async def get_updates(self, *, offset, timeout):
                return [update]
        bot.client.get_updates = Poll().get_updates
        try:
            await bot.start()
            offset, _ = await bot.poll_once(offset=store.get_update_cursor())
            assert offset == 702 and store.get_update_cursor() == 702
            await bot.poll_once(offset=offset)
            clock.advance(3)
            await bot.step([])
            with sqlite3.connect(db) as conn:
                assert conn.execute("SELECT COUNT(*) FROM updates WHERE update_id=701").fetchone()[0] == 1
                assert conn.execute("SELECT COUNT(*) FROM messages WHERE message_id=701 AND is_bot=0").fetchone()[0] == 1
            assert len(transport.sends) == 1
        finally:
            await bot.stop()
            store.close()
    asyncio.run(restart())


def test_live_debounce_fires_while_next_long_poll_is_blocked(tmp_path, monkeypatch):
    from app import main

    async def scenario():
        second_poll, release_poll, model_started = (asyncio.Event() for _ in range(3))
        stores = []
        class TrackedStore(Store):
            def __init__(self, path):
                super().__init__(path)
                stores.append(self)
        class Agent:
            async def start(self): pass
            async def stop(self): pass
            async def observe(self, observation, *, tag, handler):
                # The model is the thing that must start early; speaking is
                # irrelevant here, so finish without a message.
                model_started.set()
                return AgentResult(activation_id=tag, text=None, status="committed")
        class Network:
            def __init__(self, bot): self.calls = 0
            async def call(self, method, payload):
                if method == "getMe":
                    return {"ok": True, "result": {"id": fixtures.ALMA, "username": "alma_bot"}}
                if method == "getUpdates":
                    self.calls += 1
                    if self.calls == 1:
                        return {"ok": True, "result": [message(711, "@alma_bot question")]}
                    second_poll.set()
                    await release_poll.wait()
                    return {"ok": True, "result": []}
                return {"ok": True, "result": {"message_id": 9001}}
        monkeypatch.setattr(main, "Store", TrackedStore)
        monkeypatch.setattr(main, "Bot", lambda token: object())
        monkeypatch.setattr(main, "PythonTelegramBotTransport", Network)
        monkeypatch.setattr(main, "build_agent_factory", lambda *a, **k: lambda spec: Agent())
        config = AppConfig(telegram_token="fake", allowed_chat_ids=(fixtures.TEST_CHAT,),
                           bot_username="alma_bot", debounce_seconds=0.05, poll_timeout=30,
                           idle_seconds=0.05, db_path=str(tmp_path / "live.db"),
                           workspace=str(tmp_path), model="scripted",
                           proactive_enabled=False)
        task = asyncio.create_task(main.run_live(config, load_default_profile()))
        try:
            await asyncio.wait_for(second_poll.wait(), 2)
            assert not release_poll.is_set()
            await asyncio.wait_for(model_started.wait(), 1.5)
            assert not release_poll.is_set(), "model must start before long poll returns"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for store in stores:
                store.close()
    asyncio.run(scenario())


def test_explicit_request_survives_same_topic_supplement_during_model(tmp_path):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        prompts = []
        class Agent:
            async def start(self): pass
            async def stop(self): pass
            async def ask(self, *, chat_id, prompt, tag):
                prompts.append(prompt)
                entered.set()
                await release.wait()
                return AgentResult(activation_id=tag, text="answer to setup question", status="committed")
        clock, transport = FakeClock(), FakeTransport()
        bot, store = make_bot(tmp_path / "supplement.db", clock, transport, Agent())
        try:
            await bot.start()
            await bot.step([message(721, "@alma_bot how do I set up the tool?")])
            clock.advance(3)
            bot.schedule_turns()
            await asyncio.wait_for(entered.wait(), 2)
            await bot.step([message(722, "补充：我用 Windows")])
            clock.advance(3)
            bot.schedule_turns()
            release.set()
            await asyncio.wait_for(asyncio.gather(*list(bot._workers)), 3)
            assert prompts and "how do I set up the tool?" in prompts[0]
            assert len(transport.sends) == 1, "explicit request must receive one reply despite supplement"
            assert transport.sends[0]["text"] == "answer to setup question"
            assert transport.sends[0]["message_thread_id"] == fixtures.TOPIC_A
        finally:
            release.set()
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def prepare_competing_intent(db):
    store = Store(db)
    try:
        store.open_turn(turn_id="turn", chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A,
                        first_message=731, last_message=731, trigger="mention")
        store.record_decision(decision_id="decision", chat_id=fixtures.TEST_CHAT,
                              topic_id=fixtures.TOPIC_A, turn_id="turn", outcome="reply",
                              reason="mention", detail={}, trigger="mention")
        store.prepare_send(decision_id="decision", chat_id=fixtures.TEST_CHAT,
                           topic_id=fixtures.TOPIC_A, reply_to=731, text="answer")
    finally:
        store.close()


def race_store_operations(db, operations):
    barrier = threading.Barrier(len(operations), timeout=20)

    def worker(operation):
        store = None
        try:
            store = Store(db)
            barrier.wait()
            return operation(store)
        except BaseException:
            barrier.abort()
            raise
        finally:
            if store is not None:
                store.close()

    with ThreadPoolExecutor(max_workers=len(operations)) as executor:
        futures = [executor.submit(worker, operation) for operation in operations]
        return [future.result(timeout=40) for future in futures]


def test_concurrent_claims_only_one_succeeds(tmp_path):
    db = tmp_path / "concurrent-claims.db"
    prepare_competing_intent(db)
    claims = race_store_operations(db, [lambda store: store.mark_sending("decision")] * 2)
    assert sorted(claims) == [False, True]
    store = Store(db)
    try:
        send = store.get_send("decision")
        assert send.status == "sending"
        assert send.attempts == 1
    finally:
        store.close()


def test_concurrent_supersede_and_claim_are_exclusive(tmp_path):
    db = tmp_path / "concurrent-supersede.db"
    prepare_competing_intent(db)
    superseded, claimed = race_store_operations(db, [
        lambda store: store.supersede("decision", "replacement"),
        lambda store: store.mark_sending("decision"),
    ])
    assert (superseded, claimed) in {(True, False), (False, True)}
    store = Store(db)
    try:
        send = store.get_send("decision")
        assert store.is_superseded("decision") == superseded
        if superseded:
            assert send.status == "pending" and send.attempts == 0
            assert not store.mark_sending("decision")
        else:
            assert send.status == "sending" and send.attempts == 1
            assert not store.supersede("decision", "replacement")
    finally:
        store.close()


def test_competing_claim_cleanup_preserves_sending_turn_and_restart_unknown(tmp_path):
    db = tmp_path / "claims.db"
    first, second = Store(db), Store(db)
    try:
        first.open_turn(turn_id="turn", chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A,
                        first_message=731, last_message=731, trigger="mention")
        first.record_decision(decision_id="decision", chat_id=fixtures.TEST_CHAT,
                              topic_id=fixtures.TOPIC_A, turn_id="turn", outcome="reply",
                              reason="mention", detail={}, trigger="mention")
        first.prepare_send(decision_id="decision", chat_id=fixtures.TEST_CHAT,
                           topic_id=fixtures.TOPIC_A, reply_to=731, text="answer")
        assert first.mark_sending("decision")
        assert not second.mark_sending("decision")
        second.cancel_pending("decision", "obsolete or not claimable")
        with sqlite3.connect(db) as conn:
            closed, outcome = conn.execute("SELECT closed_at, outcome FROM turns WHERE turn_id='turn'").fetchone()
        assert second.get_send("decision").status == "sending"
    finally:
        first.close()
        second.close()
    restarted = Store(db)
    try:
        restarted.recover_orphaned_sends()
        assert restarted.get_send("decision").status == "unknown"
        assert closed is None and outcome is None, "a failed competing claim must not close an active turn"
    finally:
        restarted.close()


def test_expired_unprepared_reply_decision_is_not_regenerated_on_restart(tmp_path):
    db = tmp_path / "expired.db"
    store = Store(db)
    store.record_message(StoredMessage(fixtures.TEST_CHAT, 741, fixtures.TOPIC_A, fixtures.ALICE,
                                      "alice", False, "@alma_bot old question", time.time()), update_id=741)
    store.open_turn(turn_id="turn", chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A,
                    first_message=741, last_message=741, trigger="mention")
    store.record_decision(decision_id="decision", chat_id=fixtures.TEST_CHAT,
                          topic_id=fixtures.TOPIC_A, turn_id="turn", outcome="reply",
                          reason="mention", detail={}, trigger="mention")
    store.close()
    # The current send expiry is 86400 seconds. Age the decision and its turn,
    # leaving no send row: a newly prepared send must not reset this deadline.
    with sqlite3.connect(db) as conn:
        old = time.time() - 86400 - 60
        conn.execute("UPDATE decisions SET created_at=? WHERE decision_id='decision'", (old,))
        conn.execute("UPDATE turns SET opened_at=? WHERE turn_id='turn'", (old,))
        assert conn.execute("SELECT COUNT(*) FROM sends").fetchone()[0] == 0

    async def restart():
        prompts, transport = [], FakeTransport()
        def answer(prompt):
            prompts.append(prompt)
            return "answer to old question"
        bot, reopened = make_bot(db, FakeClock(), transport, ScriptedAgent(answer))
        try:
            await bot.start()
            await bot.step([])
            await bot.step([])
            assert not transport.sends, "expired decision must not produce a fresh send"
            send = reopened.get_send("decision")
            assert send is None or send.status not in {"pending", "sending", "sent"}
        finally:
            await bot.stop()
            reopened.close()
    asyncio.run(restart())

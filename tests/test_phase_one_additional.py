"""Durability boundaries beyond the shared black-box acceptance suite."""
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from app import fixtures
from app.store import Store, StoredMessage


def seed(store):
    store.open_turn(turn_id="t", chat_id=fixtures.TEST_CHAT, topic_id=1,
                    first_message=1, last_message=1, trigger="mention")
    store.record_decision(decision_id="d", chat_id=fixtures.TEST_CHAT, topic_id=1,
                          turn_id="t", outcome="reply", reason="mention", detail={}, trigger="mention")
    store.prepare_send(decision_id="d", chat_id=fixtures.TEST_CHAT, topic_id=1,
                       reply_to=1, text="answer")


def test_process_exit_after_claim_becomes_unknown(tmp_path):
    db = tmp_path / "state.db"
    store = Store(db)
    seed(store)
    store.close()
    code = "from app.store import Store; import os,sys; s=Store(sys.argv[1]); assert s.mark_sending('d'); os._exit(77)"
    child = subprocess.run([sys.executable, "-c", code, str(db)], cwd=Path(__file__).resolve().parents[1])
    assert child.returncode == 77
    store = Store(db)
    assert store.recover_orphaned_sends() == ["d"]
    assert store.get_send("d").status == "unknown"
    assert not store.mark_sending("d")
    store.close()


def test_confirmed_delivery_rolls_back_all_local_records_on_history_failure(tmp_path):
    store = Store(tmp_path / "state.db")
    seed(store)
    assert store.mark_sending("d")
    store._conn.execute("CREATE TRIGGER fail_history BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT, 'injected'); END")
    store._conn.commit()
    message = StoredMessage(fixtures.TEST_CHAT, 99, 1, 42, "bot", True, "answer", time.time())
    try:
        store.finish_sent("d", 99, message)
        assert False, "injected failure did not fire"
    except sqlite3.IntegrityError:
        pass
    assert store.get_send("d").status == "sending"
    assert store._query("SELECT closed_at FROM turns WHERE turn_id='t'")[0][0] is None
    store.close()
    store = Store(tmp_path / "state.db")
    assert store.recover_orphaned_sends() == ["d"]
    assert store.get_send("d").status == "unknown"
    store.close()


def test_newer_message_and_expired_pending_cannot_be_claimed(tmp_path):
    store = Store(tmp_path / "state.db")
    seed(store)
    store.record_message(StoredMessage(fixtures.TEST_CHAT, 2, 1, 7, "person", False, "new", time.time()), update_id=2)
    assert store.pending_obsolete("d")
    assert not store.mark_sending("d")
    store._conn.execute("DELETE FROM messages")
    store._conn.execute("UPDATE sends SET created_at = ? WHERE decision_id = 'd'", (time.time() - 86401,))
    store._conn.commit()
    assert store.pending_obsolete("d")
    assert not store.mark_sending("d")
    store.close()


def test_process_exit_after_reply_decision_regenerates_on_restart(tmp_path):
    import asyncio
    from test_phase_one_blackbox_acceptance import make_bot
    from conftest import FakeClock, FakeTransport

    db = tmp_path / "decision.db"
    code = """from app.store import Store, StoredMessage
from app import fixtures
import os, sys
s = Store(sys.argv[1])
s.record_message(StoredMessage(fixtures.TEST_CHAT, 701, fixtures.TOPIC_A, fixtures.ALICE, 'alice', False, '@alma_bot question', 1), update_id=701)
s.open_turn(turn_id='t', chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A, first_message=701, last_message=701, trigger='mention')
s.record_decision(decision_id='d', chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A, turn_id='t', outcome='reply', reason='mention', detail={}, trigger='mention')
os._exit(78)
"""
    child = subprocess.run([sys.executable, "-c", code, str(db)], cwd=Path(__file__).resolve().parents[1])
    assert child.returncode == 78

    async def restart():
        transport, prompts = FakeTransport(), []
        bot, store = make_bot(db, FakeClock(), transport, prompts)
        try:
            await bot.start()
            await bot.step([])
            assert len(transport.sends) == 1
            assert len(prompts) == 1 and 'question' in prompts[0]
            assert store.get_send('d').status == 'sent'
        finally:
            await bot.stop()
            store.close()
    asyncio.run(restart())


def test_scheduled_topics_run_concurrently_without_cross_supersession(tmp_path):
    import asyncio
    from app.agent import AgentResult
    from test_phase_one_blackbox_acceptance import make_bot
    from conftest import FakeClock, FakeTransport

    async def scenario():
        entered = {fixtures.TOPIC_A: asyncio.Event(), fixtures.TOPIC_B: asyncio.Event()}
        release = asyncio.Event()
        clock, transport = FakeClock(), FakeTransport()
        bot, store = make_bot(tmp_path / 'concurrency.db', clock, transport, [])
        class Agent:
            def __init__(self, topic):
                self.topic = topic
            async def start(self):
                pass
            async def stop(self):
                pass
            async def ask(self, *, chat_id, prompt, tag):
                entered[self.topic].set()
                await release.wait()
                return AgentResult(activation_id=tag, text='answer', status='committed')
        bot._agent_factory = lambda spec: Agent(fixtures.TOPIC_A if str(spec.workspace).endswith(f'topic_{fixtures.TOPIC_A}') else fixtures.TOPIC_B)
        try:
            await bot.start()
            for n, topic in enumerate((fixtures.TOPIC_A, fixtures.TOPIC_B), 1):
                bot._ingest(fixtures.message(update_id=n, message_id=n, sender_id=fixtures.ALICE,
                                            username='alice', topic_id=topic, text='@alma_bot question'),
                            __import__('app.bot', fromlist=['TickStats']).TickStats())
            clock.advance(3)
            bot.schedule_turns()
            await asyncio.wait_for(asyncio.gather(*(e.wait() for e in entered.values())), 3)
            release.set()
            await asyncio.wait_for(asyncio.gather(*list(bot._workers)), 3)
            assert len(transport.sends) == 2
            assert all(row['superseded_by'] is None for row in store.recent_decisions(fixtures.TEST_CHAT))
        finally:
            release.set()
            await bot.stop()
            store.close()
    asyncio.run(scenario())

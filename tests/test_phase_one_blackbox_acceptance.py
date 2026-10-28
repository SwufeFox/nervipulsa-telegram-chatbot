"""Black-box acceptance checks using isolated storage and fake Telegram/model.

The subprocess deliberately exits without cleanup to exercise SQLite recovery.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fixtures
from app.agent import ScriptedAgent
from app.bot import GroupAgent
from app.config import AppConfig, load_default_profile
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager
from conftest import FakeClock, FakeTransport


def updates():
    return [
        fixtures.message(update_id=701, message_id=701, sender_id=fixtures.ALICE,
                         username="alice", topic_id=fixtures.TOPIC_A,
                         text="@alma_bot please answer first detail"),
        fixtures.message(update_id=702, message_id=702, sender_id=fixtures.BOB,
                         username="bob", topic_id=fixtures.TOPIC_A, text="second detail"),
    ]


def make_bot(db, clock, transport, prompts):
    store = Store(db)
    config = AppConfig(bot_name="Alma", bot_username="alma_bot",
                       allowed_chat_ids=(fixtures.TEST_CHAT,), dry_run=False,
                       proactive_enabled=False, debounce_seconds=2.0,
                       workspace=str(db.parent), model="scripted")
    def respond(prompt):
        prompts.append(prompt)
        return "The answer is forty two."
    agent = ScriptedAgent(respond)
    bot = GroupAgent(legacy_policy=True,
        config=config, store=store, client=TelegramClient(transport),
                     system_prompt=load_default_profile(), agent_factory=lambda spec: agent,
                     turn_manager=TurnManager(debounce_seconds=2.0, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)
    return bot, store


def test_normal_multi_message_turn(tmp_path):
    async def scenario():
        clock, transport, prompts = FakeClock(), FakeTransport(), []
        bot, store = make_bot(tmp_path / "bot.sqlite3", clock, transport, prompts)
        try:
            await bot.start()
            await bot.step(updates())
            assert not transport.sends
            clock.advance(3)
            await bot.step([])
            assert len(transport.sends) == 1
            assert len(prompts) == 1
            assert "first detail" in prompts[0] and "second detail" in prompts[0]
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def test_killed_process_recovers_open_turn_and_deduplicates_redelivery(tmp_path):
    db = tmp_path / "bot.sqlite3"
    marker = tmp_path / "ingested"
    child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(db), str(marker)],
                             cwd=str(Path(__file__).resolve().parents[1]))
    try:
        child.wait(timeout=20)
        assert child.returncode == 73
        assert marker.read_text() == "ingested"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    async def restart():
        clock, transport, prompts = FakeClock(), FakeTransport(), []
        bot, store = make_bot(db, clock, transport, prompts)
        try:
            await bot.start()
            await bot.step(updates())
            clock.advance(3)
            await bot.step([])
            assert store.stats()["messages"] >= 2
            history = store.recent_messages(fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A)
            assert sum(m.message_id == 701 for m in history) == 1
            assert sum(m.message_id == 702 for m in history) == 1
            with sqlite3.connect(db) as conn:
                turns = conn.execute("SELECT turn_id, first_message, last_message, trigger FROM turns").fetchall()
                decisions = conn.execute("SELECT turn_id, trigger, outcome FROM decisions").fetchall()
            assert len(turns) == 1
            assert turns[0][1:] == (701, 702, "mention")
            assert decisions == [(turns[0][0], "mention", "reply")]
            assert len(transport.sends) == 1
            assert len(prompts) == 1
            assert "first detail" in prompts[0] and "second detail" in prompts[0]
        finally:
            await bot.stop()
            store.close()
    asyncio.run(restart())


def test_message_write_failure_does_not_consume_update(tmp_path):
    db = tmp_path / "bot.sqlite3"
    async def scenario():
        clock, transport, prompts = FakeClock(), FakeTransport(), []
        bot, store = make_bot(db, clock, transport, prompts)
        await bot.start()
        with sqlite3.connect(db) as connection:
            connection.execute("CREATE TRIGGER fail_message BEFORE INSERT ON messages "
                               "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
        try:
            try:
                await bot.step([updates()[0]])
            except sqlite3.DatabaseError:
                pass  # The failure is intentional; redelivery below is the assertion.
        finally:
            await bot.stop()
            store.close()
        with sqlite3.connect(db) as connection:
            connection.execute("DROP TRIGGER fail_message")
        bot, store = make_bot(db, clock, transport, prompts)
        try:
            await bot.start()
            await bot.step([updates()[0]])
            clock.advance(3)
            await bot.step([])
            history = store.recent_messages(fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A)
            assert sum(m.message_id == 701 for m in history) == 1
            assert len(transport.sends) == 1
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def test_debounce_uses_elapsed_clock_not_wall_clock(tmp_path):
    async def scenario():
        wall, elapsed = FakeClock(), FakeClock(100.0)
        transport, prompts = FakeTransport(), []
        bot, store = make_bot(tmp_path / "bot.sqlite3", wall, transport, prompts)
        bot.turns = TurnManager(debounce_seconds=2.0, clock=elapsed)
        try:
            await bot.start()
            await bot.step([updates()[0]])
            wall.advance(3600)
            elapsed.advance(1)
            await bot.step([])
            assert not transport.sends
            wall.advance(-7200)
            elapsed.advance(2)
            await bot.step([])
            assert len(transport.sends) == 1
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def test_successful_send_is_reflected_once_in_history(tmp_path):
    async def scenario():
        clock, transport, prompts = FakeClock(), FakeTransport(), []
        bot, store = make_bot(tmp_path / "bot.sqlite3", clock, transport, prompts)
        try:
            await bot.start()
            await bot.step([updates()[0]])
            clock.advance(3)
            await bot.step([])
            assert len(transport.sends) == 1
            history = store.recent_messages(fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A)
            sent = [m for m in history if m.message_id == transport.next_message_id]
            assert len(sent) == 1
            assert sent[0].text == transport.sends[0]["text"]
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def test_live_polling_ingests_while_model_is_blocked(tmp_path, monkeypatch):
    from app import main
    from app.agent import AgentResult

    async def scenario():
        entered, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        stores = []
        class TrackedStore(Store):
            def __init__(self, path):
                super().__init__(path)
                stores.append(self)
        class BlockingAgent:
            async def start(self):
                pass
            async def stop(self):
                pass
            async def ask(self, *, chat_id, prompt, tag):
                entered.set()
                await release.wait()
                return AgentResult(activation_id=tag, text="answer", status="committed")
        class Network:
            def __init__(self, bot):
                self.calls = 0
            async def call(self, method, payload):
                if method == "getMe":
                    return {"ok": True, "result": {"id": fixtures.ALMA, "username": "alma_bot"}}
                if method == "getUpdates":
                    self.calls += 1
                    if self.calls == 1:
                        return {"ok": True, "result": [updates()[0]]}
                    if entered.is_set() and not delivered.is_set():
                        delivered.set()
                        return {"ok": True, "result": [fixtures.message(
                            update_id=703, message_id=703, sender_id=fixtures.ALICE,
                            username="alice", topic_id=fixtures.TOPIC_B, text="another topic")]}
                    await asyncio.sleep(0.01)
                    return {"ok": True, "result": []}
                return {"ok": True, "result": {"message_id": 9001}}
        monkeypatch.setattr(main, "Store", TrackedStore)
        monkeypatch.setattr(main, "Bot", lambda token: object())
        monkeypatch.setattr(main, "PythonTelegramBotTransport", Network)
        monkeypatch.setattr(main, "build_agent_factory", lambda *a, **k: lambda spec: BlockingAgent())
        config = AppConfig(telegram_token="fake", allowed_chat_ids=(fixtures.TEST_CHAT,),
                           bot_username="alma_bot", debounce_seconds=0,
                           db_path=str(tmp_path / "live.sqlite3"), workspace=str(tmp_path),
                           model="scripted", proactive_enabled=False)
        task = asyncio.create_task(main.run_live(config, load_default_profile()))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.wait_for(delivered.wait(), 1)
            # Delivery alone is insufficient: the update must be durably ingested.
            for _ in range(100):
                if any(m.message_id == 703 for m in stores[0].recent_messages(
                        fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_B)):
                    break
                await asyncio.sleep(0.01)
            assert any(m.message_id == 703 for m in stores[0].recent_messages(
                fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_B))
        finally:
            release.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            for store in stores:
                store.close()
    asyncio.run(scenario())


def test_live_same_batch_topics_send_independently(tmp_path, monkeypatch):
    from app import main
    from app.agent import AgentResult

    async def scenario():
        entered_a, release_a, sent_b, sent_a = (asyncio.Event() for _ in range(4))
        prompts, sends, batches, stores = {}, [], [], []

        class TrackedStore(Store):
            def __init__(self, path):
                super().__init__(path)
                stores.append(self)

        class Agent:
            async def start(self):
                pass

            async def stop(self):
                pass

            async def observe(self, observation, *, tag, handler):
                """Speak only through the action tool, as production does."""
                from nervipulsa.ports import ToolCall

                rendered = json.dumps(observation, ensure_ascii=False)
                topic = "A" if "alpha-only-marker" in rendered else "B"
                prompts[topic] = rendered
                if topic == "A":
                    entered_a.set()
                    await release_a.wait()
                await handler(
                    "send_message", {"text": f"answer {topic}"},
                    None, None, ToolCall(id=f"c-{topic}", name="send_message",
                                         arguments={"text": f"answer {topic}"}),
                )
                return AgentResult(activation_id=tag, text=None, status="committed")

        class Network:
            def __init__(self, bot):
                self.calls = 0

            async def call(self, method, payload):
                if method == "getMe":
                    return {"ok": True, "result": {"id": fixtures.ALMA, "username": "alma_bot"}}
                if method == "getUpdates":
                    self.calls += 1
                    if self.calls == 1:
                        batch = [fixtures.message(update_id=800 + i, message_id=800 + i,
                                  sender_id=fixtures.ALICE, username="alice", topic_id=topic,
                                  text=f"@alma_bot {marker}")
                                 for i, (topic, marker) in enumerate([
                                     (fixtures.TOPIC_A, "alpha-only-marker"),
                                     (fixtures.TOPIC_B, "beta-only-marker")])]
                        batches.append(batch)
                        return {"ok": True, "result": batch}
                    await asyncio.sleep(0.01)
                    return {"ok": True, "result": []}
                if method == "sendMessage":
                    sends.append(dict(payload))
                    if payload["text"] == "answer B":
                        sent_b.set()
                    if payload["text"] == "answer A":
                        sent_a.set()
                    return {"ok": True, "result": {"message_id": 9000 + len(sends)}}
                return {"ok": True, "result": True}

        monkeypatch.setattr(main, "Store", TrackedStore)
        monkeypatch.setattr(main, "Bot", lambda token: object())
        monkeypatch.setattr(main, "PythonTelegramBotTransport", Network)
        monkeypatch.setattr(main, "build_agent_factory", lambda *a, **k: lambda spec: Agent())
        config = AppConfig(telegram_token="fake", allowed_chat_ids=(fixtures.TEST_CHAT,),
                           bot_username="alma_bot", debounce_seconds=0,
                           db_path=str(tmp_path / "topics.sqlite3"), workspace=str(tmp_path),
                           model="scripted", proactive_enabled=False,
                           min_action_interval_seconds=0)
        task = asyncio.create_task(main.run_live(config, load_default_profile()))
        try:
            await asyncio.wait_for(entered_a.wait(), 3)
            await asyncio.wait_for(sent_b.wait(), 3)
            assert not release_a.is_set() and not sent_a.is_set()
            assert len(batches) == 1 and len(batches[0]) == 2
            assert [(s["message_thread_id"], s["text"]) for s in sends] == [
                (fixtures.TOPIC_B, "answer B")]
            assert "alpha-only-marker" in prompts["A"] and "beta-only-marker" not in prompts["A"]
            assert "beta-only-marker" in prompts["B"] and "alpha-only-marker" not in prompts["B"]
            release_a.set()
            await asyncio.wait_for(sent_a.wait(), 3)
            assert [(s["message_thread_id"], s["text"]) for s in sends] == [
                (fixtures.TOPIC_B, "answer B"), (fixtures.TOPIC_A, "answer A")]
        finally:
            release_a.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            for store in stores:
                store.close()
    asyncio.run(scenario())


def test_live_model_concurrency_is_bounded_across_topics(tmp_path, monkeypatch):
    from app import main
    from app.agent import AgentResult

    async def scenario():
        limit = 4  # GroupAgent's established worker limit.
        release_one, release_all, saturated, extra_entered, all_entered = (
            asyncio.Event() for _ in range(5))
        active = peak = 0
        entered = []
        stores = []

        class TrackedStore(Store):
            def __init__(self, path):
                super().__init__(path)
                stores.append(self)

        class Agent:
            async def start(self):
                pass

            async def stop(self):
                pass

            async def ask(self, *, chat_id, prompt, tag):
                nonlocal active, peak
                index = next(i for i in range(limit + 1) if f"topic-marker-{i}!" in prompt)
                entered.append(index)
                active += 1
                peak = max(peak, active)
                if len(entered) == limit:
                    saturated.set()
                if len(entered) > limit:
                    extra_entered.set()
                if len(entered) == limit + 1:
                    all_entered.set()
                try:
                    if index == entered[0]:
                        await release_one.wait()
                    else:
                        await release_all.wait()
                    return AgentResult(activation_id=tag, text=f"answer {index}", status="committed")
                finally:
                    active -= 1

        class Network:
            def __init__(self, bot):
                self.calls = 0

            async def call(self, method, payload):
                if method == "getMe":
                    return {"ok": True, "result": {"id": fixtures.ALMA, "username": "alma_bot"}}
                if method == "getUpdates":
                    self.calls += 1
                    if self.calls == 1:
                        return {"ok": True, "result": [fixtures.message(
                            update_id=1000 + i, message_id=1000 + i, sender_id=fixtures.ALICE,
                            username="alice", topic_id=100 + i,
                            text=f"@alma_bot topic-marker-{i}!") for i in range(limit + 1)]}
                    await asyncio.sleep(0.01)
                    return {"ok": True, "result": []}
                return {"ok": True, "result": {"message_id": 2000}}

        monkeypatch.setattr(main, "Store", TrackedStore)
        monkeypatch.setattr(main, "Bot", lambda token: object())
        monkeypatch.setattr(main, "PythonTelegramBotTransport", Network)
        monkeypatch.setattr(main, "build_agent_factory", lambda *a, **k: lambda spec: Agent())
        config = AppConfig(telegram_token="fake", allowed_chat_ids=(fixtures.TEST_CHAT,),
                           bot_username="alma_bot", debounce_seconds=0,
                           db_path=str(tmp_path / "limit.sqlite3"), workspace=str(tmp_path),
                           model="scripted", proactive_enabled=False)
        task = asyncio.create_task(main.run_live(config, load_default_profile()))
        try:
            await asyncio.wait_for(saturated.wait(), 3)
            assert active == limit and peak == limit and len(set(entered)) == limit
            try:
                await asyncio.wait_for(extra_entered.wait(), 0.3)
            except asyncio.TimeoutError:
                pass
            else:
                raise AssertionError("more than four topic models ran concurrently")
            assert active == limit and peak == limit
            release_one.set()
            await asyncio.wait_for(all_entered.wait(), 3)
            assert len(set(entered)) == limit + 1
            assert peak == limit
        finally:
            release_one.set()
            release_all.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            for store in stores:
                store.close()
    asyncio.run(scenario())


def test_crash_after_open_turn_before_decision_recovers(tmp_path):
    db, marker = tmp_path / "crash.sqlite3", tmp_path / "opened"
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(db), str(marker), "open"],
                           cwd=str(ROOT), timeout=20)
    assert child.returncode == 74
    assert marker.read_text() == "opened"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT first_message, last_message FROM turns").fetchall() == [(701, 702)]
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0

    async def restart():
        clock, transport, prompts = FakeClock(), FakeTransport(), []
        bot, store = make_bot(db, clock, transport, prompts)
        try:
            await bot.start()
            clock.advance(3)
            await bot.step([])
            with sqlite3.connect(db) as conn:
                rows = conn.execute("SELECT t.first_message, t.last_message, d.trigger, d.outcome "
                                    "FROM decisions d JOIN turns t ON t.turn_id = d.turn_id").fetchall()
            assert rows == [(701, 702, "mention", "reply")]
            assert len(transport.sends) == 1
        finally:
            await bot.stop()
            store.close()
    asyncio.run(restart())


class DurableTransport(FakeTransport):
    """Persist each request before responding, including across hard exits."""
    def __init__(self, journal, crash=False):
        super().__init__()
        self.journal = Path(journal)
        self.crash = crash

    def call(self, method, payload):
        if method == "sendMessage":
            with self.journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if self.crash:
                os._exit(75)
        return super().call(method, payload)


def send_snapshot(db):
    with sqlite3.connect(db) as conn:
        messages = conn.execute(
            "SELECT message_id, is_bot, text FROM messages ORDER BY message_id").fetchall()
        turns = conn.execute(
            "SELECT turn_id, first_message, last_message, closed_at, outcome FROM turns").fetchall()
        decisions = conn.execute("SELECT decision_id, turn_id, outcome FROM decisions").fetchall()
        sends = conn.execute(
            "SELECT decision_id, status, attempts, telegram_message_id, text, created_at FROM sends").fetchall()
    return messages, turns, decisions, sends


def network_requests(journal):
    if not journal.exists():
        return []
    return [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("boundary,expired", [
    ("prepared", False), ("prepared", True), ("network", False), ("finished", False),
])
def test_hard_exit_send_recovery(tmp_path, monkeypatch, boundary, expired):
    from app import store as store_module

    db = tmp_path / "crash.sqlite3"
    journal = tmp_path / "network.jsonl"
    wall = tmp_path / "wall.txt"
    wall.write_text("1700000000", encoding="utf-8")
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(db), str(journal), "send-" + boundary],
        cwd=str(ROOT), timeout=20)
    assert child.returncode == {"prepared": 76, "network": 75, "finished": 77}[boundary]

    messages, turns, decisions, sends = send_snapshot(db)
    assert [(m[0], m[1]) for m in messages] == (
        [(701, 0), (702, 0), (9001, 1)] if boundary == "finished" else [(701, 0), (702, 0)])
    assert len(turns) == len(decisions) == len(sends) == 1
    assert turns[0][1:3] == (701, 702)
    assert decisions[0][1:] == (turns[0][0], "reply")
    assert sends[0][:4] == (decisions[0][0],
        {"prepared": "pending", "network": "sending", "finished": "sent"}[boundary],
        0 if boundary == "prepared" else 1, 9001 if boundary == "finished" else None)
    # The send timestamp comes from the same injected clock the harness drives
    # (it starts at 1700000000 and has advanced by the time the send row is
    # written), so assert the text and that the timestamp is on that timeline
    # rather than pinning one instant: the store no longer writes wall-clock
    # time behind the scheduler's back.
    assert sends[0][4] == "The answer is forty two."
    assert 1700000000.0 <= sends[0][5] < 1700000100.0
    assert (turns[0][3] is not None, turns[0][4]) == (
        (True, "sent") if boundary == "finished" else (False, None))
    requests = network_requests(journal)
    assert len(requests) == (0 if boundary == "prepared" else 1)
    if requests:
        assert requests[0]["text"] == sends[0][4]
        assert requests[0]["chat_id"] == fixtures.TEST_CHAT
    if boundary == "finished":
        assert messages[-1][2] == sends[0][4]

    if expired:
        wall.write_text(str(1700000000 + 86401), encoding="utf-8")
    monkeypatch.setattr(store_module, "_now", lambda: float(wall.read_text(encoding="utf-8")))

    async def restart():
        transport, prompts = DurableTransport(journal), []
        bot, store = make_bot(db, FakeClock(), transport, prompts)
        try:
            await bot.start()
            await bot.step(updates())
            await bot.step([])
            await bot.step([])
            after_messages, after_turns, after_decisions, after_sends = send_snapshot(db)
            assert after_decisions == decisions
            assert len(after_turns) == len(after_sends) == 1
            assert after_turns[0][:3] == turns[0][:3]
            expected_status = "failed" if expired else ("unknown" if boundary == "network" else "sent")
            assert after_sends[0][:4] == (sends[0][0], expected_status,
                0 if expired else 1, 9001 if expected_status == "sent" else None)
            assert after_sends[0][4:] == sends[0][4:]
            assert [(m[0], m[1]) for m in after_messages] == (
                [(701, 0), (702, 0), (9001, 1)] if expected_status == "sent" else [(701, 0), (702, 0)])
            if expected_status == "sent":
                assert after_messages[-1][2] == sends[0][4]
                assert after_turns[0][3] is not None and after_turns[0][4] == "sent"
            if expired:
                assert after_turns[0][3] is not None and after_turns[0][4] == "superseded"
            if expected_status == "unknown":
                assert after_turns[0][3:] == turns[0][3:]
            assert after_messages[:2] == messages[:2]
            requests_after = network_requests(journal)
            assert len(requests_after) == (0 if expired else 1)
            if requests_after:
                assert requests_after[0]["text"] == sends[0][4]
                assert requests_after[0]["chat_id"] == fixtures.TEST_CHAT
            assert len(transport.sends) == (1 if boundary == "prepared" and not expired else 0)
            assert not prompts
        finally:
            await bot.stop()
            store.close()
    asyncio.run(restart())


def seed_send(db, status="pending", stale=False):
    store = Store(db)
    store.open_turn(turn_id="turn", chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A,
                    first_message=701, last_message=702, trigger="mention")
    store.record_decision(decision_id="decision", chat_id=fixtures.TEST_CHAT,
                          topic_id=fixtures.TOPIC_A, turn_id="turn", outcome="reply",
                          reason="mention", detail={}, trigger="mention")
    store.prepare_send(decision_id="decision", chat_id=fixtures.TEST_CHAT,
                       topic_id=fixtures.TOPIC_A, reply_to=702, text="prepared answer")
    if stale:
        assert store.supersede("decision", "newer")
    if status in {"sending", "unknown"}:
        assert store.mark_sending("decision")
    if status == "unknown":
        store.mark_send_unknown("decision", "delivery uncertain")
    store.close()


def test_recovery_delivers_fresh_pending_and_records_confirmed_history(tmp_path):
    db = tmp_path / "pending.sqlite3"
    seed_send(db)
    async def scenario():
        transport = FakeTransport()
        bot, store = make_bot(db, FakeClock(), transport, [])
        try:
            await bot.start()
            await bot.step([])
            assert len(transport.sends) == 1
            assert transport.sends[0]["text"] == "prepared answer"
            send = store.get_send("decision")
            assert send.status == "sent"
            history = store.recent_messages(fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A)
            confirmed = [m for m in history if m.message_id == send.telegram_message_id]
            assert len(confirmed) == 1
            assert confirmed[0].is_bot and confirmed[0].text == "prepared answer"
            await bot.step([])
            assert len(transport.sends) == 1
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def test_recovery_cancels_superseded_pending(tmp_path):
    db = tmp_path / "stale.sqlite3"
    seed_send(db, stale=True)
    async def scenario():
        transport = FakeTransport()
        bot, store = make_bot(db, FakeClock(), transport, [])
        try:
            await bot.start()
            await bot.step([])
            assert not transport.sends
            assert store.get_send("decision").status not in {"pending", "sending", "sent"}
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def test_recovery_never_resends_sending_or_unknown(tmp_path):
    for status in ("sending", "unknown"):
        db = tmp_path / (status + ".sqlite3")
        seed_send(db, status=status)
        async def scenario():
            transport = FakeTransport()
            bot, store = make_bot(db, FakeClock(), transport, [])
            try:
                await bot.start()
                await bot.step([])
                assert not transport.sends
                send = store.get_send("decision")
                assert send.status == "unknown" and send.attempts == 1
            finally:
                await bot.stop()
                store.close()
        asyncio.run(scenario())


def test_superseded_between_version_check_and_claim_is_not_sent(tmp_path, monkeypatch):
    async def scenario():
        clock, transport = FakeClock(), FakeTransport()
        bot, store = make_bot(tmp_path / "race.sqlite3", clock, transport, [])
        original = store.mark_sending
        raced = []
        def supersede_before_claim(decision_id):
            raced.append(decision_id)
            assert store.supersede(decision_id, "newer")
            return original(decision_id)
        monkeypatch.setattr(store, "mark_sending", supersede_before_claim)
        try:
            await bot.start()
            await bot.step([updates()[0]])
            clock.advance(3)
            await bot.step([])
            assert len(raced) == 1
            assert store.is_superseded(raced[0])
            assert not transport.sends
            assert store.get_send(raced[0]).status != "sent"
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def _child(db, marker, mode="ingest"):
    async def ingest():
        clock = FakeClock()
        if mode.startswith("send-"):
            from app import store as store_module
            wall = Path(db).parent / "wall.txt"
            store_module._now = lambda: float(wall.read_text(encoding="utf-8"))
            boundary = mode.removeprefix("send-")
            transport = DurableTransport(marker, crash=boundary == "network")
            bot, store = make_bot(Path(db), clock, transport, [])
            if boundary == "prepared":
                original = store.prepare_send
                def exit_after_prepare(**kwargs):
                    original(**kwargs)
                    os._exit(76)
                store.prepare_send = exit_after_prepare
            elif boundary == "finished":
                original = store.finish_sent
                def exit_after_finish(*args, **kwargs):
                    original(*args, **kwargs)
                    os._exit(77)
                store.finish_sent = exit_after_finish
            await bot.start()
            await bot.step(updates())
            clock.advance(3)
            await bot.step([])
            raise AssertionError(f"send boundary {boundary} was not reached")
        bot, store = make_bot(Path(db), clock, FakeTransport(), [])
        if mode == "open":
            original = store.open_turn
            def exit_after_open(**kwargs):
                original(**kwargs)
                Path(marker).write_text("opened")
                os._exit(74)
            store.open_turn = exit_after_open
        await bot.start()
        await bot.step(updates())
        if mode == "open":
            clock.advance(3)
            await bot.step([])
            raise AssertionError("open_turn was not reached")
        Path(marker).write_text("ingested")
        os._exit(73)
    asyncio.run(ingest())


if __name__ == "__main__":
    _child(*sys.argv[1:])

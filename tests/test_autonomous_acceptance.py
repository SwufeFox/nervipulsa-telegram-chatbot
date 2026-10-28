"""Autonomous participation acceptance contract.

Interface assumptions while the implementation evolves: GroupAgent.start/step/stop
remain the public ingress and recovery entry points; NervipulsaAgent accepts a
ModelBackend; model action tools are named send_message, finish_silently and
wait_for_event, with text and purpose arguments as below. The app's durable
outbox is observable through Store.get_send and the sends table. Durable waits
are assumed to have a SQLite table whose name contains 'wait'. If the agreed
public tool schema changes, adapt this boundary, not the behavioral assertions.
All model and Telegram traffic is fake; the Runtime itself is real.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nervipulsa.config import Settings
from nervipulsa.ports import ModelResponse, ToolCall
from nervipulsa.providers import ScriptedBackend
from nervipulsa.runtime import Runtime

from app import fixtures
from app.agent import NervipulsaAgent, ScriptedAgent
from app.bot import GroupAgent
from app.config import AppConfig, load_default_profile
from app.store import Store
from app.telegram import TelegramClient
from app.turns import TurnManager
from conftest import FakeClock, FakeTransport


def update(mid=701, topic=None, text='A deployment is leaking memory'):
    return fixtures.message(update_id=mid, message_id=mid, sender_id=fixtures.ALICE,
                            username='alice', topic_id=fixtures.TOPIC_A if topic is None else topic,
                            text=text)


def call(name, **arguments):
    return ModelResponse(content='', model='scripted', tool_calls=[
        ToolCall(id='action-1', name=name, arguments=arguments)])


class AgentHandle:
    """The factory creates the agent lazily; tests still need to inspect it."""

    def __init__(self):
        self.agent = None

    @property
    def runtime(self):
        return self.agent.runtime if self.agent is not None else None


def build(db, responder, *, transport=None, scripted=False):
    clock = FakeClock()
    store = Store(db, clock=clock)
    transport = transport or FakeTransport()
    prompt = load_default_profile()
    config = AppConfig(bot_name='Alma', bot_username='alma_bot',
                       allowed_chat_ids=(fixtures.TEST_CHAT,), dry_run=False,
                       proactive_enabled=True, debounce_seconds=2.0,
                       idle_seconds=0.0,
                       workspace=str(db.parent), model='scripted')
    backend = ScriptedBackend(responder) if not scripted else None
    settings = Settings(model='scripted', workspace=str(db.parent / 'runtime'),
                        max_timeout=30, default_timeout=30, context_limit=40000)
    handle = AgentHandle()

    def factory(spec):
        if scripted:
            handle.agent = ScriptedAgent(responder)
        else:
            handle.agent = NervipulsaAgent(
                settings=settings, workspace=spec.workspace, system_prompt=prompt,
                backend=backend, timeout=4,
                tool_catalog=spec.tool_catalog,
                response_commit_hook=spec.response_commit_hook,
            )
        return handle.agent

    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=prompt, agent_factory=factory,
                     turn_manager=TurnManager(debounce_seconds=2, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)
    return bot, store, clock, transport, handle, backend


async def run_input(bot, clock, message):
    """Ingest, let the batch window close, then run one autonomous pass."""
    await bot.step([message])
    clock.advance(3)
    return await bot.autonomous_step()


def send_rows(db):
    """The action outbox is the authority for outbound intents."""
    with sqlite3.connect(db) as con:
        return con.execute('SELECT status, text FROM actions ORDER BY created_at').fetchall()


def test_unmentioned_input_reaches_real_runtime_and_selects_send(tmp_path):
    async def scenario():
        bot, store, clock, transport, agent, backend = build(
            tmp_path / 'app.db', lambda request: call('send_message', text='Check the heap'))
        try:
            await bot.start()
            await run_input(bot, clock, update())
            assert isinstance(agent.runtime, Runtime), 'ordinary input did not start the real Runtime'
            assert backend.requests, 'ordinary input never reached the model'
            assert 'leaking memory' in json.dumps(backend.requests[0].messages)
            names = [x.get('function', {}).get('name') for x in backend.requests[0].tools]
            assert 'send_message' in names
            assert [s['text'] for s in transport.sends] == ['Check the heap']
            assert send_rows(tmp_path / 'app.db') == [('sent', 'Check the heap')]
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def test_mention_can_finish_silently(tmp_path):
    async def scenario():
        bot, store, clock, transport, agent, backend = build(
            tmp_path / 'app.db', lambda request: call('finish_silently'))
        try:
            await bot.start()
            results = await run_input(bot, clock, update(text='@alma_bot already solved'))
            assert backend.requests, 'mention did not reach model'
            assert not transport.sends
            assert results and results[0].state == 'completed', results
            assert results[0].outcome == 'silent', results
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def test_empty_provider_response_is_error_not_no_action(tmp_path):
    async def scenario():
        bot, store, clock, transport, agent, backend = build(
            tmp_path / 'app.db', lambda request: ModelResponse(content='', model='scripted'))
        try:
            await bot.start()
            results = await run_input(bot, clock, update(text='@alma_bot help'))
            assert backend.requests
            assert not transport.sends
            # An empty answer is a provider failure. It must never be recorded
            # as the agent choosing to stay quiet.
            assert results and results[0].state == 'failed', results
            assert results[0].outcome == 'model_activation_failed', results
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def test_wait_survives_restart_and_new_event_reawakens(tmp_path):
    async def scenario():
        db = tmp_path / 'app.db'
        bot, store, clock, transport, agent, backend = build(
            db, lambda request: call('wait_for_event', purpose='await an update'))
        try:
            await bot.start()
            await run_input(bot, clock, update(text='@alma_bot wait for an update'))
            assert backend.requests, 'wait request did not reach model'
            assert not transport.sends
            with sqlite3.connect(db) as con:
                tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'") if 'wait' in row[0].lower()]
                assert tables, 'no durable waiting registry found (assumed table name contains wait)'
                assert any(con.execute('SELECT count(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables), 'wait was not persisted'
        finally:
            await bot.stop()
            store.close()
        bot, store, clock, transport, agent, backend = build(
            db, lambda request: call('send_message', text='I saw the update'))
        try:
            await bot.start()
            await run_input(bot, clock, update(702, text='The update is ready'))
            assert backend.requests, 'new event did not reawaken the persisted wait'
            view = json.dumps([request.messages for request in backend.requests],
                              ensure_ascii=False)
            # The agent must see both the new event and the wait it registered
            # before the restart: a fresh runtime must not lose its own intent.
            assert 'update is ready' in view
            # The wait's purpose is persisted on the row; the model sees the
            # active-wait summary, not necessarily the original turn's text.
            with sqlite3.connect(db) as con:
                purposes = [row[0] for row in con.execute(
                    'SELECT purpose FROM agent_waits ORDER BY created_at')]
            assert purposes == ['await an update'], purposes
            assert [s['text'] for s in transport.sends] == ['I saw the update']
            with sqlite3.connect(db) as con:
                statuses = [row[0] for row in con.execute(
                    'SELECT status FROM agent_waits ORDER BY created_at')]
            assert 'satisfied' in statuses, statuses
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('same_topic', [True, False], ids=['same-topic-refresh', 'other-topic-independent'])
def test_inference_receives_new_input_and_respects_scope(tmp_path, same_topic):
    db_path = tmp_path / 'app.db'
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def respond(request):
            entered.set()
            await release.wait()
            return call('send_message', text='Answer from old observation')
        bot, store, clock, transport, agent, backend = build(tmp_path / 'app.db', respond)
        first = None
        try:
            await bot.start()
            await bot.step([update(text='@alma_bot initial question')])
            clock.advance(3)
            first = asyncio.create_task(bot.autonomous_step())
            await asyncio.wait_for(entered.wait(), 5)
            topic = fixtures.TOPIC_A if same_topic else fixtures.TOPIC_B
            # Ingestion must not block on the in-flight model request.
            second = asyncio.create_task(
                bot.step([update(702, topic, 'New information arrived')]))
            await asyncio.sleep(.1)
            assert any(m.message_id == 702 for m in store.recent_messages(
                fixtures.TEST_CHAT, topic_id=topic)), 'ingress blocked during inference'
            names = [x.get('function', {}).get('name') for x in backend.requests[0].tools]
            assert 'send_message' in names, 'model cannot choose the send tool; stale-action check would be vacuous'
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 8)
            clock.advance(3)
            await bot.autonomous_step()
            # The old action was decided from sequence 1 while 702 was not yet
            # visible. Same topic: it must be cancelled, and only a decision
            # that actually saw 702 may be delivered. Other topic: the old
            # action is still current and must go out.
            with sqlite3.connect(db_path) as con:
                actions = con.execute(
                    'SELECT observed_through, status FROM actions ORDER BY created_at'
                ).fetchall()
            old = [row for row in actions if row[0] == 1]
            assert old, actions
            if same_topic:
                assert all(row[1] == 'cancelled' for row in old), actions
            else:
                assert all(row[1] == 'sent' for row in old), actions
                assert any(s['text'] == 'Answer from old observation'
                           for s in transport.sends), 'unrelated topic invalidated action'
        finally:
            release.set()
            if first and not first.done():
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)
            await bot.stop()
            store.close()
    asyncio.run(scenario())


def test_tool_call_cannot_send_before_outbox_claim(tmp_path):
    db = tmp_path / 'app.db'
    class ObservingTransport(FakeTransport):
        def call(self, method, payload):
            if method == 'sendMessage':
                assert ('sending', payload['text']) in send_rows(db), 'Telegram called without persisted claimed outbox intent'
            return super().call(method, payload)
    async def scenario():
        bot, store, clock, transport, agent, backend = build(
            db, lambda request: call('send_message', text='Durable action'), transport=ObservingTransport())
        try:
            await bot.start()
            await run_input(bot, clock, update(text='@alma_bot send an answer'))
            assert backend.requests
            assert [s['text'] for s in transport.sends] == ['Durable action']
            assert send_rows(db) == [('sent', 'Durable action')]
        finally:
            await bot.stop()
            store.close()
    asyncio.run(scenario())


class CrashTransport(FakeTransport):
    def __init__(self, journal, crash):
        super().__init__()
        self.journal, self.crash = journal, crash

    def call(self, method, payload):
        if method == 'sendMessage':
            with open(self.journal, 'a', encoding='utf-8') as f:
                f.write(json.dumps(payload) + '\n')
                f.flush()
                os.fsync(f.fileno())
            if self.crash:
                os._exit(75)
        return super().call(method, payload)


def test_sending_unknown_hard_exit_is_never_resent(tmp_path):
    """The process dies after Telegram may have accepted the message.

    The restart must mark the action `unknown` and must not call the transport
    again: a duplicate group message is worse than an unconfirmed one.
    """
    db, journal = tmp_path / 'app.db', tmp_path / 'transport.jsonl'
    child = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(db), str(journal)],
        cwd=ROOT, timeout=60, capture_output=True, text=True)
    assert child.returncode == 75, (child.returncode, child.stdout, child.stderr)
    assert send_rows(db) == [('sending', 'crash boundary answer')]
    async def restart():
        transport = CrashTransport(journal, False)
        bot, store, clock, _, agent, _ = build(db, lambda prompt: 'unexpected',
                                              transport=transport)
        try:
            await bot.start()
            clock.advance(3)
            await bot.autonomous_step()
            assert send_rows(db) == [('unknown', 'crash boundary answer')]
            assert not transport.sends
            assert len(journal.read_text(encoding='utf-8').splitlines()) == 1
        finally:
            await bot.stop()
            store.close()
    asyncio.run(restart())


def _child(db, journal):
    async def scenario():
        bot, store, clock, transport, agent, _ = build(
            Path(db), lambda request: call('send_message', text='crash boundary answer'),
            transport=CrashTransport(journal, True))
        await bot.start()
        # The transport exits the process inside sendMessage, so the action is
        # left in `sending` with the request possibly already delivered.
        await run_input(bot, clock, update(text='@alma_bot answer'))
        raise AssertionError('send boundary was not reached')
    asyncio.run(scenario())


if __name__ == '__main__':
    _child(*sys.argv[1:])

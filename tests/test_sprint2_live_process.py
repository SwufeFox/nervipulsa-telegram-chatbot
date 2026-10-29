"""Hard-exit regression for the default live scheduler and observation recovery."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fixtures


def test_live_polling_recovers_blocked_activation_after_process_exit(tmp_path):
    db = tmp_path / 'live.sqlite3'
    journal = tmp_path / 'events.jsonl'
    script = str(Path(__file__).resolve())
    before = subprocess.run([sys.executable, script, 'before', str(db), str(journal)], cwd=ROOT, timeout=30)
    assert before.returncode == 86
    with sqlite3.connect(db) as conn:
        assert conn.execute('SELECT message_id FROM messages WHERE is_bot=0 ORDER BY message_id').fetchall() == [(1201,), (1202,), (1203,)]
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert any(e.get('kind') == 'entered' and e['topic'] == 'A' for e in events)
    assert any(e.get('kind') == 'send' and e['text'] == 'answer B' for e in events)
    assert not any(e.get('kind') == 'send' and e['text'] == 'answer A' for e in events)
    after = subprocess.run([sys.executable, script, 'after', str(db), str(journal)], cwd=ROOT, timeout=30)
    assert after.returncode == 0
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    recovered = [e for e in events if e.get('kind') == 'recovered']
    assert recovered and 'alpha-first' in recovered[0]['observation'] and 'alpha-second' in recovered[0]['observation']
    assert sum(e.get('kind') == 'send' and e.get('text') == 'answer B' for e in events) == 1
    assert sum(e.get('kind') == 'send' and e.get('text') == 'answer A' for e in events) == 1
    assert all('beta-only' not in e['observation'] for e in recovered)


def child(mode, db, journal):
    from app import main
    from app.agent import AgentResult
    from app.config import AppConfig, load_default_profile
    from nervipulsa.ports import ToolCall

    def record(**event):
        with open(journal, 'a', encoding='utf-8') as f:
            f.write(json.dumps(event) + '\n')
            f.flush()
            os.fsync(f.fileno())

    entered = asyncio.Event()
    b_sent = asyncio.Event()
    recovered = asyncio.Event()
    class Agent:
        async def start(self): pass
        async def stop(self): pass
        async def observe(self, observation, *, tag, handler):
            text = json.dumps(observation, ensure_ascii=False)
            topic = 'A' if 'alpha-' in text else 'B'
            if mode == 'before' and topic == 'A':
                record(kind='entered', topic='A')
                entered.set()
                await asyncio.Event().wait()
            if mode == 'after' and topic == 'A':
                record(kind='recovered', observation=text)
                recovered.set()
            await handler('send_message', {'text': 'answer ' + topic}, None, None,
                          ToolCall(id='call-' + topic, name='send_message', arguments={'text': 'answer ' + topic}))
            return AgentResult(activation_id=tag, text=None, status='committed')

    class Network:
        def __init__(self, bot): self.calls = 0
        async def call(self, method, payload):
            if method == 'getMe': return {'ok': True, 'result': {'id': fixtures.ALMA, 'username': 'alma_bot'}}
            if method == 'getUpdates':
                self.calls += 1
                if mode == 'race-restart' and self.calls == 1:
                    record(kind='polling')
                if mode == 'before' and self.calls == 1:
                    return {'ok': True, 'result': [fixtures.message(update_id=1201, message_id=1201, sender_id=fixtures.ALICE, username='alice', topic_id=fixtures.TOPIC_A, text='alpha-first')]}
                if mode == 'before' and self.calls == 2:
                    await entered.wait()
                    return {'ok': True, 'result': [fixtures.message(update_id=1202, message_id=1202, sender_id=fixtures.BOB, username='bob', topic_id=fixtures.TOPIC_A, text='alpha-second'), fixtures.message(update_id=1203, message_id=1203, sender_id=fixtures.ALICE, username='alice', topic_id=fixtures.TOPIC_B, text='beta-only')]}
                if mode == 'before' and b_sent.is_set(): os._exit(86)
                await asyncio.sleep(.01)
                return {'ok': True, 'result': []}
            if method == 'sendMessage':
                record(kind='send', text=payload['text'])
                if payload['text'] == 'answer B': b_sent.set()
                return {'ok': True, 'result': {'message_id': 9001}}
            return {'ok': True, 'result': True}
        async def close(self): pass

    main.Bot = lambda token: object()
    main.PythonTelegramBotTransport = Network
    main.build_agent_factory = lambda *args, **kwargs: lambda spec: Agent()
    config = AppConfig(telegram_token='fake', allowed_chat_ids=(fixtures.TEST_CHAT,), bot_username='alma_bot', db_path=db, workspace=str(Path(db).parent), model='scripted', idle_seconds=0, min_action_interval_seconds=0)

    async def run():
        task = asyncio.create_task(main.run_live(config, load_default_profile()))
        if mode == 'before':
            await asyncio.wait_for(task, 20)
        elif mode == 'race-restart':
            try:
                await asyncio.sleep(1)
                assert not task.done(), 'live application exited before verification'
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        else:
            try:
                await asyncio.wait_for(recovered.wait(), 15)
                await asyncio.sleep(.2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


if __name__ == '__main__':
    child(*sys.argv[1:])

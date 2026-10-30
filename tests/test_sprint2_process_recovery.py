"""Process-level regressions for the phase-one persistence boundaries."""
import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.store import Store
from app import fixtures
from test_phase_one_blackbox_acceptance import make_bot, updates
from conftest import FakeClock, FakeTransport


def run_child(*args):
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), *map(str, args)],
                          cwd=ROOT, timeout=30)


def test_transaction_interrupted_between_update_and_message(tmp_path):
    db = tmp_path / 'inbound.db'
    result = run_child('inbound', db)
    assert result.returncode == 81
    with sqlite3.connect(db) as conn:
        for table in ('updates', 'observation_events', 'messages'):
            assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0

    async def restart():
        transport = FakeTransport()
        bot, store = make_bot(db, FakeClock(), transport, [])
        try:
            await bot.start()
            await bot.step([updates()[0]])
            await bot.step([updates()[0]])
            with sqlite3.connect(db) as conn:
                for table in ('updates', 'observation_events', 'messages'):
                    assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 1
        finally:
            await bot.stop()
            store.close()
    asyncio.run(restart())


def test_two_process_claims_and_restart_after_winner_dies(tmp_path):
    db = tmp_path / 'claims.db'
    store = Store(db)
    assert store.prepare_action(action_id='a', workflow_id=None, chat_id=1, topic_id=1,
                                text='hello', observed_through=0, freshness='strict', max_unobserved=0)
    store.close()
    gate = tmp_path / 'go'
    children = [subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'claim', str(db),
                                  str(gate), str(tmp_path / f'ready{i}'), str(tmp_path / f'result{i}')],
                                 cwd=ROOT) for i in range(2)]
    try:
        deadline = time.monotonic() + 20
        while not all((tmp_path / f'ready{i}').exists() for i in range(2)):
            assert time.monotonic() < deadline
            assert all(p.poll() is None for p in children)
            time.sleep(.01)
        gate.touch()
        for child in children:
            child.wait(timeout=20)
        assert sorted(p.returncode for p in children) == [82, 83]
        assert sorted((tmp_path / f'result{i}').read_text() for i in range(2)) == ['claimed', 'not-claimable']
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()
    # Both claimants exited without cleanup; another process can reopen the database
    # and cannot claim an already-in-flight action a second time.
    result = run_child('verify-claim', db)
    assert result.returncode == 0


def test_send_claim_races_decision_supersession_across_processes(tmp_path):
    db = tmp_path / 'send-race.db'
    store = Store(db)
    store.open_turn(turn_id='t', chat_id=fixtures.TEST_CHAT, topic_id=10, first_message=1, last_message=1, trigger='mention')
    store.record_decision(decision_id='d', chat_id=fixtures.TEST_CHAT, topic_id=10, turn_id='t',
                          outcome='reply', reason='mention', detail={}, trigger='mention')
    store.prepare_send(decision_id='d', chat_id=fixtures.TEST_CHAT, topic_id=10, reply_to=1, text='answer')
    store.close()
    gate = tmp_path / 'gate'
    processes = [subprocess.Popen([sys.executable, str(Path(__file__).resolve()), mode, str(db),
                                   str(gate), str(tmp_path / (mode + '.ready')),
                                   str(tmp_path / (mode + '.result'))], cwd=ROOT)
                 for mode in ('send-claim', 'send-supersede')]
    try:
        deadline = time.monotonic() + 20
        while not all((tmp_path / (mode + '.ready')).exists() for mode in ('send-claim', 'send-supersede')):
            assert time.monotonic() < deadline
            assert all(p.poll() is None for p in processes)
            time.sleep(.01)
        gate.touch()
        for p in processes:
            p.wait(timeout=20)
            assert p.returncode == 84
        results = [(tmp_path / (mode + '.result')).read_text() for mode in ('send-claim', 'send-supersede')]
        assert sorted(results) == ['False', 'True']
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
                p.wait()
    # Restart the actual default live application after both contenders have
    # died. Neither a superseded pending reply nor an ambiguous in-flight
    # request may reach Telegram, even after several polling iterations.
    journal = tmp_path / 'race-events.jsonl'
    script = ROOT / 'tests' / 'test_sprint2_live_process.py'
    result = subprocess.run([sys.executable, str(script), 'race-restart', str(db), str(journal)],
                            cwd=ROOT, timeout=30)
    assert result.returncode == 0
    import json
    events = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()]
    assert any(event['kind'] == 'polling' for event in events)
    assert not any(event['kind'] == 'send' for event in events)
    with sqlite3.connect(db) as conn:
        status, attempts = conn.execute("SELECT status, attempts FROM sends WHERE decision_id='d'").fetchone()
        assert (status, attempts) == (('unknown', 1) if results[0] == 'True' else ('failed', 0))


def test_real_debounce_survives_process_restart(tmp_path):
    db = tmp_path / 'debounce.db'
    assert run_child('debounce-before', db).returncode == 85
    assert run_child('debounce-after', db).returncode == 0


def child(args):
    mode, db, *rest = args
    if mode in ('debounce-before', 'debounce-after'):
        async def debounce():
            transport, prompts = FakeTransport(), []
            bot, store = make_bot(Path(db), time.monotonic, transport, prompts)
            # Persisted timestamps use wall time; the turn manager uses monotonic time.
            bot.clock = time.time
            await bot.start()
            if mode == 'debounce-before':
                await bot.step([updates()[0]])
                await asyncio.sleep(.1)
                await bot.step([])
                assert not transport.sends and not prompts
                os._exit(85)
            await bot.step([updates()[1]])
            await asyncio.sleep(.1)
            await bot.step([])
            assert not transport.sends and not prompts
            await asyncio.sleep(2.1)
            await bot.step([])
            assert len(transport.sends) == 1 and len(prompts) == 1
            assert 'first detail' in prompts[0] and 'second detail' in prompts[0]
            await bot.stop()
            store.close()
        asyncio.run(debounce())
    elif mode == 'inbound':
        async def ingest():
            bot, store = make_bot(Path(db), FakeClock(), FakeTransport(), [])
            await bot.start()
            def trace(sql):
                if sql.startswith('INSERT INTO messages'):
                    os._exit(81)
            store._conn.set_trace_callback(trace)
            await bot.step([updates()[0]])
            raise AssertionError('transaction boundary not reached')
        asyncio.run(ingest())
    elif mode == 'claim':
        gate, ready, result = map(Path, rest)
        store = Store(db)
        ready.touch()
        deadline = time.monotonic() + 20
        while not gate.exists():
            if time.monotonic() > deadline:
                raise TimeoutError('claim gate')
            time.sleep(.01)
        claimed = store.claim_action('a')
        result.write_text(claimed)
        os._exit(82 if claimed == 'claimed' else 83)
    elif mode in ('send-claim', 'send-supersede'):
        gate, ready, result = map(Path, rest)
        store = Store(db)
        ready.touch()
        deadline = time.monotonic() + 20
        while not gate.exists():
            if time.monotonic() > deadline:
                raise TimeoutError('send race gate')
            time.sleep(.01)
        won = store.mark_sending('d') if mode == 'send-claim' else store.supersede('d', 'newer')
        result.write_text(str(won))
        os._exit(84)
    elif mode == 'verify-send-race':
        store = Store(db)
        send = store.get_send('d')
        if send.status == 'sending':
            assert not store.is_superseded('d') and send.attempts == 1
            assert store.recover_orphaned_sends() == ['d']
            assert store.get_send('d').status == 'unknown'
        else:
            assert send.status == 'pending' and send.attempts == 0
            assert store.is_superseded('d')
            assert not store.mark_sending('d')
        store.close()
    elif mode == 'verify-claim':
        store = Store(db)
        assert store.get_action('a')['attempts'] == 1
        assert store.claim_action('a') == 'not-claimable'
        store.close()


if __name__ == '__main__':
    child(sys.argv[1:])

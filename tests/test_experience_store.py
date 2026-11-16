from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from app.memory import MAX_ACTIVE_MEMORIES, MAX_SOURCE_QUOTE_CHARS
from app.store import SCHEMA, Store, StoredMessage


def source(store, update=1, *, chat=10, topic=None, sender=100, text='orchids', message=None, reply=None):
    store.record_inbound_message(
        StoredMessage(chat_id=chat, message_id=update if message is None else message,
                      topic_id=topic, sender_id=sender, sender_label='person', is_bot=False,
                      text=text, created_at=1),
        update_id=update, payload={'reply_to_message_id': reply},
    )
    return store._query('SELECT sequence FROM observation_events WHERE update_id=?', (update,))[0][0]


def remember(store, sequence=1, **changes):
    arguments = dict(memory_id='m', chat_id=10, topic_id=None, summary='orchid afternoon',
                     kind='experience', source_sequences=[sequence], observed_through=sequence)
    arguments.update(changes)
    return store.remember_memory(**arguments)


def action(store, action_id='a', revision=1):
    return store.prepare_action(action_id=action_id, workflow_id=None, chat_id=10, topic_id=None,
                                text='remember this?', observed_through=10000, memory_id='m',
                                memory_revision=revision)


def test_memory_revisions_keep_literal_evidence_and_do_not_mutate_returned_objects(tmp_path):
    store = Store(tmp_path / 'db')
    sequence = source(store, text='x' * (MAX_SOURCE_QUOTE_CHARS + 20))
    initial = remember(store, sequence, person_ids=[100], certainty='observed', tags=['garden'])
    assert initial['revision'] == 1 and initial['person_ids'] == [100]
    assert initial['sources'] == [dict(sequence=sequence, type='message', message_id=1,
        sender_id=100, received_at=initial['sources'][0]['received_at'],
        text='x' * MAX_SOURCE_QUOTE_CHARS, truncated=True)]
    initial['tags'].append('not persisted')
    revised = remember(store, sequence, expected_revision=1, summary='corrected', kind='interest')
    assert revised['revision'] == 2 and revised['created_at'] == initial['created_at']
    assert revised['person_ids'] == [] and revised['tags'] == []
    history = store.memory_history('m', chat_id=10, topic_id=None)
    assert [item['summary'] for item in history] == ['orchid afternoon', 'corrected']
    assert history[0]['tags'] == ['garden']
    assert store.memory_history('m', chat_id=10, topic_id=4) == []
    store.close()


def test_rejection_inside_nested_batch_leaves_valid_work_and_pending_send_intact(tmp_path):
    store = Store(tmp_path / 'db')
    sequence = source(store)
    remember(store, sequence)
    action(store)
    with store.batch():
        with store.batch():
            with pytest.raises(ValueError, match='invalid_person'):
                remember(store, sequence, expected_revision=1, person_ids=[999])
            with pytest.raises(ValueError, match='memory_revision_conflict'):
                remember(store, sequence, expected_revision=99)
            remember(store, sequence, memory_id='other')
    assert store.get_memory('m', chat_id=10, topic_id=None)['revision'] == 1
    assert store.get_action('a')['status'] == 'pending'
    assert len(store.memory_history('m', chat_id=10, topic_id=None)) == 1
    assert store.get_memory('other', chat_id=10, topic_id=None)
    with pytest.raises(RuntimeError):
        with store.batch():
            remember(store, sequence, expected_revision=1, summary='rolled back')
            raise RuntimeError('abort entire batch')
    assert store.get_action('a')['status'] == 'pending'
    assert store.get_memory('m', chat_id=10, topic_id=None)['revision'] == 1
    store.close()


def test_sources_people_and_nullable_scopes_are_enforced_before_writes(tmp_path):
    store = Store(tmp_path / 'db')
    first = source(store)
    other_topic = source(store, 2, topic=4)
    foreign = source(store, 3, chat=20)
    future = source(store, 4)
    for sequence, high in [(other_topic, other_topic), (foreign, foreign), (future, first), (999, 999)]:
        with pytest.raises(ValueError, match='invalid_source'):
            remember(store, sequence, observed_through=high)
    replied = source(store, 5, sender=200, reply=1)
    memory = remember(store, replied, kind='person', person_ids=[100, 200])
    assert memory['person_ids'] == [100, 200]
    with pytest.raises(ValueError, match='unknown_memory'):
        remember(store, foreign, chat_id=20, expected_revision=1)
    assert store.get_memory('m', chat_id=10, topic_id=4) is None
    assert store.get_memory('m', chat_id=20, topic_id=None) is None
    assert not store.recall_memories(10, 4)
    assert len(store.memory_history('m', chat_id=10, topic_id=None)) == 1
    store.close()


def test_literal_retrieval_and_history_filter_before_limiting(tmp_path):
    store = Store(tmp_path / 'db')
    first = source(store, text='ORCHID 100%_garden 中文兴趣')
    remember(store, first, person_ids=[100], tags=['100%_garden'])
    for update in range(2, 25):
        seq = source(store, update, sender=200, text='unrelated')
        remember(store, seq, memory_id=f'new-{update}', summary='unrelated', importance=5)
    assert [item['memory_id'] for item in store.recall_memories(10, None, query='orchid', limit=1)] == ['m']
    assert [item['memory_id'] for item in store.recall_memories(10, None, query='100%_garden', limit=1)] == ['m']
    assert [item['memory_id'] for item in store.recall_memories(10, None, query='兴趣', limit=1)] == []
    assert [item['sequence'] for item in store.read_history(10, None, query='兴趣', observed_through=1000, limit=1)] == [first]
    assert [item['sequence'] for item in store.read_history(10, None, query='100%_garden', observed_through=1000, limit=1)] == [first]
    assert store.read_history(10, None, message_id=1, observed_through=first, limit=1)[0]['sequence'] == first
    assert store.recall_memories(10, None, source_message_id=1, person_id=100, limit=1)[0]['memory_id'] == 'm'
    assert store.recall_memories(10, None, source_message_id=1, person_id=200) == []
    assert store.recall_memories(10, None, observed_through=0) == []
    assert store.read_history(10, None, observed_through=0) == []
    assert store.read_history(10, None, person_id=100, observed_through=1000, limit=1)[0]['sequence'] == first
    assert store.read_history(10, None, message_id=24, observed_through=first) == []
    store.close()


def test_revision_and_source_edit_cancel_only_pending_sends_and_preserve_revisit(tmp_path):
    store = Store(tmp_path / 'db')
    seq = source(store)
    remember(store, seq)
    store.add_wait(wait_id='revisit', chat_id=10, topic_id=None, kind='until', purpose='orchids', due_at=99, memory_id='m')
    action(store, 'in-flight')
    assert store.claim_action('in-flight') == 'claimed'
    action(store, 'pending')
    remember(store, seq, expected_revision=1, summary='updated')
    assert store.get_action('pending')['status'] == 'cancelled'
    assert store.get_action('in-flight')['status'] == 'sending'
    assert store.active_waits(10, None)[0]['memory_id'] == 'm'
    action(store, 'after-revision', revision=2)
    source(store, 2, message=1, text='actually different')
    assert store.get_memory('m', chat_id=10, topic_id=None)['needs_review'] is True
    assert store.get_action('after-revision')['status'] == 'cancelled'
    assert store.get_action('in-flight')['status'] == 'sending'
    assert store.memory_history('m', chat_id=10, topic_id=None)[0]['sources'][0]['text'] == 'orchids'
    with pytest.raises(ValueError, match='memory_needs_review'):
        action(store, 'review-blocked', revision=2)
    assert store.satisfy_event_waits(10, None, 'message', 5) == []
    assert store.matching_waits_for_observations(10, None, [(2, 'message', 100)], 5) == []
    assert store.finish_action('in-flight', 'unknown')
    assert store.forget_memory('m', chat_id=10, topic_id=None, expected_revision=2)
    assert store.active_waits(10, None) == []
    assert store.claim_action('in-flight') == 'not-claimable'
    assert store.get_action('in-flight')['status'] == 'unknown'
    events = store.read_history(10, None, observed_through=1000, limit=10)
    cancelled = [event['payload'] for event in events if event['type'] == 'action_result' and event['payload']['status'] == 'cancelled']
    assert {event['memory_revision'] for event in cancelled} == {1, 2}
    assert all(event['memory_id'] == 'm' for event in cancelled)
    store.close()


def test_expiry_filters_immediately_and_retires_linked_waits_atomically(tmp_path):
    clock = [10.0]
    store = Store(tmp_path / 'db', clock=lambda: clock[0])
    seq = source(store)
    remember(store, seq, expires_at=20)
    action(store)
    store.add_wait(wait_id='w', chat_id=10, topic_id=None, kind='until', purpose='later', due_at=30, memory_id='m')
    clock[0] = 20
    assert store.get_memory('m', chat_id=10, topic_id=None) is None
    assert store.get_memory('m', chat_id=10, topic_id=None, include_inactive=True)['status'] == 'expired'
    assert store.recall_memories(10, None) == []
    assert store.expire_memories() == ['m']
    assert store.expire_memories() == []
    assert store.active_waits(10, None) == []
    assert store.get_action('a')['status'] == 'cancelled'
    assert not store.forget_memory('m', chat_id=10, topic_id=None, expected_revision=1)
    store.close()


def test_claim_rechecks_expiry_without_a_tick(tmp_path):
    store = Store(tmp_path / 'db', clock=lambda: 10)
    seq = source(store)
    remember(store, seq, expires_at=20)
    action(store)
    assert store.claim_action('a', now=20) == 'not-claimable'
    assert store.get_action('a')['status'] == 'cancelled'
    assert store.get_action('a')['error'] == 'unknown_memory'
    store.close()


def test_capacity_rejects_new_records_but_allows_revision(tmp_path):
    store = Store(tmp_path / 'db')
    seq = source(store)
    with store.batch():
        for index in range(MAX_ACTIVE_MEMORIES):
            remember(store, seq, memory_id=f'm-{index}')
        with pytest.raises(ValueError, match='memory_capacity_exceeded'):
            remember(store, seq, memory_id='overflow')
        assert remember(store, seq, memory_id='m-0', expected_revision=1)['revision'] == 2
    assert store.forget_memory('m-1', chat_id=10, topic_id=None, expected_revision=1)
    assert remember(store, seq, memory_id='replacement')['revision'] == 1
    store.close()


def test_durable_mutation_receipt_is_scoped_and_rolls_back_with_memory(tmp_path):
    from dataclasses import dataclass

    @dataclass
    class Call:
        id: str
        name: str
        arguments: dict

    @dataclass
    class Response:
        tool_calls: list

    path = tmp_path / 'db'
    store = Store(path)
    seq = source(store)
    store.commit_autonomous_response(10, None, SimpleNamespace(id='activation'),
                                     Response([Call('call', 'remember', {'summary': 'orchid'})]))
    assert store.get_autonomous_call('activation', 'call', chat_id=10, topic_id=3) is None
    with pytest.raises(RuntimeError):
        with store.batch():
            memory = remember(store, seq)
            store.finish_autonomous_call('activation', 'call', chat_id=10, topic_id=None,
                                         receipt={'memory': memory}, status='completed')
            raise RuntimeError('crash')
    assert store.get_memory('m', chat_id=10, topic_id=None) is None
    assert store.get_autonomous_call('activation', 'call', chat_id=10, topic_id=None)['receipt'] is None
    with store.batch():
        memory = remember(store, seq)
        store.finish_autonomous_call('activation', 'call', chat_id=10, topic_id=None,
                                     receipt={'memory': memory}, status='completed')
    store.close()
    store = Store(path)
    call = store.get_autonomous_call('activation', 'call', chat_id=10, topic_id=None)
    assert call['receipt']['memory']['revision'] == 1 and call['status'] == 'completed'
    with pytest.raises(ValueError, match='unknown_autonomous_call'):
        store.finish_autonomous_call('unknown', 'call', chat_id=10, topic_id=None, receipt={}, status='completed')
    store.close()


def test_concurrent_revision_has_one_winner_with_thread_owned_connections(tmp_path):
    path = tmp_path / 'db'
    store = Store(path)
    seq = source(store)
    remember(store, seq)
    store.close()
    barrier = Barrier(2)

    def revise(summary):
        connection = Store(path)
        try:
            barrier.wait(timeout=10)
            try:
                return remember(connection, seq, expected_revision=1, summary=summary)['revision']
            except ValueError as error:
                return str(error)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(revise, text) for text in ('one', 'two')]
        results = [future.result(timeout=20) for future in futures]
    assert sorted(results, key=str) == [2, 'memory_revision_conflict']
    store = Store(path)
    assert len(store.memory_history('m', chat_id=10, topic_id=None)) == 2
    store.close()


def test_v4_migration_reopen_and_purge_preserve_other_scope(tmp_path):
    path = tmp_path / 'db'
    old = sqlite3.connect(path)
    old.executescript(SCHEMA)
    old.execute("INSERT INTO meta VALUES ('schema_version','4')")
    old.execute("INSERT INTO agent_state VALUES (10,NULL,'old','{\"note\":\"legacy\"}','agent',1,NULL)")
    old.execute("INSERT INTO sends VALUES ('legacy',10,NULL,NULL,'old answer','sent',1,99,NULL,1,1)")
    old.execute("INSERT INTO feedback_events VALUES (50,10,99,'legacy',NULL,'up',1)")
    old.commit()
    old.close()
    store = Store(path)
    assert store._query("SELECT value FROM meta WHERE key='schema_version'")[0][0] == '5'
    assert store.get_state(10, None) == {'old': {'note': 'legacy'}}
    assert store.recall_memories(10, None) == []
    assert store.feedback_report()['items'][0]['decision_id'] == 'legacy'
    first = source(store)
    remember(store, first)
    second = source(store, 2, chat=20)
    remember(store, second, memory_id='foreign', chat_id=20)
    store.open_episode(episode_id='episode', chat_id=10, topic_id=None)
    store.append_synthetic_observation(dedupe_key='private', chat_id=10, topic_id=None, kind='timer', payload={'text': 'private'})
    action(store)
    store.forget_memory('m', chat_id=10, topic_id=None, expected_revision=1)
    store.close()
    store = Store(path)
    assert store.get_memory('m', chat_id=10, topic_id=None, include_inactive=True)['status'] == 'forgotten'
    store.purge_chat(10)
    for table in ('memories', 'memory_revisions', 'observation_events', 'agent_state', 'workflow_episodes', 'autonomous_responses'):
        assert store._query(f'SELECT * FROM {table} WHERE chat_id=10') == []
    assert store._query('SELECT * FROM action_result_events') == []
    assert store._query('SELECT * FROM synthetic_observations') == []
    assert store.memory_history('m', chat_id=10, topic_id=None) == []
    assert store.get_memory('foreign', chat_id=20, topic_id=None)['summary'] == 'orchid afternoon'
    store.close()


@pytest.mark.parametrize('changes,code', [
    ({'importance': True}, 'invalid_memory'),
    ({'expires_at': float('nan')}, 'invalid_memory'),
    ({'source_sequences': [True]}, 'invalid_source'),
    ({'source_sequences': [1, 1]}, 'invalid_source'),
    ({'person_ids': [True]}, 'invalid_person'),
    ({'kind': 'person', 'person_ids': []}, 'invalid_person'),
    ({'tags': ['a', 'a']}, 'invalid_memory'),
])
def test_invalid_memory_inputs_never_write_inside_batch(tmp_path, changes, code):
    store = Store(tmp_path / 'db')
    seq = source(store)
    with store.batch():
        with pytest.raises(ValueError, match=code):
            remember(store, seq, **changes)
        assert store.recall_memories(10, None) == []
        assert store.memory_history('m', chat_id=10, topic_id=None) == []
    store.close()


def test_failed_v5_migration_does_not_publish_version_or_partial_tables(tmp_path):
    path = tmp_path / 'db'
    old = sqlite3.connect(path)
    old.executescript(SCHEMA)
    old.execute("INSERT INTO meta VALUES ('schema_version','4')")
    old.commit()
    old.close()

    class RejectedMigrationStore(Store):
        def _migrate_experience_memory(self):
            def authorize(action, name, column, database, trigger):
                if action == sqlite3.SQLITE_CREATE_TABLE and name == 'memory_revisions':
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            self._conn.set_authorizer(authorize)
            try:
                super()._migrate_experience_memory()
            finally:
                self._conn.close()

    with pytest.raises(sqlite3.DatabaseError):
        RejectedMigrationStore(path)
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '4'
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {'memories', 'memory_sources', 'memory_revisions'} & tables
    assert 'memory_id' not in {row[1] for row in connection.execute('PRAGMA table_info(actions)')}
    connection.close()
    recovered = Store(path)
    assert recovered.recall_memories(10, None) == []
    recovered.close()

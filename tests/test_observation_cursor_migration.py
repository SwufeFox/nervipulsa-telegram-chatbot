"""Crash recovery and concurrency for observation cursor schema upgrades."""
import sqlite3
import subprocess
import sys

import pytest

from app.store import Store


LEGACY = """CREATE TABLE {name} (
    chat_id INTEGER NOT NULL, topic_id INTEGER, last_processed INTEGER NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY (chat_id, topic_id))"""


def seed(path, *, current="legacy", old=True):
    if current == "new":
        Store(path).close()
    with sqlite3.connect(path) as conn:
        if current == "legacy":
            conn.execute(LEGACY.format(name="observation_cursors"))
        if current:
            conn.executemany(
                "INSERT INTO observation_cursors (chat_id, topic_id, last_processed, updated_at) VALUES (?, ?, ?, ?)",
                [(1, None, 8, 8), (1, 2, 3, 3)],
            )
        if old:
            conn.execute(LEGACY.format(name="observation_cursors_old"))
            conn.executemany("INSERT INTO observation_cursors_old VALUES (?, ?, ?, ?)",
                             [(1, None, 5, 5), (1, None, 12, 12), (2, None, 7, 7)])


def assert_recovered(path, expected):
    for _ in range(2):
        store = Store(path)
        try:
            rows = store._conn.execute(
                "SELECT chat_id, topic_id, last_processed FROM observation_cursors"
            ).fetchall()
            assert {(r[0], r[1]): r[2] for r in rows} == expected
            assert store._conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'observation_cursors_old'"
            ).fetchone()[0] == 0
            info = store._conn.execute("PRAGMA table_xinfo(observation_cursors)").fetchall()
            assert any(row['name'] == 'scope_key' and row['hidden'] == 3 for row in info)
        finally:
            store.close()


@pytest.mark.parametrize("current", ["legacy", "new", None])
def test_recovers_both_interrupted_table_shapes(tmp_path, current):
    path = tmp_path / "cursor.db"
    seed(path, current=current)
    expected = {(1, None): 12, (2, None): 7}
    if current:
        expected[(1, 2)] = 3
    assert_recovered(path, expected)


def test_generated_column_is_detected_without_rebuilding(tmp_path):
    path = tmp_path / "cursor.db"
    store = Store(path)
    try:
        conn = store._conn
        assert "scope_key" not in {r['name'] for r in conn.execute("PRAGMA table_info(observation_cursors)")}
        assert "scope_key" in {r['name'] for r in conn.execute("PRAGMA table_xinfo(observation_cursors)")}
        statements = []
        conn.set_trace_callback(statements.append)
        store._migrate_observation_cursors()
        assert not any("DROP TABLE" in sql.upper() or "CREATE TABLE" in sql.upper() for sql in statements)
    finally:
        store.close()


@pytest.mark.parametrize("interrupt_at", ["DROP TABLE observation_cursors", "INSERT INTO observation_cursors", "DROP TABLE observation_cursors_old", "COMMIT"])
def test_process_death_rolls_back_the_entire_migration(tmp_path, interrupt_at):
    path = tmp_path / "cursor.db"
    seed(path)
    code = """
import os, sqlite3, sys
from app.store import Store
conn = sqlite3.connect(sys.argv[1])
conn.row_factory = sqlite3.Row
store = Store.__new__(Store)
store._conn = conn
def trace(sql):
    if sql.lstrip().upper().startswith(sys.argv[2].upper()):
        os._exit(73)
conn.set_trace_callback(trace)
store._migrate_observation_cursors()
"""
    result = subprocess.run([sys.executable, "-c", code, str(path), interrupt_at], timeout=30)
    assert result.returncode == 73
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT count(*) FROM observation_cursors").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM observation_cursors_old").fetchone()[0] == 3
        assert "scope_key" not in {r[1] for r in conn.execute("PRAGMA table_xinfo(observation_cursors)")}
    assert_recovered(path, {(1, None): 12, (1, 2): 3, (2, None): 7})


def test_concurrent_process_startup_serializes_recovery(tmp_path):
    path = tmp_path / "cursor.db"
    gate = tmp_path / "start"
    seed(path)
    code = """
import pathlib, sys, time
from app.store import Store
while not pathlib.Path(sys.argv[2]).exists():
    time.sleep(0.01)
store = Store(sys.argv[1])
store.close()
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(path), str(gate)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(4)]
    try:
        gate.touch()
        for process in processes:
            stdout, stderr = process.communicate(timeout=40)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert_recovered(path, {(1, None): 12, (1, 2): 3, (2, None): 7})

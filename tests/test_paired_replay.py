from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace

from experiments.paired_replay import (
    GROUP_CHAT,
    RunMetrics,
    _absorb,
    _final_state,
    _load_profile_details,
    load_recorded,
)


def test_load_recorded_can_select_historical_sequence_window(tmp_path) -> None:
    database = tmp_path / "observations.sqlite3"
    conn = sqlite3.connect(database)
    conn.execute(
        "CREATE TABLE observation_events ("
        "sequence INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, kind TEXT NOT NULL, "
        "received_at REAL NOT NULL, payload_json TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO observation_events VALUES (?, ?, ?, ?, ?)",
        [
            (1, GROUP_CHAT, "message", 1.0, json.dumps({"message_id": 1})),
            (2, GROUP_CHAT, "action_result", 2.0, json.dumps({"message_id": 2})),
            (3, GROUP_CHAT, "bot_message", 3.0, json.dumps({"message_id": 3})),
            (4, GROUP_CHAT, "message", 4.0, json.dumps({"message_id": 4})),
            (5, GROUP_CHAT + 1, "message", 5.0, json.dumps({"message_id": 5})),
        ],
    )
    conn.commit()
    conn.close()

    selected = load_recorded(database, limit=2, through_sequence=3)

    assert [event.sequence for event in selected] == [1, 3]
    assert [event.kind for event in selected] == ["message", "bot_message"]


def test_profile_details_hash_the_composed_profile(tmp_path) -> None:
    soul = tmp_path / "soul.md"
    introduction = tmp_path / "introduction.md"
    soul.write_text("identity\n", encoding="utf-8")
    introduction.write_text("voice\n", encoding="utf-8")
    config = tmp_path / "fox.json"
    config.write_text(json.dumps({
        "soul_path": str(soul),
        "introduction_path": str(introduction),
    }), encoding="utf-8")

    _, profile, digest = _load_profile_details(config)

    assert "identity" in profile
    assert "voice" in profile
    assert digest == hashlib.sha256(profile.encode("utf-8")).hexdigest()[:12]


def test_absorb_keeps_failed_workflows_visible() -> None:
    metrics = RunMetrics("plain")
    store = SimpleNamespace(_query=lambda query: [])
    results = [
        SimpleNamespace(state="completed", detail={}, outcome="silent", waits=[]),
        SimpleNamespace(state="failed", detail={}, outcome=None, waits=[]),
    ]

    _absorb(metrics, results, store)

    assert metrics.activations == 1
    assert metrics.workflow_states == {"completed": 1, "failed": 1}


def test_final_state_reports_workflow_state_counts() -> None:
    def query(statement: str):
        if "GROUP BY state" in statement:
            return [
                {"state": "completed", "count": 3},
                {"state": "failed", "count": 1},
            ]
        return [(0,)]

    state = _final_state(SimpleNamespace(_query=query), with_episodes=False)

    assert state["workflow_states"] == {"completed": 3, "failed": 1}

import json

from app.store import Store, StoredMessage


def test_observation_receipts_survive_restart_and_purge(tmp_path):
    path = tmp_path / "state.db"
    store = Store(path)
    def message(text):
        return StoredMessage(1, 10, 7, 42, "member", False, text, 100.0)

    assert store.record_inbound_message(message("original"), update_id=1, payload={})
    # A new Telegram update for the same message remains a distinct receipt.
    assert not store.record_inbound_message(message("edited"), update_id=2, payload={})
    assert not store.record_inbound_message(message("edited"), update_id=2, payload={})
    store.close()
    store = Store(path)
    rows = store._query("SELECT * FROM observation_events ORDER BY sequence")
    assert [row["kind"] for row in rows] == ["message", "message_edit"]
    assert [json.loads(row["payload_json"])["text"] for row in rows] == ["original", "edited"]
    assert all(row["topic_id"] == 7 for row in rows)
    store.purge_chat(1)
    assert not store._query("SELECT * FROM observation_events")
    store.close()

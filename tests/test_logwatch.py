"""Coverage for the operational log helpers (synthetic data only)."""
from __future__ import annotations

import logging

import pytest

from app import logwatch
from app.store import Store


def test_scope_label_marks_chat_kind_and_topic():
    assert logwatch.scope(8283457993) == "chat:8283457993(dm)"
    assert logwatch.scope(-1004402809405) == "chat:-1004402809405(group)"
    assert logwatch.scope(-1004402809405, None, "supergroup") == \
        "chat:-1004402809405(supergroup)"
    assert logwatch.scope(-100, 7) == "chat:-100(group)/topic:7"


def test_summary_is_off_by_default_and_bounded_when_on():
    assert logwatch.summary("有人知道这个报错吗", 0) == ""
    assert logwatch.summary("有人知道这个报错吗", -5) == ""

    assert logwatch.summary("第一行\n第二行", 40) == "第一行 第二行"
    assert logwatch.summary("x" * 50, 10) == "xxxxxxxxxx…"


def test_summary_redacts_token_shapes():
    text = "看这个 https://api.telegram.org/bot123456789:AAHfakeTokenValueHere/getMe"
    out = logwatch.summary(text, 200)
    assert "AAHfakeTokenValueHere" not in out
    assert "bot<redacted>" in out

    bare = logwatch.summary("token 123456789:AAHfakeTokenValueHere 在这", 200)
    assert "AAHfakeTokenValueHere" not in bare
    assert "<redacted-token>" in bare


def _seed(store, *, sequences, chat_id, topic_id=None, created_at=None,
          cursor=None):
    with store._write() as conn:
        for sequence in sequences:
            conn.execute(
                "INSERT INTO observation_events (sequence, update_id, chat_id, topic_id,"
                " kind, received_at, payload_json) VALUES (?, ?, ?, ?, 'message', ?, ?)",
                (sequence, sequence, chat_id, topic_id, 1000.0,
                 '{"text": "x", "created_at": %f}' % (created_at or 1000.0)),
            )
    if cursor is not None:
        with store._write() as conn:
            conn.execute(
                "INSERT INTO observation_cursors (chat_id, topic_id, last_processed,"
                " updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(scope_key)"
                " DO UPDATE SET last_processed = excluded.last_processed",
                (chat_id, topic_id, cursor, 1000.0),
            )


def test_status_snapshot_reports_backlog_age_and_running_turn(tmp_path):
    store = Store(tmp_path / "status.db")
    try:
        now = 5000.0
        # Two scopes: a group with a 400-message backlog 43 minutes old, and a
        # private chat that is fully caught up.
        _seed(store, sequences=range(1, 401), chat_id=-100,
              created_at=now - 2580, cursor=0)
        _seed(store, sequences=range(401, 404), chat_id=42, created_at=now - 5,
              cursor=403)
        store.open_workflow(chat_id=-100, topic_id=None, first_sequence=1,
                            last_sequence=24, workflow_id="wf_group")

        snapshot = logwatch.status_snapshot(store, now=now)
        by_chat = {s["chat_id"]: s for s in snapshot["scopes"]}

        assert by_chat[-100]["pending"] == 400
        assert 2570 <= by_chat[-100]["oldest_age"] <= 2590
        assert by_chat[-100]["running"]["workflow_id"] == "wf_group"
        assert by_chat[42]["pending"] == 0
        assert by_chat[42]["running"] is None

        lines = logwatch.status_lines(
            snapshot,
            kinds={-100: "supergroup"},
            limits={"max_activations_per_hour": 60, "max_actions_per_hour": 20},
        )
        assert lines[0].startswith("status: pending=400 running=1")
        assert "activations=1/60" in lines[0]
        assert any("chat:-100(supergroup)" in line and "pending=400" in line
                   and "oldest 43m" in line for line in lines[1:])
        assert any("chat:42(dm)" in line and "pending=0" in line
                   for line in lines[1:])
    finally:
        store.close()


def test_status_snapshot_counts_unsent_actions_and_budgets(tmp_path):
    now = 9000.0
    store = Store(tmp_path / "status2.db", clock=lambda: now)
    try:
        _seed(store, sequences=[1], chat_id=42, created_at=now, cursor=1)
        # `old` was delivered two hours ago, `a1` ten seconds ago; `a2` is
        # still waiting to be sent.
        store.prepare_action(action_id="old", workflow_id=None, chat_id=42,
                             topic_id=None, text="z", observed_through=1)
        with store._write() as conn:
            conn.execute("UPDATE actions SET status='sent', updated_at=? "
                         "WHERE action_id='old'", (now - 7200,))
        store.prepare_action(action_id="a1", workflow_id=None, chat_id=42,
                             topic_id=None, text="x", observed_through=1)
        assert store.claim_action("a1") == "claimed"
        assert store.finish_action("a1", "sent", telegram_message_id=7)
        store.prepare_action(action_id="a2", workflow_id=None, chat_id=42,
                             topic_id=None, text="y", observed_through=1)

        snapshot = logwatch.status_snapshot(store, now=now)
        entry = next(s for s in snapshot["scopes"] if s["chat_id"] == 42)
        assert entry["pending_actions"] == 1, "only a2 is still unsent"
        assert entry["delivered"] == 1, "only the in-window delivery counts"
    finally:
        store.close()


def test_status_snapshot_survives_a_store_failure(caplog):
    """A broken store must not take the status printer - or the bot - down."""
    class Broken:
        def _query(self, *_args, **_kwargs):
            raise RuntimeError("database is locked")

    with pytest.raises(RuntimeError):
        logwatch.status_snapshot(Broken())


def test_ingest_log_shows_scope_and_omits_text_by_default(config, transport, caplog):
    from app import fixtures
    from app.bot import GroupAgent
    from app.telegram import TelegramClient
    from conftest import run

    store = Store(":memory:")
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt="", agent_factory=lambda spec: None)
    update = fixtures.message(update_id=1, message_id=1, sender_id=1,
                              username="synthetic", text="private sentinel text")
    with caplog.at_level(logging.INFO):
        run(bot.step([update], run=False))
    assert "stored chat:-1001234567890(supergroup) message_id=1 sender=synthetic" \
        in caplog.text
    assert "private sentinel text" not in caplog.text
    store.close()


def test_ingest_log_previews_text_when_enabled(config, transport, caplog):
    from app import fixtures
    from app.bot import GroupAgent
    from app.telegram import TelegramClient
    from conftest import run

    config.log_summary_chars = 12
    store = Store(":memory:")
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt="", agent_factory=lambda spec: None)
    update = fixtures.message(update_id=1, message_id=1, sender_id=1,
                              username="synthetic", text="0123456789abcdefghij")
    with caplog.at_level(logging.INFO):
        run(bot.step([update], run=False))
    assert 'text="0123456789ab…"' in caplog.text
    store.close()

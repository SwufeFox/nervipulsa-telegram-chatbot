from __future__ import annotations

import json
import sqlite3

from app import fixtures
from app.bot import GroupAgent
from app.store import Store
from app.telegram import TelegramClient, parse_reaction_update
from experiments.feedback_report import load_report


def reaction(update_id: int, *, chat_id: int, message_id: int,
             old: list[dict] | None, new: list[dict] | None,
             actor_id: int = fixtures.ALICE) -> dict:
    return {
        "update_id": update_id,
        "message_reaction": {
            "chat": {"id": chat_id, "type": "supergroup"},
            "message_id": message_id,
            "date": 1_700_000_000,
            "user": {"id": actor_id},
            "old_reaction": old or [],
            "new_reaction": new or [],
        },
    }


def emoji(value: str) -> dict:
    return {"type": "emoji", "emoji": value}


def sent_reply(store: Store, chat_id: int, message_id: int) -> None:
    store.prepare_send(decision_id="decision-1", chat_id=chat_id, topic_id=4,
                       reply_to=1, text="answer")
    assert store.mark_sending("decision-1")
    store.mark_sent("decision-1", message_id)


def test_parser_extracts_only_thumb_vote_state_and_discards_actor() -> None:
    update = reaction(70, chat_id=fixtures.TEST_CHAT, message_id=9001,
                      old=[emoji("👍")], new=[emoji("👎")], actor_id=987654321)
    parsed = parse_reaction_update(update)
    assert parsed is not None
    assert (parsed.update_id, parsed.chat_id, parsed.message_id) == (
        70, fixtures.TEST_CHAT, 9001,
    )
    assert parsed.old_vote == "up"
    assert parsed.new_vote == "down"
    assert not hasattr(parsed, "actor_id")
    assert not hasattr(parsed, "raw")

    other = reaction(71, chat_id=fixtures.TEST_CHAT, message_id=9001,
                     old=[emoji("❤️")], new=[emoji("🔥")])
    assert parse_reaction_update(other).old_vote is None
    assert parse_reaction_update(other).new_vote is None


def test_poll_path_records_and_dedupes_reaction_changes_without_identity(
    tmp_path, config, transport, system_prompt
):
    store = Store(tmp_path / "db.sqlite3")
    sent_reply(store, fixtures.TEST_CHAT, 9001)
    up = reaction(70, chat_id=fixtures.TEST_CHAT, message_id=9001,
                  old=[], new=[emoji("👍")], actor_id=123456789)
    transport.updates = [[up], [up]]
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=system_prompt, agent_factory=lambda spec: None)

    from conftest import run

    async def poll_twice():
        offset, tick = await bot.poll_once(offset=None)
        assert offset == 71
        assert tick.feedback_updates == 1
        assert tick.messages_stored == 0
        duplicate_offset, duplicate_tick = await bot.poll_once(offset=offset)
        assert duplicate_offset == offset
        assert duplicate_tick.duplicates == 1
        return tick

    run(poll_twice())
    report = store.feedback_report(fixtures.TEST_CHAT)
    assert report["up"] == 1 and report["down"] == 0
    assert report["items"][0]["text"] == "answer"
    assert store.has_update(70)
    payload = json.loads(store._query(
        "SELECT payload_json FROM updates WHERE update_id = 70"
    )[0]["payload_json"])
    assert payload == {
        "kind": "message_reaction", "old_vote": None, "new_vote": "up",
    }
    columns = {row["name"] for row in store._query("PRAGMA table_info(feedback_events)")}
    assert "user_id" not in columns and "actor_id" not in columns
    request = next(payload for method, payload in transport.calls if method == "getUpdates")
    assert "message_reaction" in request["allowed_updates"]
    assert "callback_query" not in request["allowed_updates"]
    store.close()


def test_vote_change_and_removal_update_per_reply_totals(
    tmp_path, config, transport, system_prompt
):
    store = Store(tmp_path / "db.sqlite3")
    sent_reply(store, fixtures.TEST_CHAT, 9001)
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=system_prompt, agent_factory=lambda spec: None)
    from conftest import run

    run(bot.step([reaction(1, chat_id=fixtures.TEST_CHAT, message_id=9001,
                           old=[], new=[emoji("👍")])]))
    run(bot.step([reaction(2, chat_id=fixtures.TEST_CHAT, message_id=9001,
                           old=[emoji("👍")], new=[emoji("👎")])]))
    report = store.feedback_report()
    assert report["up"] == 0 and report["down"] == 1

    run(bot.step([reaction(3, chat_id=fixtures.TEST_CHAT, message_id=9001,
                           old=[emoji("👎")], new=[])]))
    report = store.feedback_report()
    assert report["up"] == 0 and report["down"] == 0
    assert report["items"][0]["message_id"] == 9001
    store.close()


def test_feedback_requires_allowed_chat_and_our_sent_target(
    tmp_path, config, transport, system_prompt
):
    store = Store(tmp_path / "db.sqlite3")
    sent_reply(store, fixtures.TEST_CHAT, 9001)
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=system_prompt, agent_factory=lambda spec: None)
    from conftest import run

    tick, _ = run(bot.step([
        reaction(1, chat_id=fixtures.OTHER_CHAT, message_id=9001,
                 old=[], new=[emoji("👍")]),
        reaction(2, chat_id=fixtures.TEST_CHAT, message_id=9999,
                 old=[], new=[emoji("👎")]),
        reaction(3, chat_id=fixtures.TEST_CHAT, message_id=9001,
                 old=[emoji("❤️")], new=[emoji("🔥")]),
    ]))
    assert tick.unauthorized == 1
    assert tick.ignored == 2
    assert not store.has_update(1)
    assert store.has_update(2) and store.has_update(3)
    assert store.feedback_report() == {"up": 0, "down": 0, "items": []}
    store.close()


def test_feedback_report_is_read_only_and_includes_reply_text(tmp_path):
    db = tmp_path / "db.sqlite3"
    store = Store(db)
    sent_reply(store, fixtures.TEST_CHAT, 9001)
    assert store.record_reaction_feedback(
        update_id=99, chat_id=fixtures.TEST_CHAT, message_id=9001,
        old_vote=None, new_vote="up",
    ) == "recorded"
    store.close()

    before = db.stat().st_mtime_ns
    report = load_report(db, fixtures.TEST_CHAT)
    after = db.stat().st_mtime_ns
    assert before == after
    assert report["up"] == 1 and report["down"] == 0
    assert report["items"][0]["text"] == "answer"
    assert report["items"][0]["message_id"] == 9001


def test_reaction_event_and_update_dedupe_rollback_together_on_failure(
    tmp_path, config, transport, system_prompt
):
    store = Store(tmp_path / "db.sqlite3")
    sent_reply(store, fixtures.TEST_CHAT, 9001)
    store._conn.execute(
        "CREATE TRIGGER reject_feedback BEFORE INSERT ON feedback_events "
        "BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
    )
    update = reaction(88, chat_id=fixtures.TEST_CHAT, message_id=9001,
                      old=[], new=[emoji("👍")])
    transport.updates = [[update]]
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=system_prompt, agent_factory=lambda spec: None)
    from conftest import run

    try:
        run(bot.poll_once(offset=None))
        assert False, "injected failure should escape the poll operation"
    except sqlite3.IntegrityError:
        pass
    assert not store.has_update(88)
    assert store.feedback_report()["up"] == 0
    assert store.get_update_cursor() is None

    store._conn.execute("DROP TRIGGER reject_feedback")
    transport.updates = [[update]]
    offset, tick = run(bot.poll_once(offset=None))
    assert offset == 89 and tick.feedback_updates == 1
    assert store.has_update(88)
    assert store.feedback_report()["up"] == 1
    store.close()


def test_normal_message_update_is_not_misread_as_feedback(
    tmp_path, config, transport, system_prompt
):
    store = Store(tmp_path / "db.sqlite3")
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=system_prompt, agent_factory=lambda spec: None)
    normal = {
        "update_id": 12,
        "message": {
            "message_id": 34,
            "chat": {"id": fixtures.TEST_CHAT, "type": "supergroup"},
            "from": {"id": fixtures.ALICE, "is_bot": False, "first_name": "Alice"},
            "date": 1_700_000_000,
            "text": "ordinary input",
        },
    }
    from conftest import run
    tick, decisions = run(bot.step([normal]))
    assert len(decisions) == 1 and decisions[0].is_silent
    assert tick.feedback_updates == 0
    assert store.stats()["messages"] == 1
    assert store.feedback_report() == {"up": 0, "down": 0, "items": []}
    store.close()

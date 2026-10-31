"""The interactive console: commands, scoped live feed, and its safety limits.

Everything here is synthetic: a fake bot over a real in-memory store, and a
StringIO instead of a terminal.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import time

import pytest

from app import fixtures, logwatch
from app.config import AppConfig
from app.console import Console, LiveFeed, parse_scope
from app.store import Store, StoredMessage

GROUP = -1001234567890
DM = 123456789


class FakeBot:
    """The surface the console actually touches."""

    def __init__(self, store):
        self.store = store
        self.chat_kinds = {GROUP: "supergroup", DM: "private"}
        self.stats = type("S", (), {"paused": False, "as_dict": lambda self: {
            "uptime_seconds": 12.0, "ticks": 3, "paused": False,
            "proactive_enabled": False, "total": {"updates": 7, "errors": 0},
        }})()
        self.paused_calls = 0
        self.resumed_calls = 0

    def pause(self):
        self.paused_calls += 1
        self.stats.paused = True

    def resume(self):
        self.resumed_calls += 1
        self.stats.paused = False


def seed(store, *, chat_id, topic_id=None, count=3, text="消息", age=60.0):
    now = time.time()
    with store._write() as conn:
        for index in range(count):
            sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM observation_events"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO observation_events (sequence, update_id, chat_id, topic_id,"
                " kind, received_at, payload_json) VALUES (?, ?, ?, ?, 'message', ?, ?)",
                (sequence, sequence, chat_id, topic_id, now - age,
                 json.dumps({"text": f"{text}{index}", "sender_label": "test_user",
                             "created_at": now - age}, ensure_ascii=False)),
            )
    return now


def stored(chat_id, message_id, text):
    return StoredMessage(
        chat_id=chat_id, message_id=message_id, topic_id=None,
        sender_id=fixtures.ALICE, sender_label="alice", is_bot=False,
        text=text, created_at=time.time(),
    )


def make(store, *, kinds=None):
    bot = FakeBot(store)
    if kinds:
        bot.chat_kinds = kinds
    config = AppConfig(max_activations_per_hour=60, max_actions_per_hour=20)
    stream = io.StringIO()
    console = Console(bot=bot, config=config, stream=stream)
    return console, bot, stream


def output(console, line):
    """Run one command and return only what it printed this time."""
    before = len(console.stream.getvalue())
    console.handle(line)
    return console.stream.getvalue()[before:]


def test_parse_scope_reads_the_rendered_label():
    assert parse_scope("stored chat:-1001234567890(supergroup) message_id=1") == \
        (GROUP, None, "supergroup")
    assert parse_scope("chat:123456789(private)/topic:7 pending=1") == \
        (DM, 7, "private")
    assert parse_scope("no scope in this line") is None


def test_help_lists_every_command_and_typos_do_not_raise():
    store = Store(":memory:")
    try:
        console, _, _ = make(store)
        text = output(console, "help")
        for command in ("status", "chats", "watch", "queue", "actions", "quit"):
            assert command in text

        typo = output(console, "stauts")
        assert "unknown command" in typo
        assert "traceback" not in typo.lower()
    finally:
        store.close()


def test_status_and_chats_report_both_conversations():
    store = Store(":memory:")
    try:
        seed(store, chat_id=GROUP, count=4)
        seed(store, chat_id=DM, topic_id=None, count=1)
        with store._write() as conn:
            conn.execute(
                "INSERT INTO observation_cursors (chat_id, topic_id, last_processed,"
                " updated_at) VALUES (?, NULL, ?, ?)", (DM, 999, time.time()))

        console, _, _ = make(store)
        text = output(console, "status")
        assert "status: pending=4" in text
        assert "chat:-1001234567890(supergroup)" in text
        assert "chat:123456789(private)" in text

        chats = output(console, "chats")
        assert "chat:123456789(private) pending=0" in chats
    finally:
        store.close()


def test_queue_shows_oldest_pending_with_age_and_sender():
    store = Store(":memory:")
    try:
        seed(store, chat_id=GROUP, count=2, text="在吗", age=125.0)
        console, _, _ = make(store)
        text = output(console, "queue 5")
        assert "seq=1" in text and "seq=2" in text
        assert "2m" in text
        assert "test_user" in text
        assert "在吗0" in text
    finally:
        store.close()


def test_actions_and_turns_and_waits_reflect_live_state():
    store = Store(":memory:")
    try:
        now = time.time()
        store.prepare_action(action_id="a1", workflow_id=None, chat_id=GROUP,
                             topic_id=None, text="hello there", observed_through=0)
        assert store.claim_action("a1") == "claimed"
        store.finish_action("a1", "unknown", error="Timed out")
        assert store.claim_action("a1") == "not-claimable"
        store.open_workflow(chat_id=GROUP, topic_id=None, first_sequence=1,
                            last_sequence=2, workflow_id="wf_live")
        store.add_wait(wait_id="w1", chat_id=GROUP, topic_id=None, kind="until",
                       purpose="等回复", due_at=now + 90)

        console, _, _ = make(store)
        actions = output(console, "actions")
        assert "unknown" in actions and "hello there" in actions
        assert "Timed out" in actions

        turns = output(console, "turns")
        assert "wf_live" in turns and "seq=1-2" in turns

        waits = output(console, "waits")
        assert "等回复" in waits and "due in" in waits
    finally:
        store.close()


def test_tail_only_shows_the_watched_conversation():
    store = Store(":memory:")
    try:
        store.record_message(stored(GROUP, 1, "群里的话"), update_id=1)
        store.record_message(stored(DM, 2, "私聊的话"), update_id=2)

        console, _, _ = make(store)
        everything = output(console, "tail 10")
        assert "群里的话" in everything and "私聊的话" in everything

        output(console, f"watch {GROUP}")
        only_group = output(console, "tail 10")
        assert "群里的话" in only_group
        assert "私聊的话" not in only_group
    finally:
        store.close()


def test_watch_filters_the_live_feed_and_unwatch_restores_it():
    store = Store(":memory:")
    try:
        console, _, stream = make(store)
        log = logging.getLogger("tests.console.feed")
        root = logging.getLogger()
        previous = root.level
        root.setLevel(logging.INFO)
        console.attach()
        try:
            output(console, f"watch {GROUP}")
            log.info("stored %s message_id=1", logwatch.scope(GROUP, None, "supergroup"))
            log.info("stored %s message_id=2",
                     logwatch.scope(DM, None, "private"))
            log.info("status: pending=0 running=0")

            text = stream.getvalue()
            assert "message_id=1" in text
            assert "message_id=2" not in text, "another chat leaked into the watch"
            # Unscoped lines such as the status block stay quiet while
            # something is being watched.
            assert "status: pending=0" not in text.split("console ready")[-1]

            output(console, "unwatch")
            log.info("stored %s message_id=3",
                     logwatch.scope(DM, None, "private"))
            assert "message_id=3" in stream.getvalue()
        finally:
            console.detach()
            root.setLevel(previous)
    finally:
        store.close()


def test_quiet_hides_per_event_lines_but_keeps_status():
    store = Store(":memory:")
    try:
        console, _, stream = make(store)
        log = logging.getLogger("tests.console.quiet")
        root = logging.getLogger()
        previous = root.level
        root.setLevel(logging.INFO)
        console.attach()
        try:
            # Default: everything is mirrored.
            log.info("stored %s message_id=1", logwatch.scope(GROUP, None, "supergroup"))
            assert "message_id=1" in stream.getvalue()

            output(console, "quiet on")
            marker = len(stream.getvalue())
            log.info("stored %s message_id=2", logwatch.scope(GROUP, None, "supergroup"))
            log.info("status: pending=0")
            after = stream.getvalue()[marker:]
            assert "message_id=2" not in after, "quiet still showed a per-event line"
            assert "status: pending=0" in after
        finally:
            console.detach()
            root.setLevel(previous)
    finally:
        store.close()


def test_pause_resume_and_quit_only_touch_flags():
    store = Store(":memory:")
    try:
        console, bot, _ = make(store)
        output(console, "pause")
        assert bot.paused_calls == 1 and bot.stats.paused is True
        output(console, "resume")
        assert bot.resumed_calls == 1 and bot.stats.paused is False

        # Nothing the console can do reaches Telegram: there is no client on it
        # at all, and `quit` only sets the stop event it was handed.
        stop = asyncio.Event()
        console._stop = stop
        output(console, "quit")
        assert stop.is_set()
    finally:
        store.close()


def test_run_returns_at_eof_instead_of_spinning(monkeypatch):
    """A closed stdin must end the reader, not busy-loop the event loop."""
    store = Store(":memory:")
    try:
        console, _, _ = make(store)
        monkeypatch.setattr("sys.stdin", io.StringIO(""))

        async def scenario():
            stop = asyncio.Event()
            await asyncio.wait_for(console.run(stop), timeout=5)
            return stop.is_set()

        assert asyncio.run(scenario()) is False
    finally:
        store.close()


def test_attach_does_not_duplicate_lines_through_the_base_handler():
    """basicConfig already prints INFO; the feed must not print it a second time."""
    store = Store(":memory:")
    base_output = io.StringIO()
    base = logging.StreamHandler(base_output)
    base.setLevel(logging.INFO)
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(base)
    try:
        console, _, stream = make(store)
        console.attach()
        try:
            logging.getLogger("tests.console.dupe").info(
                "stored %s message_id=1", logwatch.scope(GROUP, None, "supergroup"))
            logging.getLogger("tests.console.dupe").warning("something bad")
        finally:
            console.detach()
        assert "message_id=1" in stream.getvalue(), "feed did not show the line"
        assert "message_id=1" not in base_output.getvalue(), "line printed twice"
        assert "something bad" in base_output.getvalue(), "warnings lost their traceback path"
        assert base.level == logging.INFO, "base handler level was not restored"
    finally:
        root.removeHandler(base)
        root.setLevel(previous_level)
        store.close()


def test_stats_reports_counters():
    store = Store(":memory:")
    try:
        console, _, _ = make(store)
        text = output(console, "stats")
        assert "ticks: 3" in text
        assert "'updates': 7" in text
    finally:
        store.close()

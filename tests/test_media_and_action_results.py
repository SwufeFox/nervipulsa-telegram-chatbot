"""Two gaps a live run exposed.

1. Attachments were dropped: `parse_update` kept `text` or `caption` and threw
   the rest away, so a bare sticker reached the agent as an empty message - the
   same shape as silence. A sticker's emoji is usually its entire content.

2. Delivery outcomes never came back: the agent saw `accepted/pending` when it
   chose to speak, and the eventual `sent` / `failed` / `unknown` lived only in
   the actions table. An unconfirmed send is exactly the case where knowing
   matters, and it was invisible.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nervipulsa.config import Settings
from nervipulsa.ports import ModelResponse, ToolCall
from nervipulsa.providers import ScriptedBackend

from app import fixtures
from app.agent import NervipulsaAgent
from app.bot import GroupAgent
from app.config import AppConfig, load_default_profile
from app.store import Store
from app.telegram import TelegramClient, parse_update
from app.turns import TurnManager
from conftest import FakeClock, FakeTransport, run


def update_with(message_extra: dict, *, uid: int = 900) -> dict:
    message = {
        "message_id": uid,
        "chat": {"id": fixtures.TEST_CHAT, "type": "supergroup", "title": "g"},
        "from": {"id": fixtures.ALICE, "is_bot": False, "first_name": "Alice"},
        "date": 1_700_000_000,
    }
    message.update(message_extra)
    return {"update_id": uid, "message": message}


# -- attachment parsing ----------------------------------------------------

def test_sticker_keeps_its_emoji():
    inbound = parse_update(update_with({
        "sticker": {"emoji": "🦊", "set_name": "fox_pack", "is_animated": True},
    }))
    assert inbound is not None
    assert inbound.media_kind == "sticker"
    assert "🦊" in inbound.media_hint
    assert "fox_pack" in inbound.media_hint
    assert inbound.text == ""


def test_photo_reports_its_largest_size():
    inbound = parse_update(update_with({
        "photo": [
            {"file_id": "small", "width": 320, "height": 240},
            {"file_id": "large", "width": 1280, "height": 960},
        ],
    }))
    assert inbound is not None
    assert inbound.media_kind == "photo"
    assert inbound.media_hint == "1280x960"


def test_caption_still_arrives_as_text_alongside_the_media():
    inbound = parse_update(update_with({
        "photo": [{"file_id": "p", "width": 90, "height": 90}],
        "caption": "看这个",
    }))
    assert inbound is not None
    assert inbound.text == "看这个"
    assert inbound.media_kind == "photo"


def test_document_and_voice_are_classified():
    doc = parse_update(update_with({
        "document": {"file_name": "logs.txt", "mime_type": "text/plain"}}))
    assert doc.media_kind == "document"
    assert "logs.txt" in doc.media_hint

    voice = parse_update(update_with({"voice": {"duration": 7}}))
    assert voice.media_kind == "voice"
    assert "7" in voice.media_hint


def test_plain_text_has_no_media():
    inbound = parse_update(update_with({"text": "普通消息"}))
    assert inbound.media_kind == ""
    assert inbound.media_hint == ""


# -- end to end: the sticker reaches the agent -----------------------------

def silent(request):
    return ModelResponse(content="", model="scripted",
                         tool_calls=[ToolCall(id="c", name="finish_silently",
                                              arguments={})])


def build(tmp_path, responder=silent, *, transport=None):
    clock = FakeClock()
    store = Store(tmp_path / "app.db", clock=clock)
    transport = transport or FakeTransport()
    prompt = load_default_profile()
    settings = Settings(model="scripted", workspace=str(tmp_path / "rt"),
                        max_timeout=30, default_timeout=30, context_limit=40000)
    handle = {}

    def factory(spec):
        handle["agent"] = NervipulsaAgent(
            settings=settings, workspace=spec.workspace, system_prompt=prompt,
            backend=ScriptedBackend(responder), timeout=10,
            tool_catalog=spec.tool_catalog,
            response_commit_hook=spec.response_commit_hook,
            session_id=spec.session_id, restore=spec.restore,
        )
        return handle["agent"]

    config = AppConfig(
        bot_name="Alma", bot_username="alma_bot",
        allowed_chat_ids=(fixtures.TEST_CHAT,), idle_seconds=0.0,
        workspace=str(tmp_path), model="scripted",
        min_action_interval_seconds=0.0, max_actions_per_hour=99,
    )
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=prompt, agent_factory=factory,
                     turn_manager=TurnManager(debounce_seconds=2, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)
    return bot, store, clock, transport, handle


def test_agent_sees_the_sticker_instead_of_an_empty_message(tmp_path):
    seen: list = []

    def record(request):
        seen.append(request)
        return silent(request)

    async def scenario():
        bot, store, clock, _, _ = build(tmp_path, record)
        await bot.start()
        try:
            await bot.step([update_with({
                "sticker": {"emoji": "🦊", "set_name": "fox_pack"},
            })])
            clock.advance(5)
            await bot.autonomous_step()
            body = json.dumps(seen[-1].messages, ensure_ascii=False)
            assert "sticker" in body
            assert "🦊" in body
        finally:
            await bot.stop()
            store.close()

    run(scenario())


# -- delivery outcome comes back -------------------------------------------

def test_unconfirmed_delivery_is_reported_back_as_an_observation(tmp_path):
    """A timeout may still have reached the group; the agent must learn that."""
    from telegram.error import TimedOut

    class TimingOutTransport(FakeTransport):
        """The real PTB transport maps TimedOut to `indeterminate`."""

        def call(self, method, payload):
            if method == "sendMessage":
                self.calls.append((method, payload))
                raise TimedOut()
            return super().call(method, payload)

    async def scenario():
        bot, store, clock, transport_, handle = build(
            tmp_path,
            lambda request: ModelResponse(
                content="", model="scripted",
                tool_calls=[ToolCall(id="c", name="send_message",
                                     arguments={"text": "年糕。"})]),
            transport=TimingOutTransport(),
        )
        await bot.start()
        try:
            await bot.step([fixtures.message(
                update_id=901, message_id=901, sender_id=fixtures.ALICE,
                username="alice", topic_id=fixtures.TOPIC_A, text="猫叫什么来着")])
            clock.advance(5)
            await bot.autonomous_step()

            rows = store._query(
                "SELECT kind, payload_json FROM observation_events"
                " WHERE kind = 'action_result' ORDER BY sequence")
            assert rows, "the delivery result never entered the observation stream"
            payload = json.loads(rows[-1]["payload_json"])
            assert payload["status"] in {"unknown", "failed"}, payload
            assert payload["text"] == "年糕。"

            # A failed delivery must re-arm the scope so she can react.
            assert bot.queued_topics()
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_successful_delivery_is_recorded_without_waking_her(tmp_path):
    async def scenario():
        bot, store, clock, transport, handle = build(
            tmp_path, lambda request: ModelResponse(
                content="", model="scripted",
                tool_calls=[ToolCall(id="c", name="send_message",
                                     arguments={"text": "嗯"})]),
        )
        await bot.start()
        try:
            await bot.step([fixtures.message(
                update_id=902, message_id=902, sender_id=fixtures.ALICE,
                username="alice", topic_id=fixtures.TOPIC_A, text="在吗")])
            clock.advance(5)
            await bot.autonomous_step()

            # A successful send is not news to her: she chose to speak and the
            # tool already said it was accepted. Recording it in the stream
            # would wake her on her own message.
            rows = store._query(
                "SELECT payload_json FROM observation_events WHERE kind = 'action_result'")
            assert rows == [], [dict(r) for r in rows]
            assert bot.queued_topics() == []
            # It is still in the chat log, so later turns see it as history.
            sent_rows = store._query(
                "SELECT text, is_bot FROM messages WHERE is_bot = 1")
            assert [r["text"] for r in sent_rows] == ["嗯"]
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_own_delivery_does_not_satisfy_an_unfiltered_wait(tmp_path):
    """Waiting for the group is not satisfied by our own send receipt."""
    store = Store(tmp_path / "w.db")
    try:
        store.add_wait(wait_id="w1", chat_id=fixtures.TEST_CHAT,
                       topic_id=fixtures.TOPIC_A, kind="event",
                       purpose="等群友回答", event_types=None)
        # Her own receipt must not count.
        assert store.satisfy_event_waits(
            fixtures.TEST_CHAT, fixtures.TOPIC_A, "action_result", 100.0) == []
        # A real message still does.
        assert store.satisfy_event_waits(
            fixtures.TEST_CHAT, fixtures.TOPIC_A, "message", 101.0) == ["w1"]
    finally:
        store.close()


def test_wait_can_explicitly_ask_for_a_delivery_result(tmp_path):
    store = Store(tmp_path / "w2.db")
    try:
        store.add_wait(wait_id="w1", chat_id=fixtures.TEST_CHAT,
                       topic_id=None, kind="event",
                       purpose="确认自己发出去没有",
                       event_types=["action_result"])
        assert store.satisfy_event_waits(
            fixtures.TEST_CHAT, None, "action_result", 100.0) == ["w1"]
    finally:
        store.close()

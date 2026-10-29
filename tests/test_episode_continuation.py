"""One logical work session, many model activations.

The previous shape persisted a *goal* and started a *new workflow* on every
wake. That is not continuation: nothing in the data said "these two activations
are the same job", so a restart, a wait firing, or an action result could not be
told apart from a fresh conversation. These tests pin the identity that makes
the difference observable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

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
from app.telegram import TelegramClient
from app.turns import TurnManager
from conftest import FakeClock, FakeTransport, run


def msg(uid, text, sender_id=fixtures.ALICE, name="alice"):
    return fixtures.message(update_id=uid, message_id=uid, sender_id=sender_id,
                            username=name, topic_id=fixtures.TOPIC_A, text=text)


def call(name, **arguments):
    return ModelResponse(content="", model="scripted",
                         tool_calls=[ToolCall(id=f"c-{name}", name=name,
                                              arguments=arguments)])


def build(tmp_path, answer, *, clock=None):
    clock = clock or FakeClock()
    store = Store(tmp_path / "app.db", clock=clock)
    transport = FakeTransport()
    prompt = load_default_profile()
    settings = Settings(model="scripted", workspace=str(tmp_path / "rt"),
                        max_timeout=30, default_timeout=30, context_limit=40000)
    payloads: list[dict] = []
    calls = 0

    def factory(spec):
        def responder(request):
            nonlocal calls
            payload = payloads[-1] if payloads else {}
            calls += 1
            return answer(calls, payload)

        return NervipulsaAgent(
            settings=settings, workspace=spec.workspace, system_prompt=prompt,
            backend=ScriptedBackend(responder), timeout=10,
            tool_catalog=spec.tool_catalog,
            response_commit_hook=spec.response_commit_hook,
            session_id=spec.session_id, restore=spec.restore,
        )

    config = AppConfig(
        bot_name="Alma", bot_username="alma_bot",
        allowed_chat_ids=(fixtures.TEST_CHAT,), idle_seconds=0.0,
        workspace=str(tmp_path), model="scripted",
        min_action_interval_seconds=0.0, max_actions_per_hour=99,
    )
    bot = GroupAgent(config=config, store=store, client=TelegramClient(transport),
                     system_prompt=prompt, agent_factory=factory,
                     turn_manager=TurnManager(debounce_seconds=0, clock=clock),
                     bot_user_id=fixtures.ALMA, clock=clock)

    loop = bot.autonomy
    original = loop._render

    def render(observations, chat_id, topic_id, **kwargs):
        rendered = original(observations, chat_id, topic_id, **kwargs)
        payloads.append(rendered)
        return rendered

    loop._render = render
    return bot, store, clock, transport, payloads


def test_a_timer_wake_continues_the_same_episode(tmp_path):
    """The agent's own `wait_until` must resume the work it registered the wait in."""
    from datetime import datetime, timezone

    clock = FakeClock()

    def answer(index, payload):
        if index == 1:
            return call("set_intent", goal="十分钟后提醒她跑测试")
        if index == 2:
            # A real instant just after the fake clock, not a fixed year.
            at = datetime.fromtimestamp(clock.now + 600, tz=timezone.utc).isoformat()
            return call("wait_until", at=at, purpose="提醒跑测试")
        return call("finish_silently")

    async def scenario():
        bot, store, fake, transport, payloads = build(tmp_path, answer, clock=clock)
        await bot.start()
        try:
            await bot.step([msg(101, "先记着")])
            clock.advance(5)
            await bot.autonomous_step(force=True)
            episode_id = str(store.current_episode(
                fixtures.TEST_CHAT, fixtures.TOPIC_A)["episode_id"])

            # Register the timer.
            await bot.step([msg(102, "别忘了")])
            clock.advance(5)
            await bot.autonomous_step(force=True)
            assert store.active_waits(fixtures.TEST_CHAT, fixtures.TOPIC_A), \
                "the timer was not stored"

            # Nothing new is said; only the timer comes due.
            clock.advance(700)
            await bot.autonomous_step(force=True)

            assert len(store.episode_activations(episode_id)) >= 3, \
                "the timer wake did not join the same episode"
            assert payloads[-1]["episode"]["episode_id"] == episode_id
            assert payloads[-1]["episode"]["continuation"] is True
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_episode_spans_activations_joined_by_a_wait(tmp_path):
    """Open a goal, wait, wake: both activations must share one episode id."""
    seen = []

    def answer(index, payload):
        seen.append(payload)
        if index == 1:
            return call("set_intent", goal="等她把 dump 贴出来再判断")
        return call("finish_silently")

    async def scenario():
        bot, store, clock, transport, payloads = build(tmp_path, answer)
        await bot.start()
        try:
            await bot.step([msg(201, "先这样")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            episode = store.current_episode(fixtures.TEST_CHAT, fixtures.TOPIC_A)
            assert episode is not None, "a live goal must keep the episode open"
            episode_id = str(episode["episode_id"])
            assert store.episode_activations(episode_id) != [], \
                "the opening activation was not linked to the episode"

            await bot.step([msg(202, "dump 在这")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            activations = store.episode_activations(episode_id)
            assert len(activations) >= 2, \
                f"the wake did not join the episode: {len(activations)} activation(s)"
            # The model must be told this is a continuation, not a fresh start.
            assert payloads[-1]["episode"]["episode_id"] == episode_id
            assert payloads[-1]["episode"]["continuation"] is True
            assert payloads[-1]["episode"]["activations"] >= 1
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_the_first_activation_is_not_marked_a_continuation(tmp_path):
    seen = []

    def answer(index, payload):
        seen.append(payload)
        return call("finish_silently")

    async def scenario():
        bot, store, clock, transport, payloads = build(tmp_path, answer)
        await bot.start()
        try:
            await bot.step([msg(301, "在吗")])
            clock.advance(5)
            await bot.autonomous_step(force=True)
            assert payloads[-1]["episode"]["continuation"] is False
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_an_episode_closes_when_nothing_can_resume_it(tmp_path):
    """Leaving it open would promise a continuation that can never happen."""

    def answer(index, payload):
        if index == 1:
            return call("send_message", text="说一句")
        return call("finish_silently")

    async def scenario():
        bot, store, clock, transport, payloads = build(tmp_path, answer)
        await bot.start()
        try:
            await bot.step([msg(401, "你怎么看")])
            clock.advance(5)
            await bot.autonomous_step(force=True)
            assert store.current_episode(fixtures.TEST_CHAT, fixtures.TOPIC_A) is None, \
                "an episode with no goal, no wait and no pending work was left open"
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_a_missing_episode_id_never_becomes_the_string_none(tmp_path):
    """`str(None)` would be a plausible-looking id that merges unrelated work."""
    def answer(index, payload):
        if index == 1:
            return call("finish_silently")
        return call("finish_silently")

    async def scenario():
        bot, store, clock, transport, payloads = build(tmp_path, answer)
        # Stand in for a deployment where the episode mechanism is disabled.
        store.open_episode = lambda **kwargs: {"episode_id": None}
        store.current_episode = lambda *a, **k: None
        await bot.start()
        try:
            await bot.step([msg(601, "在吗")])
            clock.advance(5)
            results = await bot.autonomous_step(force=True)
            detail = results[0].detail if results else {}
            assert detail.get("episode_id") is None, \
                f"a missing id became {detail.get('episode_id')!r}"
            assert payloads[-1]["episode"] is None
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_a_concurrent_open_adopts_the_winner(tmp_path):
    """Two activations racing must not invent two identities for one scope."""
    store = Store(tmp_path / "app.db")
    try:
        first = store.open_episode(episode_id="ep_a", chat_id=1, topic_id=None)
        second = store.open_episode(episode_id="ep_b", chat_id=1, topic_id=None)
        assert first["episode_id"] == "ep_a"
        assert second["episode_id"] == "ep_a", "the loser created a second episode"
    finally:
        store.close()


def test_only_one_open_episode_per_scope_survives_null_topic(tmp_path):
    """SQLite treats NULLs as distinct; the schema must not rely on that."""
    store = Store(tmp_path / "app.db")
    try:
        store.open_episode(episode_id="ep_a", chat_id=7, topic_id=None)
        store.close_episode("ep_a", reason="done")
        store.open_episode(episode_id="ep_b", chat_id=7, topic_id=None)
        rows = store._query(
            "SELECT episode_id FROM workflow_episodes WHERE state = 'open'")
        assert [row["episode_id"] for row in rows] == ["ep_b"]
    finally:
        store.close()


def test_a_delivered_message_closes_the_goal_and_the_episode(tmp_path):
    def answer(index, payload):
        if index == 1:
            return call("set_intent", goal="回他一句")
        if index == 2:
            return call("send_message", text="回了", resolves_intent=True)
        return call("finish_silently")

    async def scenario():
        bot, store, clock, transport, payloads = build(tmp_path, answer)
        await bot.start()
        try:
            await bot.step([msg(501, "问个事")])
            clock.advance(5)
            await bot.autonomous_step(force=True)
            assert store.current_intent(fixtures.TEST_CHAT, fixtures.TOPIC_A) is not None

            await bot.step([msg(502, "人呢")])
            clock.advance(5)
            await bot.autonomous_step(force=True)

            assert transport.sends, "nothing was delivered"
            assert store.current_intent(fixtures.TEST_CHAT, fixtures.TOPIC_A) is None
            assert store.current_episode(fixtures.TEST_CHAT, fixtures.TOPIC_A) is None
        finally:
            await bot.stop()
            store.close()

    run(scenario())


def test_an_episode_survives_a_restart_and_is_reported(tmp_path):
    """The identity is data. A restart must resume it, not silently replace it."""
    store = Store(tmp_path / "app.db")
    try:
        episode = store.open_episode(episode_id="ep_keep", chat_id=1, topic_id=None)
        assert episode["episode_id"] == "ep_keep"
        store.open_workflow(chat_id=1, topic_id=None, first_sequence=1,
                            last_sequence=1, workflow_id="wf_1", episode_id="ep_keep")
    finally:
        store.close()

    reopened = Store(tmp_path / "app.db")
    try:
        open_episodes = reopened.open_episodes()
        assert [row["episode_id"] for row in open_episodes] == ["ep_keep"]
        assert [row["workflow_id"] for row in
                reopened.episode_activations("ep_keep")] == ["wf_1"]
    finally:
        reopened.close()


def test_old_databases_gain_the_episode_columns(tmp_path):
    """A deployed database predates these columns; startup must add them."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            "CREATE TABLE workflows (workflow_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL,"
            " topic_id INTEGER, first_sequence INTEGER NOT NULL, last_sequence INTEGER NOT NULL,"
            " state TEXT NOT NULL, outcome TEXT, detail_json TEXT NOT NULL DEFAULT '{}',"
            " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
        conn.execute(
            "CREATE TABLE agent_waits (wait_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL,"
            " topic_id INTEGER, kind TEXT NOT NULL, purpose TEXT NOT NULL,"
            " event_types_json TEXT, due_at REAL, expires_at REAL, status TEXT NOT NULL,"
            " created_at REAL NOT NULL, resolved_at REAL)")
        conn.commit()
    finally:
        conn.close()

    store = Store(path)
    try:
        workflow_columns = {row["name"] for row in
                            store._query("PRAGMA table_info(workflows)")}
        wait_columns = {row["name"] for row in
                        store._query("PRAGMA table_info(agent_waits)")}
        assert "episode_id" in workflow_columns
        assert "episode_id" in wait_columns
        # And the new path works on the migrated database.
        store.open_episode(episode_id="ep_new", chat_id=1, topic_id=None)
    finally:
        store.close()

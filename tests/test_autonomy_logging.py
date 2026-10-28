"""Privacy and operational coverage for autonomous loop logs (synthetic data)."""
import logging
from types import SimpleNamespace

from app.autonomy import AutonomousLoop
from app.store import Store
from conftest import FakeClock, run


SECRET = "synthetic-token-and-message-secret"


def make_loop(store, clock, agent, **kwargs):
    async def agent_for(chat_id, topic_id):
        return agent
    return AutonomousLoop(store=store, clock=clock, agent_for=agent_for,
                          system_prompt=SECRET, idle_seconds=0, **kwargs)


def observe(store):
    with store._write() as conn:
        for update_id in (1, 2):
            conn.execute("INSERT INTO observation_events (update_id, chat_id, topic_id, kind, received_at, payload_json) VALUES (?, 42, 7, 'message', 0, ?)",
                         (update_id, '{"text":"' + SECRET + '"}'))


def test_batch_call_and_untrusted_outcome_are_safe(caplog):
    class Agent:
        async def observe(self, payload, tag, handler):
            await handler("finish_silently", {}, None, None, None)
            return SimpleNamespace(status="ok", outcome=SECRET, detail={"secret": SECRET})

    clock = FakeClock()
    store = Store(":memory:", clock=clock)
    observe(store)
    loop = make_loop(store, clock, Agent())
    with caplog.at_level(logging.INFO, logger="app.autonomy"):
        run(loop.activate(42, 7))
    assert "batch activated chat_id=42 topic_id=7 workflow_id=wf_" in caplog.text
    assert "observations=2 first_sequence=1 last_sequence=2" in caplog.text
    assert "agent call started" in caplog.text
    assert "outcome=other elapsed_ms=" in caplog.text
    assert "pending_actions=0 waits=0" in caplog.text
    assert SECRET not in caplog.text
    store.close()


def test_agent_exception_and_budget_do_not_log_secrets(caplog):
    class Agent:
        async def observe(self, payload, tag, handler):
            raise ValueError(SECRET)

    clock = FakeClock()
    store = Store(":memory:", clock=clock)
    observe(store)
    loop = make_loop(store, clock, Agent(), max_activations_per_hour=0)
    with caplog.at_level(logging.INFO, logger="app.autonomy"):
        run(loop.activate(42, 7))
        loop.max_activations_per_hour = None
        run(loop.activate(42, 7))
    assert "reason=activation_budget observations=2" in caplog.text
    assert "outcome=agent_error error_type=ValueError" in caplog.text
    assert SECRET not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    store.close()


def test_throttled_action_logged_once_then_claimed_unknown(caplog):
    clock = FakeClock()
    store = Store(":memory:", clock=clock)
    store.prepare_action(action_id="prior", workflow_id=None, chat_id=42, topic_id=7,
                         text=SECRET, observed_through=0)
    assert store.claim_action("prior") == "claimed"
    store.finish_action("prior", "sent")
    store.prepare_action(action_id="pending", workflow_id=None, chat_id=42, topic_id=7,
                         text=SECRET, observed_through=0)

    async def send(**kwargs):
        raise RuntimeError(SECRET)

    loop = make_loop(store, clock, None, send=send, min_action_interval_seconds=20)
    with caplog.at_level(logging.INFO, logger="app.autonomy"):
        for _ in range(5):
            run(loop.flush_actions())
            clock.advance(0.25)
        clock.advance(21)
        run(loop.flush_actions())
    assert caplog.text.count("action throttled chat_id=42 topic_id=7 action_id=pending") == 1
    assert "action_id=pending outcome=claimed" in caplog.text
    assert "action_id=pending outcome=unknown error_type=RuntimeError" in caplog.text
    assert SECRET not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    store.close()


def test_pending_counts_include_created_action_and_wait(caplog):
    class Agent:
        async def observe(self, payload, tag, handler):
            await handler("send_message", {"text": SECRET}, None, None, None)
            await handler("wait_for_event", {"purpose": SECRET}, None, None, None)
            return SimpleNamespace(status="ok", outcome="waiting", detail={})

    async def send(**kwargs):
        return SimpleNamespace(status="sent", telegram_message_id=None)

    clock = FakeClock()
    store = Store(":memory:", clock=clock)
    observe(store)
    loop = make_loop(store, clock, Agent(), send=send)
    with caplog.at_level(logging.INFO, logger="app.autonomy"):
        run(loop.activate(42, 7))
    assert "outcome=waiting" in caplog.text
    assert "pending_actions=1 waits=1" in caplog.text
    assert "outcome=claimed" in caplog.text
    assert "outcome=sent" in caplog.text
    assert SECRET not in caplog.text
    store.close()


def test_expired_and_stale_claims_are_logged(caplog):
    clock = FakeClock()
    store = Store(":memory:", clock=clock)
    observe(store)
    store.prepare_action(action_id="stale", workflow_id=None, chat_id=42, topic_id=7,
                         text=SECRET, observed_through=0)
    store.prepare_action(action_id="expired", workflow_id=None, chat_id=43, topic_id=None,
                         text=SECRET, observed_through=0, expires_at=clock() - 1)
    loop = make_loop(store, clock, None)
    with caplog.at_level(logging.INFO, logger="app.autonomy"):
        run(loop.flush_actions())
    assert "action_id=stale outcome=stale" in caplog.text
    assert "action_id=expired outcome=expired" in caplog.text
    assert SECRET not in caplog.text
    store.close()

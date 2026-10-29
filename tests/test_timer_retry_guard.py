"""A timer can bypass debounce, but it must respect failure backoff."""
from nervipulsa.ports import ModelResponse, ToolCall

from app import fixtures
from conftest import run
from test_wait_wakes_agent import build


def test_due_timer_respects_retry_deadline(tmp_path):
    async def scenario():
        calls = []

        def responder(request):
            calls.append(request)
            return ModelResponse(content="", model="scripted", tool_calls=[
                ToolCall(id="timer-reply", name="finish_silently", arguments={})
            ])

        bot, store, clock, transport, _ = build(tmp_path, responder)
        scope = (fixtures.TEST_CHAT, fixtures.TOPIC_A)
        await bot.start()
        try:
            store.add_wait(wait_id="retry-probe", chat_id=scope[0], topic_id=scope[1],
                           kind="until", purpose="check later", due_at=clock() - 1)
            bot.autonomy._retry_after[scope] = clock() + 30

            await bot.autonomous_step(force=False)
            assert not calls, "due timer bypassed the active retry deadline"
            assert len(store._query("SELECT sequence FROM observation_events WHERE kind='timer'")) == 1
            assert store._query("SELECT status FROM agent_waits WHERE wait_id='retry-probe'")[0]["status"] == "satisfied"

            clock.advance(31)
            await bot.autonomous_step(force=False)
            assert len(calls) == 1, "the stored timer was not resumed after backoff"
        finally:
            await bot.stop()
            store.close()

    run(scenario())

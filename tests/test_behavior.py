"""Group behaviour: when the bot speaks, when it stays quiet, and what it never does.

The brief's nine behavioural requirements map to tests here by name.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import fixtures
from app.agent import ScriptedAgent
from app.bot import GroupAgent
from app.conversation import build_prompt, parse_reply
from app.policy import (
    Decision,
    ParticipationPolicy,
    PolicyConfig,
    TurnContext,
    proactive_score,
)
from app.store import StoredMessage
from app.telegram import TelegramClient
from app.turns import Turn, TurnManager
from conftest import run


def make_bot(store, config, transport, clock, system_prompt, responder=None, **kwargs):
    agent = ScriptedAgent(responder or (lambda prompt: "sure, the cron runs in UTC"))
    turns = kwargs.pop("turns", None) or TurnManager(debounce_seconds=2.0, clock=clock)
    policy = kwargs.pop("policy", None) or ParticipationPolicy(
        PolicyConfig(proactive_enabled=False, min_interval_seconds=240, max_per_hour=4)
    )
    bot = GroupAgent(
        config=config, store=store, client=TelegramClient(transport),
        system_prompt=system_prompt, agent_factory=lambda spec: agent,
        turn_manager=turns, policy=policy, bot_user_id=fixtures.ALMA, clock=clock,
    )
    return bot, agent


def message(message_id: int, text: str, *, sender=fixtures.ALICE, label="alice",
            topic=None, is_bot=False, reply_to_bot=False, at=0.0) -> StoredMessage:
    return StoredMessage(
        chat_id=fixtures.TEST_CHAT, message_id=message_id, topic_id=topic,
        sender_id=sender, sender_label=label, is_bot=is_bot, text=text,
        created_at=at, reply_to_bot=reply_to_bot,
    )


def turn_of(*messages: StoredMessage, turn_id: str = "t1", topic=None) -> Turn:
    return Turn(
        turn_id=turn_id, chat_id=fixtures.TEST_CHAT, topic_id=topic,
        messages=list(messages), opened_at=0.0, closed_at=1.0,
    )


# -- Requirement 2: ordinary chat gets silence, and it is recorded ---------


def test_ordinary_group_chat_produces_recorded_silence(store, config, transport, clock,
                                                        system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        tick, decisions = await bot.step(fixtures.plain_chat())
        clock.advance(3.0)
        tick2, more = await bot.step([])
        decisions += more
        assert len(decisions) == 1
        assert decisions[0].is_silent
        assert decisions[0].reason == "proactive_disabled"
        # Silence is a row in the database, not merely the absence of a send.
        rows = store._query("SELECT outcome, reason FROM decisions")
        assert len(rows) == 1 and rows[0]["outcome"] == "silent"
        # And it cost nothing.
        assert agent.calls == []
        assert transport.sends == []
        await bot.stop()

    run(body())


def test_silence_is_never_an_empty_message(store, config, transport, clock,
                                           system_prompt) -> None:
    """There is no code path that sends empty text, even if a model returns ''."""

    def empty(prompt: str) -> str:
        return ""

    bot, agent = make_bot(store, config, transport, clock, system_prompt, responder=empty)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        assert transport.sends == [], "an empty model reply produced a Telegram send"
        rows = store._query("SELECT outcome, reason FROM decisions ORDER BY created_at")
        assert any(r["reason"] == "model_returned_nothing" for r in rows), [dict(r) for r in rows]
        await bot.stop()

    run(body())


# -- Requirement 1: mention and reply take priority ------------------------


def test_mention_gets_a_reply(store, config, transport, clock, system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        assert len(transport.sends) == 1
        assert transport.sends[0]["reply_parameters"]["message_id"] == 110
        await bot.stop()

    run(body())


def test_reply_to_bot_is_treated_as_addressed(store, config, transport, clock,
                                              system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.reply_to_bot()])
        clock.advance(3.0)
        await bot.step([])
        assert len(transport.sends) == 1
        await bot.stop()

    run(body())


def test_message_addressed_to_another_bot_is_not_ours(store, config, transport, clock,
                                                      system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.message(
            update_id=5, message_id=5, sender_id=fixtures.ALICE, username="alice",
            text="@someotherbot please summarise this")])
        clock.advance(3.0)
        await bot.step([])
        assert transport.sends == []
        await bot.stop()

    run(body())


# -- Requirement 3: bursts collapse into one turn --------------------------


def test_three_rapid_messages_make_one_turn(store, config, transport, clock,
                                            system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt,
                          responder=lambda p: "it runs before the deploy")

    async def body() -> None:
        await bot.start()
        await bot.step(fixtures.three_in_a_row())
        clock.advance(3.0)
        decisions = await bot.run_turns()
        assert len(decisions) == 1
        assert store.stats()["turns"] == 1
        await bot.stop()

    run(body())


def test_burst_addressed_to_bot_answers_once(store, config, transport, clock,
                                             system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt,
                          responder=lambda p: "the migration runs before the deploy")

    async def body() -> None:
        await bot.start()
        await bot.step([
            fixtures.message(update_id=6, message_id=6, sender_id=fixtures.ALICE,
                             username="alice", text="the migration script"),
            fixtures.message(update_id=7, message_id=7, sender_id=fixtures.BOB,
                             username="bob", text="keeps timing out"),
            fixtures.message(update_id=8, message_id=8, sender_id=fixtures.ALICE,
                             username="alice", text="Alma any idea"),
        ])
        clock.advance(3.0)
        await bot.step([])
        assert len(transport.sends) == 1, f"{len(transport.sends)} sends"
        assert len(agent.calls) == 1
        await bot.stop()

    run(body())


def test_open_window_does_not_answer_early(store, config, transport, clock,
                                           system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(0.5)
        assert await bot.run_turns() == []
        assert transport.sends == []
        clock.advance(2.0)
        await bot.run_turns()
        assert len(transport.sends) == 1
        await bot.stop()

    run(body())


def test_size_cap_closes_a_never_quiet_window() -> None:
    clock = type("C", (), {"now": 0.0})()
    turn_manager = TurnManager(debounce_seconds=10.0, max_messages=3, clock=lambda: clock.now)
    produced = []
    for index in range(3):
        clock.now += 0.1
        result = turn_manager.observe(message(index, f"m{index}", at=clock.now))
        if result is not None:
            produced.append(result)
    assert len(produced) == 1
    assert len(produced[0].messages) == 3
    assert produced[0].truncated


# -- Requirement 5: supersede an unsent reply ------------------------------


def test_superseded_decision_is_never_sent(store, config, transport, clock,
                                           system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        assert len(transport.sends) == 1
        first = store._query("SELECT decision_id FROM decisions WHERE outcome='reply'")[0]
        # A newer decision supersedes it, and the stale send is not claimable.
        assert store.supersede(first["decision_id"], "dec_newer")
        send = store.get_send(first["decision_id"])
        assert send is not None and send.status == "sent"  # already sent, nothing to undo
        await bot.stop()

    run(body())


def test_a_reply_generated_while_superseded_is_dropped(store, config, transport, clock,
                                                       system_prompt) -> None:
    """The model answered, but a newer turn arrived first: do not send."""
    import asyncio

    async def body() -> None:
        bot, agent = make_bot(store, config, transport, clock, system_prompt)

        original_ask = agent.ask

        async def slow_ask(*, chat_id, prompt, tag):
            # Supersede the decision while the "model" is thinking.
            store.supersede(tag, "dec_interrupting")
            return await original_ask(chat_id=chat_id, prompt=prompt, tag=tag)

        agent.ask = slow_ask
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        assert transport.sends == [], "a superseded reply was still sent"
        rows = store._query("SELECT outcome FROM turns")
        assert rows and rows[0]["outcome"] == "superseded", [dict(r) for r in rows]
        await bot.stop()

    run(body())


# -- Requirement 6: no repeats, no self-loops ------------------------------


def test_identical_reply_is_not_sent_twice(store, config, transport, clock,
                                           system_prompt) -> None:
    """The model repeats itself; the duplicate guard drops the second send."""
    bot, agent = make_bot(store, config, transport, clock, system_prompt,
                          responder=lambda p: "the cron runs in UTC")

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.mention()])
        clock.advance(3.0)
        await bot.step([])
        assert len(transport.sends) == 1
        await bot.step([fixtures.reply_to_bot()])
        clock.advance(3.0)
        await bot.step([])
        assert len(transport.sends) == 1, f"{len(transport.sends)} sends (duplicate passed)"
        rows = store._query("SELECT outcome FROM turns ORDER BY opened_at")
        assert [r["outcome"] for r in rows] == ["sent", "duplicate"], [dict(r) for r in rows]
        await bot.stop()

    run(body())


def test_bot_never_answers_its_own_message(store, config, transport, clock,
                                           system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.bot_own_message()])
        clock.advance(3.0)
        await bot.step([])
        assert agent.calls == []
        assert transport.sends == []
        await bot.stop()

    run(body())


def test_policy_refuses_a_consecutive_bot_message() -> None:
    clock = type("C", (), {"now": 1000.0})()
    policy = ParticipationPolicy(PolicyConfig(proactive_enabled=True))
    bot_message = message(1, "anything", sender=fixtures.ALMA, label="Alma", is_bot=True)
    turn = turn_of(bot_message)
    context = TurnContext(
        turn=turn, recent_messages=[bot_message], bot_user_id=fixtures.ALMA,
        bot_name="Alma", last_bot_message_at=990.0, now=1000.0,
    )
    decision = policy.decide(context)
    assert decision.is_silent
    assert decision.reason in {"own_message", "would_be_consecutive_bot_message"}


# -- Requirement 8: topics and chats never mix -----------------------------


def test_topic_scoping_in_the_store(store) -> None:
    store.record_message(message(1, "topic A text", topic=fixtures.TOPIC_A), update_id=1)
    store.record_message(message(2, "topic B text", topic=fixtures.TOPIC_B), update_id=2)
    store.record_message(message(3, "root text"), update_id=3)

    a = store.recent_messages(fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_A)
    b = store.recent_messages(fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_B)
    root = store.recent_messages(fixtures.TEST_CHAT, topic_id=None)
    assert [m.text for m in a] == ["topic A text"]
    assert [m.text for m in b] == ["topic B text"]
    assert [m.text for m in root] == ["root text"]


def test_two_chats_never_share_history(store) -> None:
    store.record_message(message(1, "chat one", at=1.0), update_id=1)
    other = StoredMessage(chat_id=fixtures.OTHER_CHAT, message_id=1, topic_id=None,
                          sender_id=1, sender_label="x", is_bot=False, text="chat two",
                          created_at=1.0)
    store.record_message(other, update_id=2)
    one = store.recent_messages(fixtures.TEST_CHAT, topic_id=None)
    two = store.recent_messages(fixtures.OTHER_CHAT, topic_id=None)
    assert [m.text for m in one] == ["chat one"]
    assert [m.text for m in two] == ["chat two"]


def test_prompt_for_one_topic_never_contains_another() -> None:
    history = [message(1, "the secret in topic A", topic=fixtures.TOPIC_A, at=1.0)]
    turn_messages = [message(2, "question in topic B", topic=fixtures.TOPIC_B, at=2.0)]
    prompt = build_prompt(
        chat_id=fixtures.TEST_CHAT, topic_id=fixtures.TOPIC_B, history=history,
        turn_messages=turn_messages, bot_user_id=fixtures.ALMA, bot_name="Alma",
    )
    assert "topic B" in prompt.text
    assert "the secret in topic A" not in prompt.text


# -- Requirement 9: proactive rate limiting and the admin switch -----------


def test_proactive_is_off_by_default(store, config, transport, clock, system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.message(
            update_id=9, message_id=9, sender_id=fixtures.BOB, username="bob",
            text="does anyone know if the cron timezone changed?")])
        clock.advance(3.0)
        decisions = await bot.run_turns()
        assert decisions[0].reason == "proactive_disabled"
        assert transport.sends == []
        await bot.stop()

    run(body())


def test_proactive_hourly_cap_is_enforced() -> None:
    policy = ParticipationPolicy(
        PolicyConfig(proactive_enabled=True, min_interval_seconds=0, max_per_hour=2,
                     threshold=0.1)
    )
    text = message(1, "what timezone is the cron in?", at=100.0)
    turn = turn_of(text)
    context = TurnContext(
        turn=turn, recent_messages=[text], bot_user_id=fixtures.ALMA, bot_name="Alma",
        last_bot_message_at=None, replies_last_hour=2, now=100.0,
    )
    decision = policy.decide(context)
    assert decision.is_silent
    assert decision.reason == "proactive_hourly_cap"


def test_proactive_cooldown_is_enforced() -> None:
    policy = ParticipationPolicy(
        PolicyConfig(proactive_enabled=True, min_interval_seconds=300, max_per_hour=10,
                     threshold=0.1)
    )
    text = message(1, "what timezone is the cron in?", at=1000.0)
    turn = turn_of(text)
    context = TurnContext(
        turn=turn, recent_messages=[text], bot_user_id=fixtures.ALMA, bot_name="Alma",
        last_bot_message_at=900.0, replies_last_hour=0, now=1000.0,
    )
    decision = policy.decide(context)
    assert decision.is_silent
    assert decision.reason == "proactive_cooldown"


def test_already_answered_turn_is_left_alone() -> None:
    policy = ParticipationPolicy(
        PolicyConfig(proactive_enabled=True, min_interval_seconds=0, max_per_hour=10)
    )
    question = message(1, "what timezone is the cron in?", sender=fixtures.ALICE, at=100.0)
    answer = message(2, "it's UTC", sender=fixtures.BOB, label="bob", at=101.0)
    turn = turn_of(question, answer)
    context = TurnContext(
        turn=turn, recent_messages=[question, answer], bot_user_id=fixtures.ALMA,
        bot_name="Alma", now=200.0,
    )
    decision = policy.decide(context)
    assert decision.is_silent
    assert decision.reason == "already_answered_by_member"


def test_trivial_messages_are_ignored() -> None:
    policy = ParticipationPolicy(
        PolicyConfig(proactive_enabled=True, min_interval_seconds=0, max_per_hour=10)
    )
    msg = message(1, "lol", sender=fixtures.BOB, label="bob", at=100.0)
    turn = turn_of(msg)
    context = TurnContext(turn=turn, recent_messages=[msg], bot_user_id=fixtures.ALMA,
                          bot_name="Alma", now=100.0)
    decision = policy.decide(context)
    assert decision.is_silent


def test_admin_can_disable_proactive_at_runtime(store, config, transport, clock,
                                                system_prompt) -> None:
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        assert bot.policy.config.proactive_enabled is False
        bot.set_proactive(True)
        assert bot.policy.config.proactive_enabled is True
        assert bot.stats.proactive_enabled is True, "the switch did not reach the stats"
        bot.set_proactive(False)
        assert bot.policy.config.proactive_enabled is False
        assert bot.stats.proactive_enabled is False
        await bot.stop()

    run(body())


# -- Scoring: not a random constant ---------------------------------------


def test_score_rewards_questions_and_punishes_answered_turns() -> None:
    question = message(1, "how does the cron handle timezones?", at=100.0)
    unanswered = TurnContext(
        turn=turn_of(question), recent_messages=[question], bot_user_id=fixtures.ALMA,
        bot_name="Alma", now=110.0,
    )
    answered_context = TurnContext(
        turn=turn_of(question, message(2, "it uses UTC", sender=fixtures.BOB, label="bob",
                                      at=101.0)),
        recent_messages=[question], bot_user_id=fixtures.ALMA, bot_name="Alma", now=110.0,
    )
    high, components = proactive_score(unanswered)
    low, _ = proactive_score(answered_context)
    assert high > low
    assert components["question"] > 0
    assert 0.0 <= low <= 1.0 and 0.0 <= high <= 1.0


def test_score_decays_with_message_age() -> None:
    fresh = message(1, "what does the cron do?", at=1000.0)
    stale = message(2, "what does the cron do?", at=1000.0)
    fresh_ctx = TurnContext(turn=turn_of(fresh), recent_messages=[fresh],
                            bot_user_id=fixtures.ALMA, bot_name="Alma", now=1010.0)
    stale_ctx = TurnContext(turn=turn_of(stale), recent_messages=[stale],
                            bot_user_id=fixtures.ALMA, bot_name="Alma", now=1000.0 + 7200)
    fresh_score, _ = proactive_score(fresh_ctx)
    stale_score, _ = proactive_score(stale_ctx)
    assert fresh_score > stale_score


def test_score_is_deterministic() -> None:
    msg = message(1, "why is the cron late?", at=100.0)
    context = TurnContext(turn=turn_of(msg), recent_messages=[msg],
                          bot_user_id=fixtures.ALMA, bot_name="Alma", now=105.0)
    first, _ = proactive_score(context)
    second, _ = proactive_score(context)
    assert first == second


def test_judge_only_sees_the_borderline_band() -> None:
    calls = []

    def judge(context, score):
        calls.append(score)
        return {"speak": False, "why": "not worth it"}

    policy = ParticipationPolicy(
        PolicyConfig(proactive_enabled=True, min_interval_seconds=0, max_per_hour=10,
                     threshold=0.30),
        judge=judge,
    )
    # A clearly addressed message is way above threshold + 0.18 → no judge call.
    msg = message(1, "Alma why does the cron fail?", at=100.0)
    turn = turn_of(msg)
    context = TurnContext(turn=turn, recent_messages=[msg], bot_user_id=fixtures.ALMA,
                          bot_name="Alma", now=101.0)
    decision = policy.decide(context)
    assert decision.is_reply  # mention wins before proactive scoring even runs
    assert calls == []


def test_judge_declining_keeps_the_bot_quiet() -> None:
    """A score inside the borderline band is submitted to the judge, and a 'no'
    from the judge is binding. The threshold is set just under the achievable
    score so the call actually lands in the band."""
    msg = message(1, "anyone know about the cron timezone thing", at=100.0)
    turn = turn_of(msg)
    context = TurnContext(turn=turn, recent_messages=[msg], bot_user_id=fixtures.ALMA,
                          bot_name="Alma", now=101.0)
    score, _ = proactive_score(context)
    assert 0.0 < score < 1.0, f"unexpected score {score}"

    calls: list[float] = []

    def judge(ctx, value):
        calls.append(value)
        return {"speak": False, "why": "already covered"}

    # Band is [threshold, threshold + 0.18); put the score inside it.
    policy = ParticipationPolicy(
        PolicyConfig(proactive_enabled=True, min_interval_seconds=0, max_per_hour=10,
                     threshold=score - 0.01),
        judge=judge,
    )
    decision = policy.decide(context)
    assert calls == [score], "the judge was not consulted for a borderline score"
    assert decision.is_silent
    assert decision.reason == "model_judge_declined", decision.reason
    assert decision.detail["judge"]["why"] == "already covered"


# -- Reply parsing ---------------------------------------------------------


@pytest.mark.parametrize("raw", ["", "   ", "\n\n", "😂😂", "..."])
def test_unusable_model_output_is_not_a_reply(raw: str) -> None:
    assert parse_reply(raw, bot_name="Alma") is None


def test_model_echoing_its_own_label_is_stripped() -> None:
    reply = parse_reply("Alma (you, a bot): it runs at 09:00 UTC", bot_name="Alma")
    assert reply is not None
    assert reply.text == "it runs at 09:00 UTC"


def test_model_echoing_the_transcript_with_marker_keeps_only_the_answer() -> None:
    raw = (
        "[chat -100]\nalice: question?\n--- new messages ---\nAlma: the answer is 42"
    )
    reply = parse_reply(raw, bot_name="Alma")
    assert reply is not None
    assert reply.text == "the answer is 42"


def test_overlong_reply_is_truncated() -> None:
    reply = parse_reply("x" * 5000, bot_name="Alma")
    assert reply is not None
    assert reply.truncated
    assert len(reply.text) <= 1200

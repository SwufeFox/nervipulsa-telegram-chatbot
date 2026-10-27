"""Saying the bot's name is not the same as talking to it.

A group run surfaced this: alice wrote "先按 Alma 说的试试" — a normal thing to
say while discussing the bot's advice with bob — and the bot answered with a
reply about the deploy. It interrupted a conversation *about* itself.

Before the fix `_mentions` matched any occurrence of the name, so third-person
narration counted as an address. These cases pin the distinction: an explicit
`@` always counts, a bare name counts only when the sentence is aimed at it.
"""
from __future__ import annotations

import pytest

from app import fixtures
from app.policy import _mentions
from tests.test_behavior import make_bot


@pytest.mark.parametrize("text", [
    "@Alma 你是真人还是机器人",
    "@alma 在吗",
    "@Alma spec.timeZone 没写会怎样",
    "Alma 呢",
    "Alma",
    "@Alma",
    "alma 你说呢",
    "Alma 你怎么看",
])
def test_an_address_counts(text: str) -> None:
    assert _mentions(text, "Alma") is True


@pytest.mark.parametrize("text", [
    "先按 Alma 说的试试",
    "问一下 Alma 那个方案",
    "Alma 的回答挺准",
    "刚 alice 说的和 Alma 说的一样",
    "按 Alma 的建议改了",
    "Alma 上次提过这个",
])
def test_talk_about_the_bot_does_not_count(text: str) -> None:
    assert _mentions(text, "Alma") is False


def test_third_person_mention_does_not_produce_a_reply(
    store, config, transport, clock, system_prompt
) -> None:
    """The end-to-end consequence: narration in the group gets silence."""
    bot, agent = make_bot(store, config, transport, clock, system_prompt)

    async def body() -> None:
        await bot.start()
        await bot.step([fixtures.message(
            update_id=501, message_id=501, sender_id=fixtures.BOB,
            username="bob", text="先按 Alma 说的试试",
        )])
        clock.advance(3.0)
        await bot.step([])
        assert transport.sends == [], transport.sends
        assert agent.calls == [], "the model was called for a third-person mention"
        await bot.stop()

    import asyncio
    asyncio.run(body())

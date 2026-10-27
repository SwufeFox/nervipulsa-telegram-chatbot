"""Decide whether the bot participates. Most turns end in recorded silence.

The default is silence. A turn only escalates when something specific argues for
speaking, and the arguments are named rather than folded into one opaque
probability.

`proactive_score` is a sum of independently observable signals — relevance,
whether an answer already exists, how fresh the message is, how recently the bot
spoke. It is deterministic given its inputs: no RNG, so a reviewer can replay a
decision from its recorded `detail` block and get the same number, and the tests
do not have to be probabilistic.

Order of evaluation:

1. Cheap structural gates (no text, own message, another bot, self-loop).
   These never cost a model call.
2. Explicit trigger (mention or reply-to-bot) → reply, always.
3. Proactive gates: feature on? under the hourly cap? past the cooldown?
4. `proactive_score` against a threshold. Only the narrow band around the
   threshold is offered to the model judge; the extremes are decided locally.

Step 4 is what keeps an idle group from costing one LLM request per message.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .store import StoredMessage
from .turns import Turn

# Reasons are stable machine codes; diagnostics and tests assert on them.
REASON_MENTION = "addressed_by_mention"
REASON_REPLY = "addressed_by_reply"
REASON_NO_TEXT = "no_text_content"
REASON_SELF = "own_message"
REASON_OTHER_BOT = "addressed_to_other_bot"
REASON_SELF_LOOP = "would_be_consecutive_bot_message"
REASON_PROACTIVE_OFF = "proactive_disabled"
REASON_COOLDOWN = "proactive_cooldown"
REASON_HOURLY = "proactive_hourly_cap"
REASON_LOW_SCORE = "proactive_score_below_threshold"
REASON_ALREADY_ANSWERED = "already_answered_by_member"
REASON_DUPLICATE = "would_repeat_recent_reply"
REASON_JUDGE_DECLINED = "model_judge_declined"
REASON_JUDGE_UNAVAILABLE = "model_judge_unavailable"
REASON_BUDGET = "turn_budget_exhausted"

TRIGGER_MENTION = "mention"
TRIGGER_REPLY = "reply"
TRIGGER_PROACTIVE = "proactive"

# Score components. Summing these is the whole decision.
W_QUESTION = 0.30          # the turn asks something answerable
W_ADDRESS_NAME = 0.35      # the bot's name appears in the text
W_TOPIC_MATCH = 0.15       # shares vocabulary with recent bot replies
W_UNANSWERED = 0.20        # no human answered after the question
W_FRESHNESS = 0.10         # message is recent, not backlog
W_NOVELTY = 0.10           # differs from what the bot recently said
P_ANSWERED_PENALTY = 0.55  # a human already answered
P_RECENT_SELF_PENALTY = 0.40  # the bot spoke very recently
P_TRIVIAL_PENALTY = 0.50   # "ok", "lol", a bare emoji

TRIVIAL_RE = re.compile(
    r"^\s*(ok(ay)?|k|lol|lmao|haha+|hmm+|wow|nice|cool|thx|thanks|ty|"
    r"\+1|👍|😂|🤣|❤️|👌|对|是的|好的|嗯+|哈+|草|6+)[\s.!?~。！？]*$",
    re.IGNORECASE,
)
QUESTION_RE = re.compile(
    r"(\?|？|\bhow\b|\bwhy\b|\bwhat\b|\bwhen\b|\bwhere\b|\bwho\b|\bwhich\b|"
    r"\bcan\b|\bcould\b|\bshould\b|\bis it\b|\bdoes\b|\bdo you\b|"
    r"怎么|为什么|为啥|如何|什么|哪个|哪里|能不能|可以吗|是不是|有没有)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Decision:
    """A participation decision. Silence is a first-class outcome."""

    outcome: str                    # "reply" | "silent"
    reason: str                     # stable machine code, see REASON_*
    trigger: str                    # TRIGGER_*
    confidence: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        assert self.outcome in {"reply", "silent"}, self.outcome
        assert self.trigger in {TRIGGER_MENTION, TRIGGER_REPLY, TRIGGER_PROACTIVE}
        assert 0.0 <= self.confidence <= 1.0

    @property
    def is_reply(self) -> bool:
        return self.outcome == "reply"

    @property
    def is_silent(self) -> bool:
        return self.outcome == "silent"

    @classmethod
    def reply(cls, *, reason: str, trigger: str, confidence: float,
              detail: dict[str, Any] | None = None) -> "Decision":
        return cls("reply", reason, trigger, confidence, detail or {})

    @classmethod
    def silent(cls, *, reason: str, trigger: str, confidence: float = 0.0,
               detail: dict[str, Any] | None = None) -> "Decision":
        return cls("silent", reason, trigger, confidence, detail or {})


@dataclass
class TurnContext:
    """Everything the policy may look at. Passed in, never fetched."""

    turn: Turn
    recent_messages: Sequence[StoredMessage]
    bot_user_id: int | None
    bot_name: str
    last_bot_message_at: float | None = None
    last_bot_reply_text: str | None = None
    replies_last_hour: int = 0
    now: float = 0.0

    def __post_init__(self) -> None:
        if not self.now:
            self.now = time.time()


@dataclass
class PolicyConfig:
    proactive_enabled: bool = False
    min_interval_seconds: float = 240.0
    max_per_hour: int = 4
    threshold: float = 0.62
    has_judge: bool = False


class ParticipationPolicy:
    """Deterministic rules first; the model only judges the narrow band."""

    def __init__(
        self,
        config: PolicyConfig,
        *,
        judge: Callable[[TurnContext, float], Any] | None = None,
    ) -> None:
        self.config = config
        self._judge = judge

    # -- entry point -------------------------------------------------------

    def decide(self, context: TurnContext) -> Decision:
        blocked = self._structural_gate(context)
        if blocked is not None:
            return blocked

        explicit = self._explicit_trigger(context)
        if explicit is not None:
            return explicit

        return self._proactive_decision(context)

    # -- step 1: structural gates -----------------------------------------

    def _structural_gate(self, context: TurnContext) -> Decision | None:
        turn = context.turn
        substantive = [m for m in turn.messages if m.text.strip()]
        detail = {"messages": len(turn.messages), "substantive": len(substantive)}

        if not substantive:
            return Decision.silent(
                reason=REASON_NO_TEXT, trigger=TRIGGER_PROACTIVE,
                confidence=0.0, detail=detail,
            )

        # Every message in the window came from us: never answer ourselves.
        if context.bot_user_id is not None and all(
            m.sender_id == context.bot_user_id for m in substantive
        ):
            return Decision.silent(
                reason=REASON_SELF, trigger=TRIGGER_PROACTIVE,
                confidence=0.0, detail=detail,
            )

        # Check for self-reply loop before anything else can approve a reply.
        last = substantive[-1]
        mentions = _mentions(last.text, context.bot_name)
        replies_to_bot = _is_bot_sender(context, last) is False and last.reply_to_bot
        if not mentions and not replies_to_bot:
            if context.last_bot_message_at is not None:
                gap = context.now - context.last_bot_message_at
                # Two bot messages with no human in between reads as a monologue.
                if _has_human_since(context) is False:
                    return Decision.silent(
                        reason=REASON_SELF_LOOP, trigger=TRIGGER_PROACTIVE,
                        confidence=0.0, detail={**detail, "gap_seconds": round(gap, 1)},
                    )
        return None

    # -- step 2: explicit trigger -----------------------------------------

    def _explicit_trigger(self, context: TurnContext) -> Decision | None:
        turn = context.turn
        substantive = [m for m in turn.messages if m.text.strip() and not _is_bot_sender(context, m)]

        for message in reversed(substantive):
            if _is_addressed_to_other_bot(context, message):
                return Decision.silent(
                    reason=REASON_OTHER_BOT, trigger=TRIGGER_MENTION,
                    confidence=0.0, detail={"message_id": message.message_id},
                )
            if _mentions(message.text, context.bot_name):
                return Decision.reply(
                    reason=REASON_MENTION, trigger=TRIGGER_MENTION, confidence=1.0,
                    detail={"message_id": message.message_id, "messages": len(turn.messages)},
                )
            if message.reply_to_bot:
                return Decision.reply(
                    reason=REASON_REPLY, trigger=TRIGGER_REPLY, confidence=1.0,
                    detail={"message_id": message.message_id, "messages": len(turn.messages)},
                )
        return None

    # -- step 3 + 4: proactive --------------------------------------------

    def _proactive_decision(self, context: TurnContext) -> Decision:
        config = self.config
        score, components = proactive_score(context)
        detail: dict[str, Any] = {
            "score": round(score, 3),
            "threshold": config.threshold,
            "components": {k: round(v, 3) for k, v in components.items()},
            "messages": len(context.turn.messages),
        }

        if not config.proactive_enabled:
            return Decision.silent(
                reason=REASON_PROACTIVE_OFF, trigger=TRIGGER_PROACTIVE,
                confidence=0.0, detail=detail,
            )

        if context.last_bot_message_at is not None:
            gap = context.now - context.last_bot_message_at
            if gap < config.min_interval_seconds:
                return Decision.silent(
                    reason=REASON_COOLDOWN, trigger=TRIGGER_PROACTIVE,
                    confidence=0.0,
                    detail={**detail, "seconds_since_last_reply": round(gap, 1)},
                )

        if context.replies_last_hour >= config.max_per_hour:
            return Decision.silent(
                reason=REASON_HOURLY, trigger=TRIGGER_PROACTIVE,
                confidence=0.0, detail={**detail, "replies_last_hour": context.replies_last_hour},
            )

        if _answered_by_member(context):
            return Decision.silent(
                reason=REASON_ALREADY_ANSWERED, trigger=TRIGGER_PROACTIVE,
                confidence=0.0, detail=detail,
            )

        if _repeats_recent_reply(context):
            return Decision.silent(
                reason=REASON_DUPLICATE, trigger=TRIGGER_PROACTIVE,
                confidence=0.0, detail=detail,
            )

        if score < config.threshold:
            return Decision.silent(
                reason=REASON_LOW_SCORE, trigger=TRIGGER_PROACTIVE,
                confidence=0.0, detail=detail,
            )

        # Only the borderline band is worth a model request. Anything far above
        # the threshold is already a clear yes.
        if self._judge is not None and not config.has_judge:
            config.has_judge = True

        if score < config.threshold + 0.18:
            if self._judge is None:
                # No judge configured: the deterministic score decides, and the
                # decision is recorded with the reason that says so.
                return Decision.reply(
                    reason="proactive_score_accepted", trigger=TRIGGER_PROACTIVE,
                    confidence=score, detail={**detail, "judge": "unavailable"},
                )
            verdict = self._judge(context, score)
            approved, note = _interpret_verdict(verdict)
            detail["judge"] = note
            if not approved:
                return Decision.silent(
                    reason=REASON_JUDGE_DECLINED,
                    trigger=TRIGGER_PROACTIVE,
                    confidence=score,
                    detail=detail,
                )
            return Decision.reply(
                reason="proactive_score_accepted_with_judge",
                trigger=TRIGGER_PROACTIVE,
                confidence=score,
                detail=detail,
            )

        return Decision.reply(
            reason="proactive_score_accepted", trigger=TRIGGER_PROACTIVE,
            confidence=score, detail={**detail, "judge": "not_needed"},
        )


# -- scoring ---------------------------------------------------------------


def proactive_score(context: TurnContext) -> tuple[float, dict[str, float]]:
    """Score in [0, 1]. Every term is observable and independently justified."""
    turn = context.turn
    substantive = [m for m in turn.messages if m.text.strip() and not _is_bot_sender(context, m)]
    if not substantive:
        return 0.0, {"relevance": 0.0, "question": 0.0, "unanswered": 0.0,
                     "freshness": 0.0, "novelty": 0.0, "penalties": 1.0}

    combined = " ".join(m.text for m in substantive)

    question = QUESTION_RE.search(combined) is not None
    address = _mentions(combined, context.bot_name)
    topic = _topic_overlap(combined, context.last_bot_reply_text)
    unanswered = not _answered_by_member(context)
    freshness = _freshness(context)
    novelty = 0.0 if _repeats_recent_reply(context) else 1.0
    trivial = all(TRIVIAL_RE.match(m.text) for m in substantive)

    raw = (
        W_QUESTION * float(question)
        + W_ADDRESS_NAME * float(address)
        + W_TOPIC_MATCH * topic
        + W_UNANSWERED * float(unanswered)
        + W_FRESHNESS * freshness
        + W_NOVELTY * novelty
    )
    penalty = 0.0
    if not unanswered:
        penalty += P_ANSWERED_PENALTY
    if context.last_bot_message_at is not None:
        gap = context.now - context.last_bot_message_at
        if gap < max(120.0, context.now * 0 + 120.0):
            penalty += P_RECENT_SELF_PENALTY
    if trivial:
        penalty += P_TRIVIAL_PENALTY

    score = max(0.0, min(1.0, raw - penalty))
    components = {
        "relevance": float(address) * W_ADDRESS_NAME + topic * W_TOPIC_MATCH,
        "question": float(question) * W_QUESTION,
        "unanswered": float(unanswered) * W_UNANSWERED,
        "freshness": freshness * W_FRESHNESS,
        "novelty": novelty * W_NOVELTY,
        "penalties": penalty,
    }
    return score, components


def _freshness(context: TurnContext) -> float:
    """1.0 for a message from the last few minutes, decaying to 0 over an hour."""
    if not context.turn.messages:
        return 0.0
    newest = max(m.created_at for m in context.turn.messages)
    age = max(0.0, context.now - newest)
    if age <= 180:
        return 1.0
    if age >= 3600:
        return 0.0
    return 1.0 - (age - 180) / (3600 - 180)


def _topic_overlap(text: str, last_bot_reply: str | None) -> float:
    if not last_bot_reply:
        return 0.0
    left = {w for w in re.findall(r"[a-z0-9]{3,}", text.lower())}
    right = {w for w in re.findall(r"[a-z0-9]{3,}", last_bot_reply.lower())}
    if not left or not right:
        return 0.0
    return min(1.0, len(left & right) / 4.0)


def _answered_by_member(context: TurnContext) -> bool:
    """Did a human answer after the newest question in the turn?"""
    turn = context.turn
    indices = [
        index for index, message in enumerate(turn.messages)
        if message.text.strip() and QUESTION_RE.search(message.text)
    ]
    if not indices:
        return False
    last_question = indices[-1]
    for message in turn.messages[last_question + 1 :]:
        if _is_bot_sender(context, message):
            continue
        if message.text.strip():
            return True
    return False


def _repeats_recent_reply(context: TurnContext) -> bool:
    """Cheap bag-of-words guard against saying the same thing twice."""
    if not context.last_bot_reply_text:
        return False
    candidate = " ".join(m.text for m in context.turn.messages)
    left = {w for w in re.findall(r"[a-z0-9]{4,}", candidate.lower())}
    right = {w for w in re.findall(r"[a-z0-9]{4,}", context.last_bot_reply_text.lower())}
    if len(left) < 4 or len(right) < 4:
        return False
    return len(left & right) / len(left | right) > 0.6


def _has_human_since(context: TurnContext) -> bool:
    """True when a human message exists in the window (so it is not a monologue)."""
    return any(
        not _is_bot_sender(context, m) and m.text.strip()
        for m in context.turn.messages
    )


def _is_bot_sender(context: TurnContext, message: StoredMessage) -> bool:
    if message.is_bot:
        return True
    return context.bot_user_id is not None and message.sender_id == context.bot_user_id


def _is_addressed_to_other_bot(context: TurnContext, message: StoredMessage) -> bool:
    """The message names some *other* bot, and not us."""
    if message.is_bot:
        return False
    other = re.findall(r"@([A-Za-z0-9_]{4,32})", message.text)
    if not other:
        return False
    ours = context.bot_name.lower().lstrip("@")
    return all(name.lower() != ours for name in other)


def _mentions(text: str, bot_name: str) -> bool:
    if not bot_name:
        return False
    name = re.escape(bot_name.lstrip("@"))
    return re.search(rf"(?<![A-Za-z0-9_])@?{name}(?![A-Za-z0-9_])", text, re.IGNORECASE) is not None


def _interpret_verdict(verdict: Any) -> tuple[bool, dict[str, Any]]:
    """Normalize a judge result. Anything unclear is a refusal."""
    if isinstance(verdict, bool):
        return verdict, {"verdict": verdict}
    if isinstance(verdict, dict):
        raw = verdict.get("speak")
        if isinstance(raw, bool):
            return raw, dict(verdict)
        return False, {**dict(verdict), "note": "no boolean 'speak' field"}
    if isinstance(verdict, str):
        lowered = verdict.strip().lower()
        if lowered.startswith("yes") or lowered.startswith("speak"):
            return True, {"raw": verdict[:200]}
        return False, {"raw": verdict[:200]}
    return False, {"note": f"unusable judge result: {type(verdict).__name__}"}

"""Build the string the model sees, and parse what comes back.

Everything the model needs about *who said what* is rendered into text, because
the runtime drops event payload fields at the projector (see
docs/architecture/runtime-integration.md §2). That makes this module the single
place where chat identity becomes model input — and therefore the single place
that has to be careful about injection.

Layout of one request:

    [chat -1001234567890 topic 3]
    alice: is the deploy done?
    bob: yes, five minutes ago
    Alma (you, a bot): thanks for confirming.
    --- new messages ---
    carol: great

Rules:
* Each rendered line is prefixed with a participant name and a colon. A message
  body can contain newlines, so continuation lines are indented. This stops a
  message from forging a new speaker line.
* Control characters are stripped and length is bounded per message.
* A message that looks like an instruction ("ignore previous instructions") is
  still rendered as data — the system prompt is what tells the model not to obey
  it. The renderer's job is to make the boundary unambiguous, not to censor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from .store import StoredMessage

MAX_INPUT_MESSAGE_CHARS = 1200
MAX_REPLY_CHARS = 1200
CONTEXT_MARKER = "--- new messages ---"
_FORBIDDEN_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(text: str, *, limit: int = MAX_INPUT_MESSAGE_CHARS) -> str:
    """Strip control characters, collapse blank runs, bound the length."""
    cleaned = _FORBIDDEN_CONTROL.sub("", text)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    # Cap before collapsing so a pathological input cannot blow up a regex.
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def render_speaker(message: StoredMessage, *, bot_user_id: int | None, bot_name: str) -> str:
    """A safe single speaker label. Never taken from user-controlled markup."""
    if message.is_bot or (bot_user_id is not None and message.sender_id == bot_user_id):
        return f"{sanitize_label(bot_name)} (you, a bot)"
    return sanitize_label(message.sender_label)


def sanitize_label(label: str) -> str:
    cleaned = _FORBIDDEN_CONTROL.sub("", label or "unknown").strip()
    cleaned = cleaned.replace("\n", " ").replace(":", "：")
    return cleaned[:48] or "unknown"


def render_line(message: StoredMessage, *, bot_user_id: int | None, bot_name: str) -> str:
    speaker = render_speaker(message, bot_user_id=bot_user_id, bot_name=bot_name)
    body = sanitize_text(message.text)
    if not body:
        return f"{speaker}: (empty)"
    # Indent continuations so a newline inside a message cannot look like a new
    # speaker. Two spaces is the standard "continuation" convention.
    body = body.replace("\n", "\n  ")
    return f"{speaker}: {body}"


@dataclass
class PromptBuild:
    text: str
    included_messages: int
    dropped_messages: int


def build_prompt(
    *,
    chat_id: int,
    topic_id: int | None,
    history: Sequence[StoredMessage],
    turn_messages: Sequence[StoredMessage],
    bot_user_id: int | None,
    bot_name: str,
    max_chars: int = 12_000,
) -> PromptBuild:
    """Render context + the turn being answered.

    `history` is older messages already in the conversation; `turn_messages` is
    the window under decision. The newest turn message is always included: a
    prompt that omits the message being answered is a bug, not a truncation.

    The topic filter is applied here, not only at the store. A caller that hands
    over a wider history — a bug, a future refactor, a test — must not be able to
    leak one topic's text into another topic's prompt.
    """
    turn_ids = {(m.chat_id, m.message_id) for m in turn_messages}
    history = [
        m for m in history
        if (m.chat_id, m.message_id) not in turn_ids
        and m.chat_id == chat_id
        and m.topic_id == topic_id
    ]

    header = f"[chat {chat_id}"
    if topic_id is not None:
        header += f" topic {topic_id}"
    header += "]"

    turn_lines = [
        render_line(m, bot_user_id=bot_user_id, bot_name=bot_name) for m in turn_messages
    ]
    # The header + marker + turn are the non-negotiable minimum.
    fixed = len(header) + len(CONTEXT_MARKER) + sum(len(line) + 1 for line in turn_lines) + 8

    kept: list[str] = []
    budget = max_chars - fixed
    for message in reversed(list(history)):
        line = render_line(message, bot_user_id=bot_user_id, bot_name=bot_name)
        if len(line) + 1 > budget:
            break
        kept.append(line)
        budget -= len(line) + 1
    kept.reverse()

    parts = [header]
    parts.extend(kept)
    parts.append(CONTEXT_MARKER)
    parts.extend(turn_lines)
    text = "\n".join(parts)
    return PromptBuild(
        text=text,
        included_messages=len(kept) + len(turn_lines),
        dropped_messages=len(history) - len(kept),
    )


@dataclass
class Reply:
    text: str
    truncated: bool


_ECHO_PREFIXES = (
    "alma (you, a bot):",
    "alma:",
    "[chat ",
    CONTEXT_MARKER,
)


def parse_reply(raw: str, *, bot_name: str, dry_run: bool = False) -> Reply | None:
    """Turn model output into text worth sending, or None for "do not send".

    None is not a failure. It is the correct result when the model produced
    nothing, produced only whitespace, or echoed the transcript back at us.

    In dry-run the caller prefixes with a marker; this function never adds it so
    that the stored text and the sent text stay identical between modes.
    """
    if not isinstance(raw, str):
        return None
    text = _FORBIDDEN_CONTROL.sub("", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not text:
        return None

    # Strip a leading self-label the model sometimes copies from the transcript.
    lowered = text.lower()
    for prefix in _ECHO_PREFIXES:
        candidate = prefix.lower()
        if lowered.startswith(candidate):
            text = text[len(prefix):].lstrip(" :")
            break

    # If the model echoed the whole transcript, keep only what follows the marker.
    if CONTEXT_MARKER in text:
        text = text.split(CONTEXT_MARKER)[-1].strip()
        if text.lower().startswith(f"{bot_name.lower()}:"):
            text = text.split(":", 1)[1].strip()

    if not text:
        return None

    # A message that is only punctuation/emoji is not a reply.
    if not re.search(r"[A-Za-z0-9\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", text):
        return None

    # Collapse a multi-paragraph answer into chat-sized text.
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if len(paragraphs) > 1:
        text = "\n".join(paragraphs)

    truncated = False
    if len(text) > MAX_REPLY_CHARS:
        text = text[: MAX_REPLY_CHARS - 1] + "…"
        truncated = True
    return Reply(text=text, truncated=truncated)


def is_duplicate_of_history(text: str, history: Sequence[StoredMessage]) -> bool:
    """Exact-match guard against resending a line the bot already sent."""
    normalized = _normalize(text)
    if not normalized:
        return False
    return any(_normalize(m.text) == normalized for m in history if m.is_bot)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())

"""Debounce chat messages into turns.

Three rapid messages must produce one reply, not three. A turn is a run of
messages in one (chat, topic) separated by less than `debounce_seconds` of
silence, or bounded by `max_messages` so a permanently busy group still gets
answers.

The clock is injected. Tests drive it directly instead of sleeping, which is
the difference between a suite that takes 3.5s per case and one that takes 3ms.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from .store import StoredMessage


@dataclass
class Turn:
    """A closed window of messages awaiting a participation decision."""

    turn_id: str
    chat_id: int
    topic_id: int | None
    messages: list[StoredMessage]
    opened_at: float
    closed_at: float
    # True when the window was closed because it hit max_messages, not silence.
    truncated: bool = False

    @property
    def first_message_id(self) -> int:
        return self.messages[0].message_id

    @property
    def last_message_id(self) -> int:
        return self.messages[-1].message_id

    def texts(self) -> list[str]:
        return [m.text for m in self.messages]


@dataclass
class _OpenTurn:
    chat_id: int
    topic_id: int | None
    messages: list[StoredMessage] = field(default_factory=list)
    opened_at: float = 0.0
    last_at: float = 0.0
    turn_id: str = ""


class TurnManager:
    """Groups messages per (chat, topic). One open turn per key, never across keys."""

    def __init__(
        self,
        *,
        debounce_seconds: float = 3.5,
        max_messages: int = 12,
        clock=time.monotonic,
    ) -> None:
        if debounce_seconds < 0:
            raise ValueError("debounce_seconds must be >= 0")
        if max_messages < 1:
            raise ValueError("max_messages must be >= 1")
        self.debounce_seconds = debounce_seconds
        self.max_messages = max_messages
        self._clock = clock
        self._open: dict[tuple[int, int | None], _OpenTurn] = {}

    def observe(self, message: StoredMessage) -> Turn | None:
        """Add a message. Returns a turn if the window is now full."""
        key = (message.chat_id, message.topic_id)
        now = self._clock()
        current = self._open.get(key)
        if current is None:
            current = _OpenTurn(
                chat_id=message.chat_id,
                topic_id=message.topic_id,
                opened_at=now,
                turn_id=f"turn_{uuid.uuid4().hex[:12]}",
            )
            self._open[key] = current
        current.messages.append(message)
        current.last_at = now
        if len(current.messages) >= self.max_messages:
            return self._close(key, truncated=True, now=now)
        return None

    def ready(self, *, now: float | None = None) -> list[Turn]:
        """Close every window whose quiet period has elapsed. Oldest first."""
        moment = self._clock() if now is None else now
        closed: list[Turn] = []
        for key in list(self._open):
            current = self._open[key]
            if moment - current.last_at >= self.debounce_seconds:
                closed.append(self._close(key, truncated=False, now=moment))
        closed.sort(key=lambda turn: turn.opened_at)
        return closed

    def flush(self) -> list[Turn]:
        """Close every open window now. Used on shutdown and by tests."""
        now = self._clock()
        return [self._close(key, truncated=False, now=now) for key in list(self._open)]

    def open_keys(self) -> list[tuple[int, int | None]]:
        return list(self._open)

    def has_open(self, chat_id: int, topic_id: int | None) -> bool:
        return (chat_id, topic_id) in self._open

    def _close(self, key: tuple[int, int | None], *, truncated: bool, now: float) -> Turn:
        current = self._open.pop(key)
        return Turn(
            turn_id=current.turn_id,
            chat_id=current.chat_id,
            topic_id=current.topic_id,
            messages=list(current.messages),
            opened_at=current.opened_at,
            closed_at=now,
            truncated=truncated,
        )

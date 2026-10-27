"""Shared fixtures. No network, no real clock, no real Telegram."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fixtures  # noqa: E402
from app.config import AppConfig, load_default_profile  # noqa: E402
from app.store import Store  # noqa: E402


class FakeClock:
    """A clock you advance by hand. Debounce tests never sleep."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class FakeTransport:
    """Records every API call. Success by default; failures are scripted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.next_message_id = 9000
        self.updates: list[list[dict]] = []
        self.fail_next: list[Exception] = []
        self.send_failures: list[Exception] = []

    def call(self, method: str, payload: dict) -> dict:
        # A failed send is recorded as an attempt, then raises: the real
        # transport also reaches the network before it can fail.
        if method == "sendMessage" and self.send_failures:
            self.calls.append((method, payload))
            raise self.send_failures.pop(0)
        self.calls.append((method, payload))
        if method == "getUpdates":
            return {"ok": True, "result": self.updates.pop(0) if self.updates else []}
        if method == "sendMessage":
            if self.send_failures:
                raise self.send_failures.pop(0)
            self.next_message_id += 1
            return {"ok": True, "result": {"message_id": self.next_message_id}}
        if method == "getMe":
            return {"ok": True, "result": {"id": fixtures.ALMA, "is_bot": True,
                                           "username": "alma_bot"}}
        if method == "deleteMessage":
            return {"ok": True, "result": True}
        return {"ok": True, "result": {}}

    @property
    def sends(self) -> list[dict]:
        return [payload for method, payload in self.calls if method == "sendMessage"]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "bot.sqlite3")
    yield s
    s.close()


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        bot_name="Alma",
        bot_username="alma_bot",
        allowed_chat_ids=(fixtures.TEST_CHAT,),
        admin_user_ids=(fixtures.ALICE,),
        dry_run=False,
        proactive_enabled=False,
        debounce_seconds=2.0,
        history_messages=20,
        workspace=str(tmp_path),
        model="scripted",
    )


@pytest.fixture
def system_prompt() -> str:
    return load_default_profile()


def run(coro):
    """Run a coroutine in a fresh loop. Tests stay synchronous and readable."""
    return asyncio.run(coro)

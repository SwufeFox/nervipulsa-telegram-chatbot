"""Entry point: long polling, graceful shutdown, and the offline replay modes.

Runs:
    python -m app.main --check              # validate config, print redacted report
    python -m app.main --replay fixtures/   # offline, scripted model, no network
    python -m app.main                      # live long polling

Live mode requires TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS, and a model
key. `--dry-run` keeps the whole pipeline but never calls the Telegram send API,
so a first run against a real group cannot post anything.

Admin commands are read from the chat and are NOT sent to the model:
    /pause /resume /proactive on|off /stats /purge
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from telegram import Bot

from .agent import NervipulsaAgent, ScriptedAgent
from .bot import GroupAgent
from .config import AppConfig, load_config, load_profile, load_system_prompt
from .store import Store
from .telegram import PythonTelegramBotTransport, TelegramClient, TransportError
from .turns import TurnManager


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Never let a library log the token or a full update payload.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_settings(config: AppConfig):
    from nervipulsa.config import Settings

    return Settings(
        provider=config.provider,
        base_url=config.base_url or "http://127.0.0.1:3425/v1",
        model=config.model,
        api_key=config.model_api_key or "magpie",
        workspace=config.workspace,
        context_limit=config.context_limit,
        max_output_tokens=config.max_output_tokens,
        # The model HTTP timeout has to follow the reply budget: an unlimited
        # reply can legitimately take minutes. `default_timeout` stays short
        # because it bounds tool execution, and this application exposes no
        # tools at all.
        max_timeout=config.request_timeout,
        default_timeout=min(30.0, config.request_timeout),
    )


def build_agent_factory(config: AppConfig, system_prompt: str, *, offline: bool = False):
    settings = build_settings(config)

    def factory(spec):
        if offline:
            return ScriptedAgent(_offline_responder)
        return NervipulsaAgent(
            settings=settings,
            workspace=spec.workspace,
            system_prompt=system_prompt,
            timeout=config.request_timeout,
            # The autonomous path gives the model a closed set of action
            # tools. Passing the catalog here (not later) matters: the actor
            # snapshots its schema list at construction.
            tool_catalog=getattr(spec, "tool_catalog", None),
            response_commit_hook=getattr(spec, "response_commit_hook", None),
            session_id=getattr(spec, "session_id", None),
            restore=getattr(spec, "restore", False),
            vision_resolver=getattr(spec, "vision_resolver", None),
            vision_observation_check=getattr(spec, "vision_observation_check", None),
        )

    return factory


def _offline_responder(prompt: str) -> str:
    """A canned participant. Used only by --replay, never in live mode."""
    body = prompt.split("--- new messages ---")[-1].lower()
    if "timezone" in body or "cron" in body:
        return "the cron runs in UTC"
    if "deploy" in body:
        return "it went out five minutes ago"
    return "I'd need to check that one"


def profile_files(config: AppConfig) -> list[tuple[str, str]]:
    """The identity files this config reads, as (label, path)."""
    if config.system_prompt_path and not config.introduction_path:
        return [("system prompt", config.system_prompt_path)]
    return [
        ("soul", config.soul_path),
        ("introduction", config.introduction_path),
    ]


def describe_profile(config: AppConfig) -> str:
    return " + ".join(path for _label, path in profile_files(config))


def print_check(config: AppConfig) -> int:
    print(json.dumps({"config": config.describe()}, indent=2, ensure_ascii=False))
    problems = []
    # A key nobody consumes is a file that lies about its own configuration.
    # Report it rather than let it silently do nothing.
    for key in config.unknown_keys:
        problems.append(
            f"unknown config key {key!r}; it has no effect "
            f"(see config.example.toml for the accepted names)"
        )
    if not config.telegram_token:
        problems.append("TELEGRAM_BOT_TOKEN is missing")
    if not config.allowed_chat_ids:
        problems.append("TELEGRAM_ALLOWED_CHAT_IDS is empty; the bot would refuse every chat")
    if not config.model:
        problems.append("NERVIPULSA_MODEL is empty")
    # Report the identity files individually: "which one is missing" is the
    # whole question when the bot refuses to start.
    for label, path in profile_files(config):
        if not path:
            problems.append(f"{label} is not configured")
            continue
        if not Path(path).exists():
            problems.append(f"{label} not found: {path}")
    try:
        text = load_profile(config)
    except (OSError, ValueError) as exc:
        problems.append(str(exc))
    else:
        print(f"agent profile: {len(text)} chars from {describe_profile(config)}")
    if problems:
        print("\nNOT READY:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nconfiguration looks usable")
    return 0


async def replay(config: AppConfig, system_prompt: str) -> int:
    """Milestone A as a CLI: no token, no key, no network."""
    from . import fixtures

    store = Store(":memory:")
    transport = _RecordingTransport()
    agent = ScriptedAgent(_offline_responder)
    clock = [1_700_000_000.0]
    # --replay deliberately demonstrates the *legacy* rule path: it exists to
    # reproduce old fixtures offline, not to model the production agent. The
    # autonomous loop needs a tool-calling responder, which a canned offline
    # responder is not. Say so rather than let the output imply the bot still
    # works this way.
    bot = GroupAgent(
        legacy_policy=True,
        config=config,
        store=store,
        client=TelegramClient(transport),
        system_prompt=system_prompt,
        agent_factory=lambda spec: agent,
        turn_manager=TurnManager(debounce_seconds=config.debounce_seconds,
                                 clock=lambda: clock[0]),
        bot_user_id=fixtures.ALMA,
        clock=lambda: clock[0],
    )
    await bot.start()
    for label, updates in (
        ("ordinary chat", fixtures.plain_chat()),
        ("mention", [fixtures.mention()]),
        ("burst", fixtures.three_in_a_row()),
        ("unauthorized", fixtures.unauthorized()),
    ):
        await bot.step(updates)
        clock[0] += config.debounce_seconds + 1
        decisions = await bot.run_turns()
        print(f"{label:16} → {[(d.outcome, d.reason) for d in decisions]}")
    print("\nsends:")
    for payload in transport.sends:
        print(f"  {payload['text']}")
    print(f"\nstore: {json.dumps(store.stats(), ensure_ascii=False)}")
    print("\nnote: --replay exercises the legacy rule path for offline fixtures;")
    print("      live mode lets the agent decide through its action tools.")
    await bot.stop()
    store.close()
    return 0


class _RecordingTransport:
    def __init__(self) -> None:
        self.sends: list[dict] = []
        self._next = 1000

    def call(self, method: str, payload: dict) -> dict:
        if method == "sendMessage":
            self._next += 1
            self.sends.append(payload)
            return {"ok": True, "result": {"message_id": self._next}}
        return {"ok": True, "result": []}


def log_poll_ingress(tick, *, paused: bool) -> None:
    if tick.updates or tick.messages_stored:
        logging.getLogger(__name__).info(
            "poll ingress: updates=%d stored=%d duplicates=%d unauthorized=%d "
            "ignored=%d feedback=%d%s; sends run in background",
            tick.updates, tick.messages_stored, tick.duplicates, tick.unauthorized,
            tick.ignored, tick.feedback_updates, " (paused)" if paused else "",
        )


async def run_live(config: AppConfig, system_prompt: str) -> int:
    if not config.telegram_token:
        print("TELEGRAM_BOT_TOKEN is required for live mode", file=sys.stderr)
        return 2
    if not config.allowed_chat_ids:
        print("TELEGRAM_ALLOWED_CHAT_IDS is required; refusing to run open", file=sys.stderr)
        return 2

    store = Store(config.db_path)
    bot_api = Bot(config.telegram_token)
    transport = PythonTelegramBotTransport(bot_api)
    client = TelegramClient(transport, dry_run=config.dry_run)

    me: dict[str, Any] = {}
    try:
        await client.start()
        me = await client.get_me()
    except TransportError as exc:
        print(f"cannot reach Telegram (stage=telegram_start error_type={type(exc).__name__} "
              f"error_kind={exc.kind} error_category={exc.category})", file=sys.stderr)
        await client.close()
        store.close()
        return 2
    bot_user_id = me.get("id")
    if isinstance(bot_user_id, int):
        config.bot_user_id = bot_user_id  # type: ignore[attr-defined]
    if me.get("username"):
        config.bot_username = str(me["username"])

    # Privacy mode decides what this bot can perceive. It defaults to ON for
    # new bots, which means Telegram delivers only mentions and replies - every
    # other group message never reaches the process. Autonomous participation
    # depends on seeing ordinary conversation, so say so loudly instead of
    # silently behaving like a mention-only bot.
    if me.get("can_read_all_group_messages") is False:
        print(
            "WARNING: privacy mode is ON - Telegram will only deliver mentions and\n"
            "         replies. Ordinary group messages will never arrive, so the\n"
            "         agent cannot observe the conversation it is supposed to join.\n"
            "         Fix: BotFather -> /setprivacy -> Disable, then remove and\n"
            "         re-add the bot to the group for it to take effect.",
            file=sys.stderr,
        )

    if config.dry_run:
        print("DRY RUN: replies are logged and recorded, never sent to Telegram")

    bot = GroupAgent(
        config=config,
        store=store,
        client=client,
        system_prompt=system_prompt,
        agent_factory=build_agent_factory(config, system_prompt),
        turn_manager=TurnManager(
            debounce_seconds=config.debounce_seconds,
            max_messages=config.max_turn_messages,
        ),
        bot_user_id=bot_user_id if isinstance(bot_user_id, int) else None,
    )
    await bot.start()

    stop = asyncio.Event()

    def request_stop(*_args) -> None:
        stop.set()

    try:
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
    except (ValueError, AttributeError):
        pass  # not on the main thread (tests) — signals are optional

    print(f"{config.bot_name} is listening on chat(s) {list(config.allowed_chat_ids)}; "
          "mode=autonomous")
    logging.getLogger(__name__).info(
        "limits: activations_per_hour=%s actions_per_hour=%s min_action_interval_seconds=%s "
        "idle_seconds=%s max_batch_seconds=%s max_batch_observations=%s",
        config.max_activations_per_hour, config.max_actions_per_hour,
        config.min_action_interval_seconds, config.idle_seconds,
        config.max_batch_seconds, config.max_batch_observations,
    )

    # Pausing must not stop polling. `getUpdates` only returns updates newer than
    # the offset, and Telegram drops unacknowledged batches after ~24h; skipping
    # the poll while paused would lose messages, not defer them. So a paused bot
    # keeps ingesting durably and simply decides "silent" for every turn.
    offset = store.get_update_cursor()
    pause_logged = False
    log = logging.getLogger(__name__)
    consecutive_failures = 0
    async def wake_due_turns() -> None:
        while not stop.is_set():
            bot.schedule_turns()
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

    timer = asyncio.create_task(wake_due_turns())
    while not stop.is_set():
        if bot.stats.paused and not pause_logged:
            print("paused: still recording messages, will not reply")
            pause_logged = True
        elif not bot.stats.paused:
            pause_logged = False
        try:
            poll_task = asyncio.create_task(bot.poll_once(offset=offset, background=True))
            stop_task = asyncio.create_task(stop.wait())
            done, _ = await asyncio.wait((poll_task, stop_task), return_when=asyncio.FIRST_COMPLETED)
            if stop_task in done and not poll_task.done():
                poll_task.cancel()
                await asyncio.gather(poll_task, return_exceptions=True)
                break
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
            offset, tick = await poll_task
            consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A polling loop that exits on one bad cycle is a bot that dies
            # quietly. Back off and keep going; the durable inbox means nothing
            # is lost by the delay.
            consecutive_failures += 1
            log.error("poll cycle failed (%d in a row; stage=poll error_type=%s)",
                      consecutive_failures, type(exc).__name__)
            delay = min(30.0, 2.0 ** min(consecutive_failures, 4))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            continue
        log_poll_ingress(tick, paused=bot.stats.paused)

    timer.cancel()
    await asyncio.gather(timer, return_exceptions=True)
    # Close any open debounce window so a message that arrived in the last few
    # seconds is still decided rather than silently dropped on exit.
    pending = bot.hold_open_turns()
    print(f"\nshutting down; {pending} open turn(s) to process")
    if pending:
        try:
            await asyncio.wait_for(bot.run_turns(), timeout=10)
        except Exception as exc:  # never let shutdown raising hide the exit
            log.warning("final turn pass failed (error_type=%s)", type(exc).__name__)
    await bot.stop()
    # Close PTB's HTTP session explicitly: an unclosed client keeps its
    # connection pool (and the process) alive after the loop ends.
    await client.close()
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nervipulsa-telegram-bot")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.toml (or a legacy .json)")
    parser.add_argument("--check", action="store_true", help="validate configuration and exit")
    parser.add_argument("--replay", action="store_true", help="offline replay, no network")
    parser.add_argument("--dry-run", action="store_true", help="never call the send API")
    parser.add_argument("--proactive", action="store_true", help="enable proactive participation")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.dry_run:
        config.dry_run = True
    if args.proactive:
        config.proactive_enabled = True
    setup_logging(config.log_level)

    if args.check:
        return print_check(config)

    try:
        system_prompt = load_profile(config)
    except (OSError, ValueError) as exc:
        print(f"cannot load the agent profile: {exc}", file=sys.stderr)
        return 2

    if args.replay:
        return asyncio.run(replay(config, system_prompt))

    return asyncio.run(run_live(config, system_prompt))


if __name__ == "__main__":
    sys.exit(main())

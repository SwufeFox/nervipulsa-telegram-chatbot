"""Live console: inspect one conversation, or the whole bot, while it runs.

Why in-process rather than a separate query tool: the interesting state is
split between SQLite (durable queue, cursors, actions) and memory (in-flight
turns, agent failures, whether it is paused). A second process could read the
database but not the part that matters when something looks stuck.

Design constraints that shaped this:

* It must never become a second scheduler. Every command is read-only except
  `pause`, `resume` and `quit`, and those only flip a flag or set the stop
  event. Nothing here sends to Telegram, so a typo cannot post to a group.
* stdin is read in a worker thread. A blocking `readline()` on the event loop
  would freeze polling, ingestion and the model calls it is meant to observe.
* The live feed is a `logging.Handler`, not a parallel event path. Nothing in
  the bot has to know the console exists, so no code path can behave
  differently just because somebody is watching.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from typing import Any, Iterable, Sequence

from . import logwatch

# The exact shape `logwatch.scope()` renders. Parsing our own format is safe
# because these strings are built in one place; relying on it for user input
# would not be.
_SCOPE = re.compile(r"chat:(-?\d+)\(([a-z]+)\)(?:/topic:(\d+))?")

_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"


def parse_scope(text: str) -> tuple[int, int | None, str] | None:
    """Pull a scope out of a formatted log line."""
    match = _SCOPE.search(text)
    if match is None:
        return None
    chat_id = int(match.group(1))
    kind = match.group(2)
    topic = match.group(3)
    return chat_id, (int(topic) if topic is not None else None), kind


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _clip(text: Any, limit: int = 60) -> str:
    flat = " ".join(logwatch.redact(str(text or "")).split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


class LiveFeed(logging.Handler):
    """Mirrors log records to the console, filtered to one scope on demand."""

    def __init__(self, console: "Console") -> None:
        super().__init__(level=logging.INFO)
        self._console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            console = self._console
            message = record.getMessage()
            scope = parse_scope(message)
            if console.watching is not None:
                if scope is None:
                    return
                chat_id, topic_id, _kind = scope
                if (chat_id, topic_id) != console.watching:
                    return
            elif console.quiet_unfocused and scope is not None:
                # Quiet mode keeps the status blocks and startup lines and
                # drops per-conversation chatter.
                return
            self._console.write(
                f"{_DIM}{time.strftime('%H:%M:%S')}{_RESET} {message}",
            )
        except Exception:  # a broken feed must not kill logging
            self.handleError(record)


class Console:
    """A tiny REPL bound to the running bot.

    `bot` needs `store`, `chat_kinds`, `stats`, `config`, and optionally
    `pause`/`resume`/`diagnostics`; anything missing degrades to a message
    instead of an exception.
    """

    HELP = [
        ("status", "whole-bot snapshot: backlog, in-flight turns, budget usage"),
        ("chats", "every known conversation, with pending and delivered counts"),
        ("watch <chat> [topic]", "show only that conversation's live events"),
        ("unwatch", "stop filtering (show everything again)"),
        ("quiet on|off", "hide per-conversation events, keep status lines"),
        ("tail [n]", "recent stored messages for the watched conversation"),
        ("queue [n]", "pending observations, oldest first, with age"),
        ("actions [n]", "recent send intents and how they ended"),
        ("turns", "workflow turns still running"),
        ("waits", "agent waits that are armed or expired"),
        ("stats", "counters since start"),
        ("pause / resume", "stop or resume replying (ingestion continues)"),
        ("quit", "shut down cleanly"),
    ]

    def __init__(self, *, bot: Any, config: Any, stream: Any = None,
                 level: int = logging.INFO) -> None:
        self.bot = bot
        self.config = config
        self.stream = stream or sys.stdout
        self.watching: tuple[int, int | None] | None = None
        # Off by default: the console mirrors the log until asked to be quiet.
        # A monitor that silently drops lines is worse than a noisy one.
        self.quiet_unfocused = False
        self._feed = LiveFeed(self)
        self._feed.setLevel(level)
        self._muted: list[tuple[logging.Handler, int]] = []
        self._closed = False
        self._stop: asyncio.Event | None = None
        self._colour = self._supports_colour()

    # -- output ------------------------------------------------------------

    def _supports_colour(self) -> bool:
        try:
            return bool(self.stream.isatty())
        except Exception:
            return False

    def write(self, text: str = "") -> None:
        if self._closed:
            return
        if not self._colour:
            text = text.replace(_DIM, "").replace(_BOLD, "").replace(_RESET, "")
        try:
            self.stream.write(text + "\n")
            self.stream.flush()
        except Exception:
            self._closed = True

    def attach(self) -> None:
        """Start mirroring the log, without printing every line twice.

        `logging.basicConfig` already sends INFO to stdout. The console feed
        prints the same records in a more readable shape, so the base handler
        is raised to WARNING while attached: warnings and errors still reach
        it - and a traceback stays intact - while the routine chatter comes
        from the feed only.
        """
        root = logging.getLogger()
        root.addHandler(self._feed)
        for handler in root.handlers:
            if handler is self._feed:
                continue
            self._muted.append((handler, handler.level))
            if handler.level < logging.WARNING or handler.level == logging.NOTSET:
                handler.setLevel(logging.WARNING)

    def detach(self) -> None:
        logging.getLogger().removeHandler(self._feed)
        for handler, level in self._muted:
            handler.setLevel(level)
        self._muted.clear()
        self._closed = True

    # -- lifecycle ---------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Read commands until EOF or `quit`.

        Each read happens in a thread so a quiet console costs the event loop
        nothing: polling, ingestion and model calls keep running.
        """
        self._stop = stop
        self.attach()
        self.write(f"{_BOLD}console ready{_RESET} — 'help' for commands, "
                   "'watch <chat_id>' to follow one conversation")
        while not stop.is_set():
            try:
                line = await asyncio.to_thread(sys.stdin.readline)
            except (OSError, ValueError):
                break
            if line == "":  # EOF: no interactive terminal
                break
            line = line.strip()
            if not line:
                continue
            try:
                self.handle(line)
            except Exception as exc:  # never let a typo kill the bot
                self.write(f"error: {type(exc).__name__}: {exc}")
            if stop.is_set():
                break
        self.detach()

    def handle(self, line: str) -> None:
        """Dispatch one command. Kept synchronous so tests can call it."""
        parts = line.split()
        command = parts[0].lstrip("/").lower()
        args = parts[1:]
        handler = getattr(self, f"cmd_{command}", None)
        if handler is None:
            self.write(f"unknown command {command!r}; try 'help'")
            return
        output = handler(args)
        if output is None:
            return
        for row in output:
            self.write(row)

    # -- helpers -----------------------------------------------------------

    @property
    def store(self) -> Any:
        return getattr(self.bot, "store", None)

    def _kinds(self) -> dict[int, str]:
        return getattr(self.bot, "chat_kinds", {}) or {}

    def _label(self, chat_id: int, topic_id: int | None) -> str:
        return logwatch.scope(chat_id, topic_id, self._kinds().get(chat_id))

    def _limits(self) -> dict[str, Any]:
        return {
            "max_activations_per_hour": getattr(self.config, "max_activations_per_hour", "?"),
            "max_actions_per_hour": getattr(self.config, "max_actions_per_hour", "?"),
        }

    @staticmethod
    def _int(value: str, *, default: int | None = None) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _limit(self, args: Sequence[str], *, default: int, ceiling: int = 200) -> int:
        """Optional numeric argument, clamped. Missing or nonsense keeps `default`."""
        value = self._int(args[0], default=default) if args else default
        return max(1, min(ceiling, int(value or default)))

    # -- commands ----------------------------------------------------------

    def cmd_help(self, args: Sequence[str]) -> Iterable[str]:
        width = max(len(name) for name, _ in self.HELP)
        return [f"  {name.ljust(width)}  {description}"
                for name, description in self.HELP]

    def cmd_status(self, args: Sequence[str]) -> Iterable[str]:
        if self.store is None:
            return ["no store attached"]
        snapshot = logwatch.status_snapshot(self.store)
        lines = logwatch.status_lines(snapshot, kinds=self._kinds(),
                                      limits=self._limits())
        extra = []
        paused = getattr(getattr(self.bot, "stats", None), "paused", None)
        if paused is not None:
            extra.append(f"  paused={paused} watching={self._watching_label()}")
        # What the agent is currently waiting for is part of "where do we
        # stand": a silent bot with an armed timer is behaving correctly, and
        # that looks identical to one that is stuck unless it is shown.
        armed = self.store._query(
            "SELECT chat_id, topic_id, kind, purpose, due_at, expires_at"
            " FROM agent_waits WHERE status = 'active' ORDER BY created_at")
        now = time.time()
        for row in armed:
            deadline = row["due_at"] or row["expires_at"]
            when = (f"due in {_age(float(deadline) - now)}" if deadline
                    else "no deadline")
            extra.append(f"  waiting {self._label(int(row['chat_id']), row['topic_id'])}"
                         f" {row['kind']} {when}: {_clip(row['purpose'], 40)}")
        return list(lines) + extra

    def cmd_chats(self, args: Sequence[str]) -> Iterable[str]:
        if self.store is None:
            return ["no store attached"]
        snapshot = logwatch.status_snapshot(self.store)
        if not snapshot["scopes"]:
            return ["no conversations seen yet"]
        return [line for line in
                logwatch.status_lines(snapshot, kinds=self._kinds(),
                                      limits=self._limits())[1:]]

    def _watching_label(self) -> str:
        if self.watching is None:
            return "all"
        return self._label(*self.watching)

    def cmd_watch(self, args: Sequence[str]) -> Iterable[str]:
        if not args:
            return [f"watching: {self._watching_label()}",
                    "usage: watch <chat_id> [topic_id]"]
        chat_id = self._int(args[0])
        if chat_id is None:
            return [f"not a chat id: {args[0]!r}"]
        topic_id = self._int(args[1]) if len(args) > 1 else None
        self.watching = (chat_id, topic_id)
        return [f"watching {self._label(chat_id, topic_id)} — "
                "'unwatch' to see everything again"]

    def cmd_unwatch(self, args: Sequence[str]) -> Iterable[str]:
        self.watching = None
        return ["watching: all"]

    def cmd_quiet(self, args: Sequence[str]) -> Iterable[str]:
        choice = (args[0].lower() if args else "on")
        if choice in {"off", "0", "false", "no"}:
            self.quiet_unfocused = False
        elif choice in {"on", "1", "true", "yes"}:
            self.quiet_unfocused = True
        else:
            return [f"usage: quiet on|off (currently {'on' if self.quiet_unfocused else 'off'})"]
        return [f"quiet: {'on' if self.quiet_unfocused else 'off'}"]

    def cmd_tail(self, args: Sequence[str]) -> Iterable[str]:
        if self.store is None:
            return ["no store attached"]
        limit = self._int(args[0], default=15) if args else 15
        limit = max(1, min(200, int(limit or 15)))
        if self.watching is None:
            rows = self.store._query(
                "SELECT * FROM messages ORDER BY created_at DESC LIMIT ?", (limit,))
        else:
            chat_id, topic_id = self.watching
            rows = self.store._query(
                "SELECT * FROM messages WHERE chat_id = ? AND topic_id IS ?"
                " ORDER BY created_at DESC LIMIT ?", (chat_id, topic_id, limit))
        if not rows:
            return ["no stored messages"]
        now = time.time()
        out = []
        for row in reversed(rows):
            who = row["sender_label"]
            if row["is_bot"]:
                who = f"{who}*"
            out.append(f"  {_age(now - float(row['created_at'])):>5}  "
                       f"{self._label(int(row['chat_id']), row['topic_id'])}  "
                       f"{who}: {_clip(row['text'])}")
        return out

    def cmd_queue(self, args: Sequence[str]) -> Iterable[str]:
        if self.store is None:
            return ["no store attached"]
        limit = self._limit(args, default=20)
        import json

        rows = self.store._query(
            "SELECT o.* FROM observation_events o"
            " LEFT JOIN observation_cursors c"
            "   ON c.chat_id = o.chat_id AND c.topic_id IS o.topic_id"
            " WHERE o.sequence > COALESCE(c.last_processed, 0)"
            " ORDER BY o.sequence LIMIT ?", (limit,))
        if self.watching is not None:
            rows = [r for r in rows
                    if (int(r["chat_id"]), r["topic_id"]) == self.watching]
        if not rows:
            return ["queue empty"]
        now = time.time()
        out = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            moment = payload.get("created_at") or payload.get("date") or row["received_at"]
            kind = row["kind"]
            text = payload.get("text") or (payload.get("media") or {}).get("hint") or ""
            out.append(f"  seq={row['sequence']:<5} {_age(now - float(moment)):>5}  "
                       f"{self._label(int(row['chat_id']), row['topic_id'])}  "
                       f"{kind} {payload.get('sender_label') or '?'}: {_clip(text)}")
        return out

    def cmd_actions(self, args: Sequence[str]) -> Iterable[str]:
        if self.store is None:
            return ["no store attached"]
        limit = self._limit(args, default=15)
        rows = self.store._query(
            "SELECT * FROM actions ORDER BY created_at DESC LIMIT ?", (limit,))
        if self.watching is not None:
            rows = [r for r in rows if int(r["chat_id"]) == self.watching[0]]
        if not rows:
            return ["no send intents yet"]
        now = time.time()
        out = []
        for row in reversed(rows):
            error = row["error"] or ""
            out.append(f"  {_age(now - float(row['created_at'])):>5}  "
                       f"{str(row['status']):<9} "
                       f"{self._label(int(row['chat_id']), row['topic_id'])}  "
                       f"{_clip(row['text'], 40)}"
                       + (f"  [{_clip(error, 50)}]" if error else ""))
        return out

    def cmd_turns(self, args: Sequence[str]) -> Iterable[str]:
        if self.store is None:
            return ["no store attached"]
        rows = self.store.running_workflows()
        if not rows:
            return ["no turn in flight"]
        now = time.time()
        return [f"  {row['workflow_id']} "
                f"{self._label(int(row['chat_id']), row['topic_id'])} "
                f"seq={row['first_sequence']}-{row['last_sequence']} "
                f"age={_age(now - float(row['created_at']))}"
                for row in rows]

    def cmd_waits(self, args: Sequence[str]) -> Iterable[str]:
        if self.store is None:
            return ["no store attached"]
        rows = self.store._query(
            "SELECT * FROM agent_waits WHERE status = 'active' ORDER BY created_at")
        if not rows:
            return ["no active waits"]
        now = time.time()
        out = []
        for row in rows:
            due = row["due_at"] or row["expires_at"]
            when = f"due in {_age(float(due) - now)}" if due else "no deadline"
            out.append(f"  {self._label(int(row['chat_id']), row['topic_id'])}  "
                       f"{row['kind']}  {when}  {_clip(row['purpose'], 40)}")
        return out

    def cmd_stats(self, args: Sequence[str]) -> Iterable[str]:
        stats = getattr(self.bot, "stats", None)
        if stats is None or not hasattr(stats, "as_dict"):
            return ["no stats available"]
        data = stats.as_dict()
        total = data.pop("total", {})
        out = [f"  {key}: {value}" for key, value in data.items()]
        if total:
            non_zero = {k: v for k, v in total.items() if v}
            out.append(f"  totals: {non_zero}")
        return out

    def cmd_pause(self, args: Sequence[str]) -> Iterable[str]:
        if not hasattr(self.bot, "pause"):
            return ["this bot cannot be paused"]
        self.bot.pause()
        return ["paused: still ingesting, will not reply"]

    def cmd_resume(self, args: Sequence[str]) -> Iterable[str]:
        if not hasattr(self.bot, "resume"):
            return ["this bot cannot be resumed"]
        self.bot.resume()
        return ["resumed"]

    def cmd_quit(self, args: Sequence[str]) -> Iterable[str]:
        if self._stop is not None:
            self._stop.set()
        return ["shutting down"]

"""Completion must describe what the dispatcher actually accepts.

The console used to take a bare `input()` line: no command list, no argument
hints, and the prompt was overwritten by log output. These tests pin the parts
that are testable without a terminal - the completion source and the dispatch
contract - so a command cannot exist in one and be missing from the other.
"""
from __future__ import annotations

import asyncio
import io
import logging
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import fixtures
from app.console import Console, _completer, _run_interactive


class Store:
    def __init__(self):
        self.rows = []

    def _query(self, sql, params=()):
        return []

    def running_workflows(self):
        return []


class Bot:
    store = Store()
    chat_kinds = {fixtures.TEST_CHAT: "group", 12345: "dm"}
    stats = type("S", (), {"paused": False, "as_dict": lambda self: {}})()

    def pause(self):
        pass

    def resume(self):
        pass


def make() -> Console:
    config = type("C", (), {"max_activations_per_hour": 60,
                            "max_actions_per_hour": 20})()
    return Console(bot=Bot(), config=config, stream=io.StringIO())


def test_every_help_entry_is_dispatchable():
    console = make()
    names = set(console.command_names())
    for entry, _description in Console.HELP:
        command = entry.split()[0]
        assert command in names, f"help lists {command!r} but there is no cmd_{command}"


def test_every_command_is_documented():
    console = make()
    documented = {entry.split()[0] for entry, _ in Console.HELP}
    for name in console.command_names():
        assert name in documented, f"cmd_{name} exists but help does not mention it"


def completions(console: Console, text: str) -> list[str]:
    """Run the real completer, so this tests what the terminal will offer."""
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    completer = _completer(console)
    document = Document(text=text, cursor_position=len(text))
    return [c.text for c in completer.get_completions(document, CompleteEvent())]


def test_prefix_completes_commands():
    console = make()
    assert "status" in completions(console, "sta")
    assert "pause" in completions(console, "pa")
    assert "resume" in completions(console, "res")


def test_empty_prefix_offers_every_command():
    console = make()
    offered = set(completions(console, ""))
    assert offered == set(console.command_names())


def test_quiet_completes_its_arguments():
    console = make()
    assert completions(console, "quiet ") == ["on", "off"]


def test_watch_completes_known_chat_ids():
    console = make()
    offered = completions(console, "watch ")
    assert str(fixtures.TEST_CHAT) in offered
    assert "12345" in offered


def test_unknown_command_is_reported_not_raised():
    console = make()
    console.dispatch("totally-not-a-command")
    assert "unknown command 'totally-not-a-command'" in console.stream.getvalue()



def test_interactive_log_output_preserves_prompt_and_stop_unblocks_input():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    class RecordingOutput(DummyOutput):
        def __init__(self):
            self.writes = []

        def write_raw(self, data):
            self.writes.append(data)

    async def scenario():
        console = make()
        stop = asyncio.Event()
        output = RecordingOutput()
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=output):
                prompt = asyncio.create_task(_run_interactive(console, stop))
                await asyncio.sleep(0.05)
                pipe.send_text("quiet ")
                await asyncio.sleep(0.05)

                # LiveFeed may run on a worker thread while the user has typed
                # an incomplete command. The patched stdout proxy must route it
                # through prompt_toolkit without touching the input buffer.
                logger = logging.getLogger("console-test")
                logger.addHandler(console._feed)
                logger.setLevel(logging.INFO)
                thread = threading.Thread(
                    target=lambda: logger.info("log while typing"), daemon=True)
                thread.start()
                await asyncio.to_thread(thread.join, 1)
                logger.removeHandler(console._feed)

                pipe.send_text("on\r")
                await asyncio.sleep(0.05)
                assert console.quiet_unfocused
                assert "quiet: on" in console.stream.getvalue()
                assert any("log while typing" in text for text in output.writes)

                # Shutdown must cancel a prompt currently waiting for input.
                stop.set()
                await asyncio.wait_for(prompt, timeout=1)
                assert console._live_writer is None

    asyncio.run(scenario())


def test_console_run_restores_logging_handlers_when_input_fails(monkeypatch):
    class BrokenInput:
        def isatty(self):
            return False

        def readline(self):
            raise OSError("stdin closed")

    console = make()
    root = logging.getLogger()
    before = {handler: handler.level for handler in root.handlers}
    monkeypatch.setattr(sys, "stdin", BrokenInput())

    asyncio.run(console.run(asyncio.Event()))

    assert console._feed not in root.handlers
    assert {handler: handler.level for handler in root.handlers} == before

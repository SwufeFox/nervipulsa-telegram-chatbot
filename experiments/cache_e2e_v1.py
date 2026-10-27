"""Privacy-conscious end-to-end cache probe; results contain no request text or headers."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import experiments.long_conversation as L
import nervipulsa.providers as providers


def counters(raw: bytes) -> dict[str, Any]:
    try:
        response = json.loads(raw)
    except (ValueError, UnicodeError):
        return {"usage_present": False, "prompt_tokens": None, "cached_tokens": None}
    usage = response.get("usage") if isinstance(response, dict) else None
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
    def number(value: Any) -> int | None:
        return value if type(value) is int else None
    return {"usage_present": isinstance(usage, dict),
            "prompt_tokens": number(usage.get("prompt_tokens")) if isinstance(usage, dict) else None,
            "cached_tokens": number(details.get("cached_tokens")) if isinstance(details, dict) else None}


class Probe:
    def __init__(self, original):
        self.original = original
        self.rows = []
        self.turn = None
        self.calls = 0
        self.previous = None

    def post(self, request, timeout):
        self.calls += 1
        call_id = self.calls
        body = request.data
        parsed = json.loads(body)
        messages = parsed.get("messages")
        shared = None
        if isinstance(messages, list) and self.previous is not None:
            shared = 0
            for a, b in zip(self.previous, messages):
                if json.dumps(a, sort_keys=True, ensure_ascii=True) != json.dumps(b, sort_keys=True, ensure_ascii=True):
                    break
                shared += 1
        self.previous = messages if isinstance(messages, list) else None
        digest = hashlib.sha256(body).hexdigest()
        base = {"call_id": call_id, "turn": self.turn, "body_sha256": digest,
                "body_bytes": len(body), "message_count": len(messages) if isinstance(messages, list) else None,
                "shared_full_messages_with_previous_call": shared,
                "body_field_names": sorted(parsed.keys())}

        def send(repeat_index):
            row = {**base, "request_id": len(self.rows) + 1, "repeat_index": repeat_index,
                   "usage_present": False, "prompt_tokens": None, "cached_tokens": None,
                   "status": "started"}
            self.rows.append(row)
            started = time.monotonic()
            try:
                # Replay constructs a fresh Request using precisely the captured bytes and headers.
                outgoing = request if repeat_index == 0 else urllib.request.Request(
                    request.full_url, data=body, headers=dict(request.header_items()), method=request.get_method())
                row["body_sha256"] = hashlib.sha256(outgoing.data).hexdigest()
                row["body_bytes"] = len(outgoing.data)
                raw = self.original(outgoing, timeout)
                row.update(counters(raw))
                row["status"] = "ok"
                return raw
            except Exception as exc:
                row["status"] = "error"
                row["error_type"] = type(exc).__name__
                raise
            finally:
                row["elapsed_seconds"] = round(time.monotonic() - started, 3)

        raw = send(0)
        # The first call is a baseline; prior server cache state is unknown.
        if call_id > 1 and self.rows[-1]["cached_tokens"] == 0:
            for repeat_index in (1, 2):
                try:
                    send(repeat_index)
                except Exception:
                    pass
        return raw


async def run(probe, turns):
    with tempfile.TemporaryDirectory(prefix="cache-e2e-", dir=ROOT) as tmp:
        root = Path(tmp)
        store = L.Store(root / "bot.sqlite3")
        config = L._build_config(root, 24)
        prompt = L.load_profile(config)
        clock = [1_700_000_000.0]
        agent = L.NervipulsaAgent(settings=L._settings(config), workspace=root / "chat",
                                   system_prompt=prompt, timeout=config.request_timeout)
        bot = L.GroupAgent(config=config, store=store, client=L.TelegramClient(L.RecordingTransport()),
                           system_prompt=prompt, agent_factory=lambda spec: agent,
                           turn_manager=L.TurnManager(debounce_seconds=config.debounce_seconds, clock=lambda: clock[0]),
                           bot_user_id=L.fixtures.ALMA, clock=lambda: clock[0])
        try:
            await agent.start()
            await bot.start()
            for index, utterance in enumerate(L.SCRIPT[:turns]):
                probe.turn = index + 1
                await bot.step([L._update(index + 1, 1001 + index, utterance, None)])
                clock[0] += config.debounce_seconds + 0.1
                await bot.run_turns()
        finally:
            try:
                await bot.stop()
            finally:
                try:
                    await agent.stop()
                finally:
                    store.close()


def validate(result):
    rows = result["requests"]
    assert [r["request_id"] for r in rows] == list(range(1, len(rows) + 1))
    originals = [r for r in rows if r["repeat_index"] == 0]
    assert [r["call_id"] for r in originals] == list(range(1, len(originals) + 1))
    for row in originals:
        repeats = [r for r in rows if r["call_id"] == row["call_id"] and r["repeat_index"] > 0]
        expected = [1, 2] if row["call_id"] > 1 and row["cached_tokens"] == 0 else []
        assert [r["repeat_index"] for r in repeats] == expected
        assert all(r["body_sha256"] == row["body_sha256"] and r["body_bytes"] == row["body_bytes"] for r in repeats)
    assert all(len(r["body_sha256"]) == 64 and r["status"] in ("ok", "error") for r in rows)
    assert all(r["cached_tokens"] is None or type(r["cached_tokens"]) is int for r in rows)


def main():
    turns = int(sys.argv[1]) if len(sys.argv) > 1 else 49
    if not 1 <= turns <= len(L.SCRIPT):
        raise ValueError("turn count outside script")
    out = ROOT / "docs" / "evaluations" / ("cache-e2e-v1-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8] + ".json")
    probe = Probe(providers._http_post)
    result = {"schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
              "model": L.MODEL, "script_turns_requested": turns, "requests": probe.rows,
              "assumptions": ["The endpoint's cached_tokens field reports cached prompt tokens.",
                              "The first model call is a baseline; actual server cache state is unknown.",
                              "A matching full-message prefix is necessary evidence of client prefix stability, not proof of server cache behavior."],
              "run_status": "running"}
    providers._http_post = probe.post
    try:
        asyncio.run(run(probe, turns))
        result["run_status"] = "completed"
    except Exception as exc:
        result["run_status"] = "error"
        result["error_type"] = type(exc).__name__
    finally:
        providers._http_post = probe.original
        validate(result)
        result["structure_validated"] = True
        result["nonfirst_zero_calls"] = sum(r["repeat_index"] == 0 and r["call_id"] > 1 and r["cached_tokens"] == 0 for r in probe.rows)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(out), "run_status": result["run_status"], "requests": len(probe.rows),
                          "model_calls": probe.calls, "nonfirst_zero_calls": result["nonfirst_zero_calls"]}))
    return 0 if result["run_status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

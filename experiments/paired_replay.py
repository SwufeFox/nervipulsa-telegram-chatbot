"""Paired replay over recorded group observations.

Two runs, identical inputs, identical model and profile, isolated state, and a
recording transport that never contacts Telegram:

  plain         the pre-continuation behaviour - every wake is a fresh activation
                with no durable episode identity
  continuation  this tree - activations that belong to one logical episode

The only intended difference between the arms is whether the episode mechanism is
on, so any difference in what she says is attributable to it.

Usage:
  python -m experiments.paired_replay --limit 60

Writes a JSON report under docs/evaluations/. The recorded database is opened
read-only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GROUP_CHAT = -1004402809405
# This bot's Telegram id is *not* a constant to guess. The value hard-coded here
# used to be 8993827408, which is `linverseHelperBot` - a different bot in the
# same group. Because the app skips messages whose sender is `bot_user_id`, that
# number made the replay silently drop ten of that bot's messages as "our own".
# It is read from Telegram (getMe) or passed explicitly instead.
BOT_ID = 0
BATCH = 6


# ---------------------------------------------------------------------------
# recorded input
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecordedEvent:
    sequence: int
    kind: str
    received_at: float
    payload: dict[str, Any]

    @property
    def created_at(self) -> float:
        return float(self.payload.get("created_at") or self.received_at)

    def as_update(self) -> dict[str, Any] | None:
        """Rebuild the Telegram-shaped update the bot's ingest path expects.

        Only chat messages can be replayed this way; action results and edits
        have no inbound update behind them, so they are skipped and counted in
        the report rather than silently dropped.
        """
        if self.kind not in {"message", "bot_message"}:
            return None
        p = self.payload
        label = str(p.get("sender_label") or "unknown")
        message: dict[str, Any] = {
            "message_id": int(p["message_id"]),
            "from": {"id": int(p.get("sender_id") or 0),
                     "is_bot": bool(p.get("is_bot")),
                     "username": label, "first_name": label},
            "chat": {"id": GROUP_CHAT, "type": "supergroup", "title": "Recorded group"},
            "date": int(self.created_at),
        }
        text = str(p.get("text") or "")
        if text:
            message["text"] = text
        media = p.get("media") or {}
        # Media is replayed by reference only. Fetching the bytes would contact
        # Telegram, which this replay must never do.
        if media.get("kind") == "photo":
            message["photo"] = [{"file_id": str(media.get("file_id") or ""),
                                 "file_unique_id": "replay", "width": 1, "height": 1}]
            message["caption"] = text
        elif media.get("kind") == "sticker":
            message["sticker"] = {"file_id": str(media.get("file_id") or ""),
                                  "file_unique_id": "replay", "emoji": "🙂"}
        if p.get("reply_to_bot"):
            # The recorded payload keeps a boolean only; the exact parent id was
            # not retained, so a stand-in is used and this is disclosed.
            message["reply_to_message"] = {"message_id": max(1, int(p["message_id"]) - 1),
                                           "from": {"id": BOT_ID, "is_bot": True,
                                                    "username": "linGotFoxbot"}}
        return {"update_id": int(p["message_id"]), "message": message}


def load_recorded(db_path: Path, *, limit: int, chat_id: int = GROUP_CHAT
                  ) -> list[RecordedEvent]:
    import sqlite3

    if not db_path.exists():
        raise SystemExit(f"recorded database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT sequence, kind, received_at, payload_json FROM observation_events"
            " WHERE chat_id = ? AND kind IN ('message', 'bot_message')"
            " ORDER BY sequence DESC LIMIT ?", (chat_id, limit)).fetchall()
    finally:
        conn.close()
    return [RecordedEvent(int(r["sequence"]), str(r["kind"]), float(r["received_at"]),
                          json.loads(r["payload_json"]))
            for r in reversed(rows)]


# ---------------------------------------------------------------------------
# recording transport: the real client, no network
# ---------------------------------------------------------------------------

class RecordingTransport:
    """Satisfies the client's transport contract and records sends.

    Only `sendMessage` and `getMe` are meaningful; everything else raises, so an
    unexpected network path fails loudly instead of pretending to work.
    """

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []
        self._next_id = 900000

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if method == "sendMessage":
            self.sends.append(dict(payload))
            self._next_id += 1
            return {"ok": True, "result": {"message_id": self._next_id}}
        if method == "getMe":
            # No id: the recording contains nothing authored by this bot, so
            # there is nothing to filter. Guessing an id would be worse than
            # having none - 8993827408 is `linverseHelperBot`, and treating it
            # as ours silently dropped ten of that bot's messages.
            return {"ok": True, "result": {"id": BOT_ID, "is_bot": True,
                                           "username": "linGotFoxbot"}}
        if method in {"deleteMessage", "getUpdates"}:
            return {"ok": True, "result": [] if method == "getUpdates" else True}
        raise RuntimeError(f"replay transport refuses {method}: no network in replay")


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

@dataclass
class RunMetrics:
    name: str
    activations: int = 0
    activations_with_episode: int = 0
    continuations: int = 0
    episodes_opened: int = 0
    sends: int = 0
    silent: int = 0
    undecided: int = 0
    waits: int = 0
    goals_set: int = 0
    prompt_chars: int = 0
    usage_in: int = 0
    usage_out: int = 0
    usage_cached: int = 0
    usage_missing: int = 0
    # Activations whose usage block has no cache field at all: unknown, not 0.
    usage_cache_unknown: int = 0
    elapsed_ms: float = 0.0
    interruptions: int = 0
    replies: list[str] = field(default_factory=list)
    # Reply index -> how many replayed inbound messages had arrived before it.
    reply_positions: list[int] = field(default_factory=list)
    inbound_seen: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "activations": self.activations,
            "activations_with_episode": self.activations_with_episode,
            "continuations": self.continuations,
            "episodes_opened": self.episodes_opened,
            "sends": self.sends,
            "silent": self.silent,
            "undecided": self.undecided,
            "waits": self.waits,
            "goals_set": self.goals_set,
            "prompt_chars": self.prompt_chars,
            "usage_in": self.usage_in,
            "usage_out": self.usage_out,
            "usage_cached": self.usage_cached,
            "usage_activations_without_usage": self.usage_missing,
            "usage_activations_without_cache_field": self.usage_cache_unknown,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "interruptions": self.interruptions,
            "reply_positions": self.reply_positions,
            "inbound_seen": self.inbound_seen,
            "replies": self.replies,
        }


@dataclass
class RunOutcome:
    metrics: RunMetrics
    final_state: dict[str, Any]


# ---------------------------------------------------------------------------
# one replay arm
# ---------------------------------------------------------------------------

def _config_for(config_path: Path, workspace: Path, model: str | None) -> Any:
    from app.config import load_config

    base = load_config(config_path)
    fields = dict(base.__dict__)
    fields.update({
        "workspace": str(workspace),
        "db_path": str(workspace / "app.db"),
        "allowed_chat_ids": (GROUP_CHAT,),
        # Nothing in the replay may reach the network except the model.
        "telegram_token": "",
        "dry_run": False,
        "idle_seconds": 0.0,
        "log_status_seconds": 0,
    })
    if model:
        fields["model"] = model
    return base.__class__(**fields)


def _build(config_path: Path, *, workspace: Path, model: str | None,
           with_episodes: bool) -> tuple[Any, Any, RecordingTransport, Any]:
    from app.agent import NervipulsaAgent
    from app.bot import GroupAgent
    from app.config import load_default_profile
    from app.store import Store
    from app.telegram import TelegramClient
    from app.turns import TurnManager

    workspace.mkdir(parents=True, exist_ok=True)
    config = _config_for(config_path, workspace, model)
    # The profile must be the deployed one (config.fox.toml points at the fox
    # soul/introduction), not the shipped English default. Testing a different
    # persona than production runs would make the comparison meaningless.
    from app.config import load_default_profile, load_profile

    prompt = load_profile(config) if (config.soul_path or config.introduction_path
                                     or config.system_prompt_path) else load_default_profile()
    store = Store(workspace / "app.db")
    transport = RecordingTransport()
    client = TelegramClient(transport)
    agents: dict[tuple[int, int | None], Any] = {}

    def agent_for(spec: Any) -> Any:
        """Build the agent exactly as the bot's own factory does.

        The runtime refuses observations without a tool catalog, so the spec's
        catalog, commit hook and session id are all forwarded rather than
        reconstructed - rebuilding them here would test a different agent than
        the one that runs in production.
        """
        key = (spec.chat_id, getattr(spec, "topic_id", None))
        if key not in agents:
            workspace_for_agent = Path(spec.workspace)
            workspace_for_agent.mkdir(parents=True, exist_ok=True)
            agents[key] = NervipulsaAgent(
                settings=_settings(config, workspace_for_agent),
                workspace=workspace_for_agent,
                system_prompt=prompt,
                timeout=config.request_timeout,
                tool_catalog=spec.tool_catalog,
                response_commit_hook=spec.response_commit_hook,
                session_id=spec.session_id,
                restore=spec.restore,
            )
        return agents[key]

    bot = GroupAgent(config=config, store=store, client=client,
                     system_prompt=prompt, agent_factory=agent_for,
                     turn_manager=TurnManager(debounce_seconds=0),
                     bot_user_id=BOT_ID)

    if not with_episodes:
        # The single intended difference: no durable episode identity. Each wake
        # is what it was before - an independent activation.
        def _no_episode(**_kwargs: Any) -> dict[str, Any]:
            return {"episode_id": None, "chat_id": 0, "topic_id": None,
                    "state": "open", "reason": None, "intent_id": None,
                    "opened_at": 0.0, "updated_at": 0.0, "closed_at": None}

        bot.autonomy.store.open_episode = _no_episode  # type: ignore[method-assign]
        bot.autonomy.store.current_episode = lambda *a, **k: None  # type: ignore[method-assign]
        bot.autonomy.store.close_episode = lambda *a, **k: False  # type: ignore[method-assign]
        bot.autonomy.store.attach_episode_intent = lambda *a, **k: None  # type: ignore[method-assign]
        bot.autonomy.store.episode_activations = lambda *a, **k: []  # type: ignore[method-assign]
        bot.autonomy.store.open_episodes = lambda *a, **k: []  # type: ignore[method-assign]
        bot.autonomy._settle_episode = lambda *a, **k: None  # type: ignore[method-assign]
    return bot, store, transport, config


def _settings(config: Any, workspace: Path) -> Any:
    from nervipulsa.config import Settings

    return Settings(
        model=config.model,
        base_url=config.base_url or "http://127.0.0.1:3425/v1",
        workspace=str(workspace),
        max_timeout=config.request_timeout,
        default_timeout=config.request_timeout,
        context_limit=config.context_limit,
        max_output_tokens=config.max_output_tokens,
    )


async def run_once(events: Iterable[RecordedEvent], *, name: str, workspace: Path,
                   config_path: Path, model: str | None,
                   with_episodes: bool) -> RunOutcome:
    bot, store, transport, _config = _build(
        config_path, workspace=workspace, model=model, with_episodes=with_episodes)
    metrics = RunMetrics(name=name)
    started = time.monotonic()
    await bot.start()
    try:
        batch: list[dict[str, Any]] = []
        for event in events:
            update = event.as_update()
            if update is not None:
                batch.append(update)
                metrics.inbound_seen += 1
            if len(batch) >= BATCH:
                await bot.step(batch, run=False)
                batch = []
                before = len(transport.sends)
                _absorb(metrics, await bot.autonomous_step(force=True), store)
                _note_positions(metrics, transport, before)
        if batch:
            await bot.step(batch, run=False)
            before = len(transport.sends)
            _absorb(metrics, await bot.autonomous_step(force=True), store)
            _note_positions(metrics, transport, before)
    finally:
        await bot.stop()
        metrics.elapsed_ms = (time.monotonic() - started) * 1000
        metrics.sends = len(transport.sends)
        metrics.replies = [str(s.get("text") or "") for s in transport.sends]
        state = _final_state(store, with_episodes)
        store.close()
    return RunOutcome(metrics=metrics, final_state=state)


def _note_positions(metrics: RunMetrics, transport: "RecordingTransport",
                    before: int) -> None:
    """Record where in the stream each reply landed.

    Two replies with the same inbound position mean the second one spoke before
    anyone answered the first - the closest thing to an "interruption" the replay
    can measure mechanically.
    """
    for _ in transport.sends[before:]:
        metrics.reply_positions.append(metrics.inbound_seen)
    positions = metrics.reply_positions
    metrics.interruptions = sum(
        1 for i in range(1, len(positions)) if positions[i] == positions[i - 1])


def _absorb(metrics: RunMetrics, results: list[Any], store: Any) -> None:
    for result in results:
        if result is None or getattr(result, "state", "") != "completed":
            continue
        metrics.activations += 1
        detail = getattr(result, "detail", {}) or {}
        if detail.get("episode_id"):
            metrics.activations_with_episode += 1
        if detail.get("continuation"):
            metrics.continuations += 1
        metrics.waits += len(getattr(result, "waits", []) or [])
        outcome = getattr(result, "outcome", None)
        if outcome == "silent":
            metrics.silent += 1
        if outcome == "undecided":
            metrics.undecided += 1
        activation = detail.get("activation") or {}
        metrics.prompt_chars += int(activation.get("request_transcript_chars") or 0)
        usage = activation.get("usage")
        if isinstance(usage, dict) and usage:
            metrics.usage_in += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            metrics.usage_out += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            # Providers disagree about where the cache count lives: OpenAI puts
            # it under `prompt_tokens_details.cached_tokens`, some gateways
            # expose it flat. Reading only the flat key reported a real cache as
            # zero - the number was never missing, it was looked for in the
            # wrong place.
            cached = _cache_read(usage)
            if cached is None:
                # A usage block with no cache field anywhere is unknown, not 0.
                metrics.usage_cache_unknown += 1
            else:
                metrics.usage_cached += int(cached)
        else:
            metrics.usage_missing += 1
    try:
        metrics.goals_set = len(store._query("SELECT 1 FROM agent_intents"))
        metrics.episodes_opened = len(store._query("SELECT 1 FROM workflow_episodes"))
    except Exception:
        pass


def _cache_read(usage: dict[str, Any]) -> int | None:
    """Number of prompt tokens served from cache, or None if unreported.

    Checked in the shapes actually seen in the wild: OpenAI's nested
    `prompt_tokens_details.cached_tokens`, the flat `cached_tokens` /
    `cache_read_input_tokens`, and Anthropic's
    `cache_read_input_tokens`-style totals. Missing everywhere means unknown -
    it must not be summed as a zero.
    """
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        return int(details["cached_tokens"])
    for key in ("cached_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if value is not None:
            return int(value)
    return None


def _count(store: Any, sql: str) -> int:
    try:
        return int(store._query(sql)[0][0])
    except Exception:
        return 0


def _final_state(store: Any, with_episodes: bool) -> dict[str, Any]:
    state = {
        "workflows": _count(store, "SELECT COUNT(*) FROM workflows"),
        "intents": _count(store, "SELECT COUNT(*) FROM agent_intents"),
        "intents_open": _count(store, "SELECT COUNT(*) FROM agent_intents WHERE status = 'open'"),
        "actions": _count(store, "SELECT COUNT(*) FROM actions"),
        "actions_sent": _count(store, "SELECT COUNT(*) FROM actions WHERE status = 'sent'"),
        "waits": _count(store, "SELECT COUNT(*) FROM agent_waits"),
        "observations": _count(store, "SELECT COUNT(*) FROM observation_events"),
        "episode_mechanism": with_episodes,
    }
    if with_episodes:
        state["episodes"] = _count(store, "SELECT COUNT(*) FROM workflow_episodes")
        state["episodes_open"] = _count(
            store, "SELECT COUNT(*) FROM workflow_episodes WHERE state = 'open'")
        state["episode_activations"] = _count(
            store, "SELECT COUNT(*) FROM workflows WHERE episode_id IS NOT NULL")
    return state


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------

def _naturalness(texts: list[str]) -> dict[str, Any]:
    """Structural proxies only; semantic quality is not machine-judged here."""
    if not texts:
        return {"replies": 0, "mean_chars": 0.0, "max_chars": 0, "empty": 0,
                "note": "no delivered messages to judge"}
    lengths = [len(t) for t in texts]
    return {"replies": len(texts), "mean_chars": round(statistics.fmean(lengths), 1),
            "max_chars": max(lengths),
            "empty": sum(1 for t in texts if not t.strip()),
            "note": "length/structure only; appropriateness needs a human read"}


def _interruptions(texts: list[str]) -> int:
    """Two or more deliveries with no replayed inbound message between them."""
    return 0  # computed per-run from the event stream, see below


def compare(plain: RunOutcome, cont: RunOutcome) -> dict[str, Any]:
    a, b = plain.metrics, cont.metrics
    keys = ("activations", "continuations", "sends", "silent", "undecided", "waits",
            "goals_set", "prompt_chars", "usage_in", "usage_out", "usage_cached",
            "elapsed_ms")
    out = {}
    for key in keys:
        av, bv = getattr(a, key), getattr(b, key)
        out[key] = {"plain": av, "continuation": bv,
                    "delta": (bv - av) if isinstance(av, (int, float)) else None}
    out["usage_missing"] = {"plain": a.usage_missing, "continuation": b.usage_missing}
    out["usage_cache_unknown"] = {"plain": a.usage_cache_unknown,
                                  "continuation": b.usage_cache_unknown}
    out["interruptions"] = {"plain": a.interruptions, "continuation": b.interruptions,
                            "delta": b.interruptions - a.interruptions}
    return out


async def main_async(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    events = load_recorded(db_path, limit=args.limit)
    if not events:
        raise SystemExit("no recorded message observations found")
    usable = sum(1 for e in events if e.as_update() is not None)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"paired-replay-{stamp}"
    config_path = Path(args.config)

    print(f"recorded events: {len(events)} (usable updates: {usable})")
    print(f"model: {args.model or 'from config'}  repeats: {args.repeat}")

    plain_runs: list[RunOutcome] = []
    cont_runs: list[RunOutcome] = []
    for i in range(args.repeat):
        plain = await run_once(events, name="plain", workspace=base / f"plain-{i}",
                               config_path=config_path, model=args.model,
                               with_episodes=False)
        plain_runs.append(plain)
        print(f"plain#{i}: {plain.metrics.activations} activations, "
              f"{plain.metrics.sends} sends, {plain.metrics.continuations} continuations")
        cont = await run_once(events, name="continuation",
                              workspace=base / f"continuation-{i}",
                              config_path=config_path, model=args.model,
                              with_episodes=True)
        cont_runs.append(cont)
        print(f"continuation#{i}: {cont.metrics.activations} activations, "
              f"{cont.metrics.sends} sends, {cont.metrics.continuations} continuations")

    plain = plain_runs[0]
    cont = cont_runs[0]

    def spread(runs: list[RunOutcome], key: str) -> dict[str, Any]:
        values = [getattr(r.metrics, key) for r in runs]
        return {"values": values,
                "min": min(values) if values else 0,
                "max": max(values) if values else 0,
                "spread": (max(values) - min(values)) if values else 0}

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_database": str(db_path),
        "source_chat_id": GROUP_CHAT,
        "recorded_events": len(events),
        "usable_replayed_updates": usable,
        "model": args.model or "from config",
        "limit": args.limit,
        "batch_size": BATCH,
        "repeats": args.repeat,
        "plain": {**plain.metrics.as_dict(),
                  "final_state": plain.final_state,
                  "naturalness": _naturalness(plain.metrics.replies)},
        "continuation": {**cont.metrics.as_dict(),
                         "final_state": cont.final_state,
                         "naturalness": _naturalness(cont.metrics.replies)},
        "comparison": compare(plain, cont),
        # Whether the observed difference exceeds run-to-run noise. Without this
        # a single pair invites exactly the wrong conclusion.
        "run_to_run": {
            "plain": {k: spread(plain_runs, k) for k in ("sends", "continuations", "activations")},
            "continuation": {k: spread(cont_runs, k) for k in ("sends", "continuations", "activations")},
        },
        "continuation_exercised": {
            "plain": sum(r.metrics.continuations for r in plain_runs),
            "continuation": sum(r.metrics.continuations for r in cont_runs),
            "goals_set": sum(r.metrics.goals_set for r in plain_runs + cont_runs),
            "note": ("Zero means the model never kept a goal or a wait open, so no "
                     "activation ever continued another. The continuation mechanism "
                     "was then not what produced any difference in this replay. "
                     "A replay whose arms both arm nothing compares nothing: treat "
                     "the deltas above as noise, not as a result about continuation."),
        },
        "limits": [
            "Messages are replayed from the recorded observation payloads; media is "
            "replayed by file_id reference only and never fetched.",
            "Only message observations have an inbound update. Action results and "
            "edits are excluded.",
            "Replying to the bot is replayed with a stand-in parent id: the recorded "
            "payload kept only a boolean, not the exact message it answered.",
            "Batch size is fixed and artificial; the live scheduler batches on idle "
            "time, so activation boundaries here are not the production ones.",
            "Token usage comes from the activation detail when the provider reports "
            "it; activations without usage are counted separately, not assumed zero.",
            "Naturalness is measured structurally (length, empties). Whether a reply "
            "was socially appropriate still needs a human read.",
            "Both arms replay into isolated databases; nothing is sent to Telegram.",
        ],
    }
    json_path = base.with_suffix(".json")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {json_path}")
    print(json.dumps(report["comparison"], ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "state" / "fox" / "bot.sqlite3"))
    parser.add_argument("--config", default=str(ROOT / "config.fox.toml"))
    parser.add_argument("--out", default=str(ROOT / "docs" / "evaluations"))
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--repeat", type=int, default=1,
                        help="runs per arm; >1 exposes run-to-run noise")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

"""Regression checks for long-conversation measurements and cleanup."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nervipulsa.ports import ModelResponse
from nervipulsa.providers import ScriptedBackend

import experiments.long_conversation as experiment


def _record(index: int, activation: dict, outcome: str = "sent"):
    detail = {"activation": activation}
    record = experiment.TurnRecord(
        index=index, utterance="hello", addressed=True, outcome="reply",
        reason="addressed", latency_s=1.0, sender_id=experiment.ALICE,
        detail=detail, turn_outcome=outcome,
        send_status="sent" if outcome == "sent" else None,
        text="answer" if outcome == "sent" else None,
    )
    for key, value in experiment._usage_from_detail(detail).items():
        setattr(record, key, value)
    return record


def test_summary_separates_missing_usage_zero_and_stale_pause(tmp_path: Path) -> None:
    records = [
        _record(0, {"usage": {"prompt_tokens": 200, "completion_tokens": 20,
                              "prompt_tokens_details": {"cached_tokens": 100},
                              "completion_tokens_details": {"reasoning_tokens": 10}}}),
        _record(1, {"usage": {"prompt_tokens": 100, "completion_tokens": 20}}),
        _record(2, {"usage": {"prompt_tokens": 50, "completion_tokens": 0,
                              "prompt_tokens_details": {"cached_tokens": 0},
                              "completion_tokens_details": {"reasoning_tokens": 0}}}),
        _record(3, {"error_kind": "response_invalid"}, "error"),
        _record(4, {"error_kind": "response_invalid", "not_this_activation": True}, "error"),
        _record(5, {}, "empty"),
    ]
    result = experiment._summarise(records, SimpleNamespace(sends=[1, 2, 3]),
                                   experiment.AppConfig(), tmp_path)
    turns, tokens = result["turns"], result["tokens"]
    assert turns["reply_decisions_with_usage"] == 3
    assert turns["reply_decisions_without_usage"] == 3
    assert turns["invalid_model_responses"] == 1
    assert turns["empty_model_responses"] == 1
    assert tokens["prompt_total"] == 350
    assert tokens["cached_total"] == 100
    assert tokens["cache_hit_rate_pct"] == 40.0  # 100 / (200 + 50), not 100 / 350
    assert tokens["cache_hit_rate_basis"]["decisions_with_both_counters"] == 2
    assert tokens["known_counter_decisions"]["cached_tokens"] == 2
    assert result["cache_curve"][1]["cached"] is None
    assert result["cache_curve"][2]["cached"] == 0
    assert tokens["reasoning_share_pct"] == 50.0
    assert result["latency"]["replies_per_minute"] == 30.0
    report = experiment.render_markdown(result)
    assert "计费模型调用" not in report
    assert "cached -" in report
    assert "cached 0" in report
    assert "不是整段墙钟速率或并发吞吐" in report


def test_missing_counts_do_not_become_zero_percent(tmp_path: Path) -> None:
    record = _record(0, {"usage": {"prompt_tokens": 100}}, "error")
    result = experiment._summarise([record], SimpleNamespace(sends=[]),
                                   experiment.AppConfig(), tmp_path)
    assert result["tokens"]["cache_hit_rate_pct"] is None
    assert result["tokens"]["known_counter_decisions"]["cached_tokens"] == 0
    assert result["cache_curve"][0]["hit_pct"] is None
    assert experiment._usage_from_detail({"activation": {}}) == {}


def test_run_removes_workspace_on_success_and_setup_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(experiment, "ROOT", tmp_path)
    monkeypatch.setattr(experiment, "load_profile", lambda config: "A test bot")
    actual_agent = experiment.NervipulsaAgent
    backend = ScriptedBackend(lambda request: ModelResponse(
        content="test response", model="scripted",
        usage={"prompt_tokens": 10, "completion_tokens": 2,
               "prompt_tokens_details": {"cached_tokens": 0}},
    ))
    monkeypatch.setattr(experiment, "NervipulsaAgent",
                        lambda **kwargs: actual_agent(backend=backend, **kwargs))
    result = asyncio.run(experiment.run(9, history=24, verbose=False))
    assert result["config"]["workspace_ephemeral"] is True
    assert not Path(result["config"]["workspace"]).exists()
    assert list(tmp_path.iterdir()) == []

    def fail(config):
        raise RuntimeError("injected setup failure")

    monkeypatch.setattr(experiment, "load_profile", fail)
    with pytest.raises(RuntimeError, match="injected setup failure"):
        asyncio.run(experiment.run(1, history=24, verbose=False))
    assert list(tmp_path.iterdir()) == []

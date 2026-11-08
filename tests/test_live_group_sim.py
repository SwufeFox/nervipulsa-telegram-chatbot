from types import SimpleNamespace

from experiments import live_group_sim
from experiments.live_group_sim import (
    _has_runtime_evidence,
    _runtime_activation_count,
    _runtime_tool_names,
)


def test_sim_clock_advances_only_by_explicit_elapsed_time():
    clock = live_group_sim.SimulationClock.__new__(live_group_sim.SimulationClock)
    clock._time = 1_000.0

    clock.advance(20.5)

    assert clock() == 1_020.5

def test_simulation_requires_both_activation_and_model_call_evidence():
    assert not _has_runtime_evidence(0, 0)
    assert not _has_runtime_evidence(1, 0)
    assert not _has_runtime_evidence(0, 1)
    assert _has_runtime_evidence(1, 1)

def test_simulation_counts_only_started_runtimes():
    assert _runtime_activation_count([SimpleNamespace(runtime=None)]) == 0
    runtime = SimpleNamespace(actor=SimpleNamespace(activation_count=2))
    agents = [SimpleNamespace(runtime=None), SimpleNamespace(runtime=runtime)]
    assert _runtime_activation_count(agents) == 2

def test_runtime_summary_lists_tool_names_without_schemas():
    tools = [{
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Full schema description",
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
        },
    }]

    assert _runtime_tool_names(tools) == ["send_message"]

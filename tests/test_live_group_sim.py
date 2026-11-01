from types import SimpleNamespace

from experiments.live_group_sim import _has_runtime_evidence, _runtime_activation_count


def test_simulation_requires_both_activation_and_model_call_evidence():
    assert not _has_runtime_evidence(0, 0)
    assert not _has_runtime_evidence(1, 0)
    assert not _has_runtime_evidence(0, 1)
    assert _has_runtime_evidence(1, 1)

def test_simulation_counts_only_started_runtimes():
    assert _runtime_activation_count([SimpleNamespace(runtime=None)]) == 0
    runtime = SimpleNamespace(actor=SimpleNamespace(activations=[object(), object()]))
    agents = [SimpleNamespace(runtime=None), SimpleNamespace(runtime=runtime)]
    assert _runtime_activation_count(agents) == 2

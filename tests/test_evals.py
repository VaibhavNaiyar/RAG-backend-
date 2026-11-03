from evals import run as ev


def test_harness_runs_and_attributes_failures(settings):
    gs = [g for g in ev.load_goldens(type("A", (), {"only": None, "topic": None, "demo": True})())]
    assert len(gs) >= 5
    scores, agg = ev.run_variant("baseline", gs, settings, use_judge=True, verbose=False)
    assert agg["route_accuracy"] == 1.0
    assert all(s.failed_stage in (None, *ev.STAGES) for s in scores)


def test_regression_is_caught_only_with_verifier_off(settings):
    gs = [g for g in ev.load_goldens(type("A", (), {"only": "CLAIM-01,CLAIM-02,COMP-01", "topic": None, "demo": False})())]
    _, with_v = ev.run_variant("regressed_prompt", gs, settings, use_judge=True, verbose=False)
    _, without_v = ev.run_variant("regressed_prompt_no_verifier", gs, settings, use_judge=True, verbose=False)
    assert with_v["groundedness"] > without_v["groundedness"]
    assert without_v["failures_by_stage"]["grounding"] > 0


def test_gate(settings):
    assert ev.gate({"route_accuracy": 0.5}, None)
    assert not ev.gate({"route_accuracy": 1.0, "forbidden_rate": 0.0}, None)

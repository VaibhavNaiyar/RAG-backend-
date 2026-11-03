import pytest

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


def test_each_golden_gets_its_own_trace(settings):
    # Langfuse scores attach per trace id; one shared trace would pile every golden's scores together.
    gs = [g for g in ev.load_goldens(type("A", (), {"only": "SEMA-01,COMP-01", "topic": None, "demo": False})())]
    scores, _ = ev.run_variant("baseline", gs, settings, use_judge=False, verbose=False)
    ids = [s.trace_id for s in scores]
    assert all(ids) and len(set(ids)) == len(ids)


def test_judge_sees_document_and_section_like_the_answerer(settings, pipeline, monkeypatch):
    # "BR-3" and "FDA" live in the section heading and document title, not the chunk text. A judge
    # shown bare text marks every correctly cited rule as unsupported.
    from claimcheck.llm import FakeLLM
    from evals import metrics

    seen = {}
    real = metrics.run_agent

    def spy(name, payload, settings_, llm, **kw):
        seen.update(payload)
        return real(name, payload, settings_, llm, **kw)

    monkeypatch.setattr(metrics, "run_agent", spy)
    r = pipeline.run("Can an ad say 'Lose 20 lbs in your first month - guaranteed'?")
    metrics.judge_groundedness(r, settings, FakeLLM())
    assert all({"document", "section", "text"} <= set(c) for c in seen["context"])


def test_deep_mode_cost_uses_the_real_model_price():
    # deep.py prices from token counts; a stale fallback table made Haiku 5.5 look 10x dearer than the pipeline.
    from claimcheck.llm import _price

    assert _price("anthropic/claude-haiku-5-5", 40_000, 1_000) == pytest.approx(0.0045)  # $0.10 / $0.50 per 1M


def test_deep_mode_reports_the_skill_its_agent_read():
    # The deep agent picks a skill by reading its SKILL.md; without recording it, skill_accuracy scores 0.
    from types import SimpleNamespace as Msg

    from claimcheck.deep import _plan_from

    messages = [Msg(tool_calls=[{"name": "read_file", "args": {"file_path": "/pool/label-facts/SKILL.md"}}]),
                Msg(tool_calls=[{"name": "search_corpus", "args": {"query": "Zepbound maintenance dose"}}]),
                Msg(content="done")]
    plan = _plan_from(messages, "q")
    assert plan.skill == "label-facts" and plan.sub_queries == ["Zepbound maintenance dose"]
    assert _plan_from([Msg(content="declined")], "q") is None


def test_deep_agent_can_read_skills_but_never_write_to_the_pool():
    # Its file backend is the package's skills folder; a deep run wrote skills/tmp/draft.txt there.
    pytest.importorskip("deepagents")
    from deepagents.middleware.filesystem import _check_fs_permission

    from claimcheck.deep import _read_only

    assert _check_fs_permission(_read_only(), "write", "/pool/claim-review/SKILL.md") == "deny"
    assert _check_fs_permission(_read_only(), "write", "/tmp/draft.txt") == "deny"
    assert _check_fs_permission(_read_only(), "read", "/pool/claim-review/SKILL.md") == "allow"

import pytest

from claimcheck.core import pools
from claimcheck.core.agentspec import ContractError, parse_agent_spec
from claimcheck.core.skill import SkillError, SkillScope, parse_skill


def test_pools_load_and_validate():
    assert set(pools.agents()) == {"intent_router", "planner", "compliance_answerer", "claim_verifier", "eval_judge"}
    assert set(pools.skills()) == {"claim-review", "label-facts", "compounding-policy", "ad-rules"}
    assert pools.validate_pools() == []


def test_every_planner_skill_runs_search_answer_verify_in_order():
    for s in pools.skills().values():
        assert [st.name for st in pools.steps_of(s)] == ["search", "compliance_answerer", "claim_verifier"]


def test_planner_skill_may_not_declare_wiring():
    with pytest.raises(SkillError, match="wiring"):
        parse_skill("---\nname: x\ndescription: d\ntools: [search]\n---\nbody", scope=SkillScope.PLANNER)


def test_agent_yaml_may_not_duplicate_description():
    with pytest.raises(ContractError, match="SKILL.md owns it"):
        parse_agent_spec("name: a\ndescription: nope\n")


def test_references_are_whitelisted():
    sk = pools.agent("compliance_answerer").skill
    assert "claim_types.md" in sk.references
    with pytest.raises(SkillError):
        sk.read_reference("../agent.yaml")


def test_model_routes_resolve(settings):
    assert pools.resolve_model(pools.agent("planner").model, settings)
    assert pools.resolve_model(pools.agent("eval_judge").model, settings) != \
        pools.resolve_model(pools.agent("compliance_answerer").model, settings)


def test_agent_effort_reaches_the_model_call(settings):
    # Claude 5.5 models ignore temperature; effort is the knob that changes their behaviour and cost.
    from claimcheck.llm import FakeLLM
    from claimcheck.runtime import run_agent

    seen = {}

    class Spy(FakeLLM):
        def call(self, **kw):
            seen.update(kw)
            return super().call(**kw)

    run_agent("intent_router", {"question": "What does the Wegovy label say about pregnancy?"}, settings, Spy())
    assert seen["effort"] == pools.agent("intent_router").model.effort == "low"


def test_agent_span_is_an_agent_not_a_generation_in_langfuse(settings):
    # Langfuse turns any span with a `model` attribute into a generation; only cc.llm.* are model calls.
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from claimcheck import telemetry
    from claimcheck.llm import FakeLLM
    from claimcheck.runtime import run_agent

    spans = InMemorySpanExporter()
    telemetry.setup_telemetry("test", extra_span_exporter=spans)
    run_agent("intent_router", {"question": "What does the Wegovy label say about pregnancy?"}, settings, FakeLLM())
    agent = next(s for s in spans.get_finished_spans() if s.name == "cc.agent.router")
    assert agent.attributes["langfuse.observation.type"] == "agent"

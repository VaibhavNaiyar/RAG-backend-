"""Per-golden scoring and failure attribution.

Cheap deterministic checks first; the only LLM metric is groundedness, scored
by `eval_judge` on the judge route (a stronger model than the
answerer), against ALL retrieved context — not by the verifier, which already
shaped the answer and would be grading its own work.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from claimcheck.config import Settings
from claimcheck.llm import LLM
from claimcheck.runtime import run_agent
from claimcheck.schemas import RunResult, VerifierReport

#: Pipeline order. A failure is attributed to the FIRST stage that went wrong,
#: because everything downstream of it was working from bad input.
STAGES = ["router", "planner", "retrieval", "answerer", "verifier", "grounding"]


@dataclass
class GoldenScore:
    id: str
    topic: str
    question: str
    status: str
    route_ok: bool
    skill_ok: bool | None = None
    retrieval_hit: bool | None = None
    doc_recall: float | None = None
    citation_validity: float | None = None
    groundedness: float | None = None
    key_facts: float | None = None
    verdict_ok: bool | None = None
    forbidden_hit: list[str] = field(default_factory=list)
    escalated: bool = False
    cost_usd: float = 0.0
    judge_cost_usd: float = 0.0   # eval-only spend, kept out of the per-question cost
    latency_ms: float = 0.0
    trace_id: str = ""
    failed_stage: str | None = None
    why: str = ""
    answer: str = ""

    @property
    def passed(self) -> bool:
        return self.failed_stage is None

    def to_dict(self) -> dict:
        return {**asdict(self), "passed": self.passed}


def answer_text(r: RunResult) -> str:
    if not r.final:
        return r.message
    parts = [r.final.summary, *(c.text for c in r.final.claims), r.final.suggested_rewrite or ""]
    return "\n".join(p for p in parts if p)


def judge_groundedness(r: RunResult, settings: Settings, llm: LLM) -> tuple[float | None, float]:
    """(fraction of final claims the judge finds supported by retrieved context, judge cost)."""
    if not (r.final and r.final.claims and r.hits):
        return None, 0.0
    # Same metadata the answerer saw: "BR-3" lives in a section heading and "FDA" in a document title.
    context = [{"document": h.chunk.doc_title, "page": h.chunk.page, "section": h.chunk.section,
                "text": h.chunk.text} for h in r.hits]
    payload = {"claims": [c.text for c in r.final.claims], "context": context}
    rep, usage = run_agent("eval_judge", payload, settings, llm)
    assert isinstance(rep, VerifierReport)
    n = len(r.final.claims)
    ok = sum(1.0 if c.label == "supported" else 0.5 if c.label == "partial" else 0.0 for c in rep.checks[:n])
    return round(ok / n, 3), usage.cost_usd


def score(g: dict, r: RunResult, settings: Settings, judge_llm: LLM, *, use_judge: bool = True) -> GoldenScore:
    expect_in_scope = g["expect_route"] == "compliance_question"
    route = r.route.route if r.route else "none"
    s = GoldenScore(
        id=g["id"], topic=g["topic"], question=g["question"], status=r.status,
        route_ok=(route == g["expect_route"]) and ((r.status != "refused") == expect_in_scope),
        escalated=r.status == "escalated", cost_usd=r.cost_usd, latency_ms=r.latency_ms,
        trace_id=r.trace_id, answer=answer_text(r)[:1200],
    )
    if not s.route_ok:
        s.failed_stage, s.why = "router", f"routed {route}/{r.status}, expected {g['expect_route']}"
        return s
    if not expect_in_scope:
        return s

    # planner
    if g.get("expect_skill"):
        s.skill_ok = bool(r.plan) and r.plan.skill == g["expect_skill"]
    # retrieval
    got_docs = {h.chunk.doc_id for h in r.hits}
    exp = set(g.get("expect_docs", []))
    if exp:
        s.retrieval_hit = bool(got_docs & exp)
        s.doc_recall = round(len(got_docs & exp) / len(exp), 3)
    # answer
    draft = r.draft
    if draft and draft.claims:
        valid = {h.ref for h in r.hits}
        cited = [c for c in draft.claims if c.citations]
        s.citation_validity = round(
            sum(all(x in valid for x in c.citations) for c in cited) / len(draft.claims), 3
        )
    text = answer_text(r).lower()
    groups = g.get("must_mention", [])
    if groups:
        s.key_facts = round(sum(any(p.lower() in text for p in grp) for grp in groups) / len(groups), 3)
    s.forbidden_hit = [p for p in g.get("must_not_say", []) if p.lower() in text]
    if g.get("expect_verdict"):
        s.verdict_ok = bool(r.final) and r.final.verdict in g["expect_verdict"]
    if use_judge:
        s.groundedness, s.judge_cost_usd = judge_groundedness(r, settings, judge_llm)

    # attribution — first stage that went wrong
    if s.skill_ok is False:
        s.failed_stage, s.why = "planner", f"skill {r.plan.skill if r.plan else None}, expected {g['expect_skill']}"
    elif s.retrieval_hit is False:
        s.failed_stage, s.why = "retrieval", f"retrieved {sorted(got_docs)}, expected any of {sorted(exp)}"
    elif s.forbidden_hit:
        s.failed_stage, s.why = "answerer", f"said forbidden: {s.forbidden_hit}"
    elif s.verdict_ok is False:
        s.failed_stage = "verifier" if (r.draft and r.draft.verdict in g["expect_verdict"]) else "answerer"
        s.why = f"verdict {r.final.verdict if r.final else None} (draft {r.draft.verdict if r.draft else None}), expected {g['expect_verdict']}"
    elif s.key_facts is not None and s.key_facts < 1.0:
        draft_text = "\n".join([draft.summary, *(c.text for c in draft.claims)]).lower() if draft else ""
        draft_ok = all(any(p.lower() in draft_text for p in grp) for grp in groups)
        s.failed_stage = "verifier" if draft_ok else "answerer"
        s.why = f"key facts {s.key_facts:.0%}" + (" (draft had them; verifier removed)" if draft_ok else "")
    elif s.groundedness is not None and s.groundedness < 0.8:
        s.failed_stage, s.why = "grounding", f"judge groundedness {s.groundedness:.0%}"
    return s


def aggregate(scores: list[GoldenScore]) -> dict:
    def mean(xs):
        xs = [float(x) for x in xs if x is not None]
        return round(sum(xs) / len(xs), 3) if xs else None

    lat = sorted(s.latency_ms for s in scores)
    in_scope = [s for s in scores if s.skill_ok is not None or s.retrieval_hit is not None]
    return {
        "n": len(scores),
        "pass_rate": mean(s.passed for s in scores),
        "route_accuracy": mean(s.route_ok for s in scores),
        "skill_accuracy": mean(s.skill_ok for s in in_scope),
        "retrieval_recall": mean(s.retrieval_hit for s in in_scope),
        "citation_validity": mean(s.citation_validity for s in in_scope),
        "groundedness": mean(s.groundedness for s in in_scope),
        "key_fact_recall": mean(s.key_facts for s in in_scope),
        "verdict_accuracy": mean(s.verdict_ok for s in scores),
        "forbidden_rate": mean(bool(s.forbidden_hit) for s in in_scope),
        "escalation_rate": mean(s.escalated for s in in_scope),
        "cost_usd_total": round(sum(s.cost_usd for s in scores), 4),
        "cost_usd_per_q": round(sum(s.cost_usd for s in scores) / max(1, len(scores)), 5),
        "judge_cost_usd_total": round(sum(s.judge_cost_usd for s in scores), 4),
        "latency_p50_ms": lat[len(lat) // 2] if lat else 0,
        "latency_p95_ms": lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0,
        "failures_by_stage": {st: sum(1 for s in scores if s.failed_stage == st) for st in STAGES},
    }

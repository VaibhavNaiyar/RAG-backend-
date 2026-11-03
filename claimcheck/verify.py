"""The verifier: an LLM labels, deterministic code decides.

Two layers, cheapest first:

1. Citation validity — pure code, zero tokens. A claim citing a ref that was
   never retrieved, or citing nothing, is unsupported before any model sees it.
2. Support — the claim_verifier agent reads each claim against ONLY the text it
   cites and labels it supported / partial / unsupported (+ a fix for partial).

Then policy, in code, so it is testable and the same every time:
  * unsupported -> removed
  * partial     -> replaced by the verifier's fix (if it gave one), else removed
  * supported fraction < escalate_below, or nothing left, or a verdict that
    lost its support -> escalate to the compliance team
"""

from __future__ import annotations

from claimcheck import telemetry
from claimcheck.config import Settings
from claimcheck.llm import LLM
from claimcheck.runtime import run_agent
from claimcheck.schemas import Answer, Claim, ClaimCheck, Hit, StageUsage, Verification, VerifierReport


def check_citations(answer: Answer, hits: list[Hit]) -> tuple[list[int], list[str]]:
    """(indexes of claims with no valid citation, list of invalid refs)."""
    valid = {h.ref for h in hits}
    bad_claims, bad_refs = [], []
    for i, c in enumerate(answer.claims):
        refs = [r.strip() for r in c.citations]
        bad_refs += [r for r in refs if r not in valid]
        if not any(r in valid for r in refs):
            bad_claims.append(i)
    return bad_claims, sorted(set(bad_refs))


def verify(answer: Answer, hits: list[Hit], settings: Settings, llm: LLM) -> tuple[Answer, Verification, StageUsage | None]:
    by_ref = {h.ref: h.chunk for h in hits}
    with telemetry.span("verify", claims=len(answer.claims)) as sp:
        no_cite, bad_refs = check_citations(answer, hits)
        to_check = [i for i in range(len(answer.claims)) if i not in no_cite]
        checks: dict[int, ClaimCheck] = {
            i: ClaimCheck(index=i, label="unsupported", reason="no valid citation") for i in no_cite
        }
        usage = None
        if to_check:
            payload = {
                "claims": [
                    {
                        "index": i,
                        "text": answer.claims[i].text,
                        "evidence": [by_ref[r].text for r in answer.claims[i].citations if r in by_ref],
                    }
                    for i in to_check
                ]
            }
            # FakeLLM indexes claims positionally; keep the original index in the payload.
            report, usage = run_agent("claim_verifier", payload, settings, llm)
            assert isinstance(report, VerifierReport)
            for local_i, c in enumerate(report.checks):
                idx = c.index if c.index in to_check else (to_check[local_i] if local_i < len(to_check) else None)
                if idx is not None:
                    checks[idx] = c.model_copy(update={"index": idx})
            for i in to_check:  # a claim the verifier skipped is not silently trusted
                checks.setdefault(i, ClaimCheck(index=i, label="unsupported", reason="verifier returned no label"))

        kept: list[Claim] = []
        removed = rewritten = 0
        for i, claim in enumerate(answer.claims):
            c = checks[i]
            if c.label == "supported":
                kept.append(claim)
            elif c.label == "partial" and c.fix:
                kept.append(Claim(text=c.fix, citations=claim.citations))
                rewritten += 1
            else:
                removed += 1

        n = len(answer.claims)
        supported = sum(1 for c in checks.values() if c.label == "supported") + 0.5 * rewritten
        frac = supported / n if n else 0.0
        verdict_lost = answer.verdict in ("allowed", "not_allowed") and not kept
        escalate = (n == 0) or (frac < settings.escalate_below) or verdict_lost or answer.insufficient_evidence

        final = answer.model_copy(update={"claims": kept})
        if escalate:
            final = final.model_copy(update={"verdict": "needs_review" if answer.verdict != "informational" else "informational"})
        v = Verification(
            checks=sorted(checks.values(), key=lambda c: c.index), invalid_citations=bad_refs,
            supported_fraction=round(frac, 3), removed=removed, rewritten=rewritten, escalate=escalate,
        )
        telemetry.set_attrs(sp, supported_fraction=v.supported_fraction, removed=removed, rewritten=rewritten,
                            escalate=escalate, invalid_citations=bad_refs)
    telemetry.record_verification(v.supported_fraction, removed, rewritten, escalate)
    return final, v, usage

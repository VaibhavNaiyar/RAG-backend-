---
name: claim-review
description: Review a specific piece of marketing copy, slogan, headline,
             testimonial or "can we say X" claim about a GLP-1 medication and
             return allowed / not allowed / needs review with the rule it hits
             and a compliant rewrite. Use whenever the question contains or
             describes wording someone wants to publish. For the general rules
             with no specific wording, use ad-rules instead.
domains:    [health_marketing]
purposes:   [research]
modalities: [unstructured]
# Non-wiring keys are preserved in SkillSpec.extra and read by the pipeline.
sources:    [brand_rules, ftc_health_claims, fda_compounded_glp1, wegovy_label, zepbound_label]
verdict:    required
answerer_references: [claim_types.md]
---

## Procedure

1. **search** (tool) — always include `brand_rules`; add the regulator documents
   the claim touches (FTC for proof and testimonials, FDA compounding guidance
   for compounded products, the label for any outcome or safety number).
2. **compliance_answerer** (agent) — verdict, cited claims, rewrite.
3. **claim_verifier** (agent) — every claim checked against its citations.

## Rules

- Identify the claim TYPE first (establishment, outcome, safety, equivalence,
  implied); `references/claim_types.md` defines them.
- The verdict must name the specific rule or guidance it rests on (e.g. "BR-4",
  "FTC: establishment claims").
- Brand rules are the strictest source: if a brand rule forbids it, it is
  `not_allowed` even when FTC/FDA text alone would only say "be careful".
- A compounded product is never "FDA-approved", never "the same as" a branded
  drug, and never "clinically proven".
- If no source addresses the wording, the verdict is `needs_review` (BR-9).
- The claims are the reasons for the verdict. Facts from the evidence that do
  not bear on this wording (approval dates, adverse-event counts, unrelated
  examples) are left out; the verifier removes what it cannot tie to a
  citation, and too many removals send the review to the compliance team.
- Why this order: the answerer sees only what the search returned, so it cannot
  answer from outside the corpus; the verifier runs last because it checks what
  the answerer wrote.

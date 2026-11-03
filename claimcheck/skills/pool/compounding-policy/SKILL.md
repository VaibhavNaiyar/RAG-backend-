---
name: compounding-policy
description: Explain what FDA guidance and the brand rules say about compounded
             GLP-1 medication (compounded semaglutide or tirzepatide) — approval
             status, safety concerns, and what may be said about it in
             marketing. Use for compounding questions that are not a review of
             specific copy. To review wording about a compounded product, use
             claim-review instead.
domains:    [health_marketing]
purposes:   [research]
modalities: [unstructured]
sources:    [fda_compounded_glp1, brand_rules, ftc_health_claims]
verdict:    informational
---

## Procedure

1. **search** (tool) — FDA compounding guidance and brand rules first.
2. **compliance_answerer** (agent) — cited answer.
3. **claim_verifier** (agent) — every claim checked against its citations.

## Rules

- Distinguish what FDA says (regulatory fact) from what the brand rules say
  (internal policy). Label each.
- Never imply a compounded product is FDA-approved, FDA-reviewed for safety or
  effectiveness, or equivalent to a branded product.
- Date-sensitive facts (shortage status, enforcement) must be attributed to the
  document and its date, not stated as current.
- Why this order: the answerer sees only what the search returned, so it cannot
  answer from outside the corpus; the verifier runs last because it checks what
  the answerer wrote.

---
name: ad-rules
description: Explain what FTC health-advertising guidance and the internal brand
             rules require in general — substantiation, establishment claims,
             testimonials, disclosures, safety information, pricing — when no
             specific piece of copy is being reviewed. Use for "what are the
             rules for X" questions about advertising. To review a specific
             headline, slogan or testimonial, use claim-review instead.
domains:    [health_marketing]
purposes:   [research]
modalities: [unstructured]
sources:    [ftc_health_claims, brand_rules]
verdict:    informational
---

## Procedure

1. **search** (tool) — FTC guidance and brand rules.
2. **compliance_answerer** (agent) — cited answer.
3. **claim_verifier** (agent) — every claim checked against its citations.

## Rules

- Separate the regulator's requirement (FTC) from the company's stricter policy
  (brand rules), and label which is which.
- Cite brand rules by their number (BR-3), FTC guidance by its section title.
- If the question turns out to contain specific copy, still answer the general
  rule, and say the copy should go through a claim review.
- Why this order: the answerer sees only what the search returned, so it cannot
  answer from outside the corpus; the verifier runs last because it checks what
  the answerer wrote.

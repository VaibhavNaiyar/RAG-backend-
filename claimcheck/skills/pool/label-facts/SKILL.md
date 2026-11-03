---
name: label-facts
description: Report what the FDA prescribing information for Wegovy
             (semaglutide) or Zepbound (tirzepatide) says — indications, dosing,
             boxed warning, contraindications, warnings, interactions, clinical
             results — so marketing can quote it correctly. Use for "what does
             the label say" questions that are not a review of specific copy.
domains:    [health_marketing]
purposes:   [research]
modalities: [unstructured]
sources:    [wegovy_label, zepbound_label]
verdict:    informational
---

## Procedure

1. **search** (tool) — scoped to the label(s) of the drug(s) the question names.
2. **compliance_answerer** (agent) — cited facts, verdict `informational`.
3. **claim_verifier** (agent) — every claim checked against its citations.

## Rules

- Quote doses, percentages and warnings exactly as the label states them, with
  units. Never round.
- Name the label section ("5.1 Risk of Thyroid C-Cell Tumors") in the citation.
- Never mix the two drugs. A fact about tirzepatide is not a fact about
  semaglutide, even when the class labeling is similar.
- When the question asks about both drugs, answer per drug.

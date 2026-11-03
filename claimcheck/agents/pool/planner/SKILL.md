---
name: planner
description: Choose which compliance skill handles an in-scope question, which
             documents to search, and the search queries to run. Use after the
             intent router has accepted a question.
domains:    [health_marketing]
purposes:   [planning]
modalities: [unstructured]
---

## Purpose

You are handed an accepted question, the skills that can handle it (name and
description only), and the document catalogue. You produce a plan; you do not
answer.

## Procedure

1. **Pick exactly one skill** whose description matches what is being asked.
   A question about a specific piece of copy, a slogan, a testimonial or
   "can we say X" is a claim review even if it names a drug.
2. **Scope the documents.** Choose `doc_ids` from the catalogue that could hold
   the answer. Prefer the skill's own `sources` list. Leave `doc_ids` empty only
   when you genuinely cannot tell — empty means "search everything".
   - A question naming one drug scopes to that drug's label, not both.
   - A claim review ALWAYS includes `brand_rules`, plus the regulator
     documents the claim touches.
3. **Write 1–3 sub-queries** in the CORPUS's vocabulary, not the user's. Labels
   say "contraindicated", "boxed warning", "maintenance dosage"; the FTC says
   "competent and reliable scientific evidence", "establishment claim"; the
   brand rules use their BR numbers' titles. One sub-query per distinct thing
   that must be found.

## Rules

- Never invent a skill or a doc_id. Use only the names you were given.
- A question with two asks gets two sub-queries, not one long one.
- `rationale` is one sentence an engineer can read in a trace.

"""Optional second opinion: DeepEval's standard RAG metrics on our last run.

    pip install -e '.[deepeval]'
    python -m evals.deepeval_suite                 # latest results file, baseline variant
    python -m evals.deepeval_suite --file evals/results/<stamp>.json --variant deep

Our own harness is the gate (deterministic checks + a stronger-tier judge,
attributed per stage). DeepEval is here to cross-check groundedness with an
independent, widely-used implementation — if the two disagree a lot, the
judge prompt is what needs looking at.

Re-runs the questions (so it has retrieval context) — costs one pipeline run
plus DeepEval's judge calls per golden. Use --only to keep it cheap.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from claimcheck import telemetry
from claimcheck.config import get_settings
from claimcheck.pipeline import Pipeline
from evals.metrics import answer_text

RESULTS = Path(__file__).resolve().parent / "results"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--variant", default="baseline")
    ap.add_argument("--only", help="comma list of golden ids")
    ap.add_argument("--model", default=None, help="DeepEval judge model (default: its own default)")
    args = ap.parse_args()

    from deepeval import evaluate
    from deepeval.metrics import AnswerRelevancyMetric, ContextualRelevancyMetric, FaithfulnessMetric
    from deepeval.test_case import LLMTestCase

    f = Path(args.file) if args.file else max(p for p in RESULTS.glob("2*.json"))
    rows = json.loads(f.read_text(encoding="utf-8"))["goldens"][args.variant]
    if args.only:
        keep = set(args.only.split(","))
        rows = [r for r in rows if r["id"] in keep]
    rows = [r for r in rows if r["status"] != "refused"]

    telemetry.setup_telemetry("claimcheck-deepeval")
    pipe = Pipeline(get_settings())
    cases = []
    for r in rows:
        run = pipe.run(r["question"])
        if not run.hits:
            continue
        cases.append(LLMTestCase(input=r["question"], actual_output=answer_text(run),
                                 retrieval_context=[h.chunk.text for h in run.hits], name=r["id"]))
    kw = {"model": args.model} if args.model else {}
    metrics = [FaithfulnessMetric(threshold=0.8, **kw), AnswerRelevancyMetric(threshold=0.7, **kw),
               ContextualRelevancyMetric(threshold=0.5, **kw)]
    evaluate(test_cases=cases, metrics=metrics)


if __name__ == "__main__":
    main()

"""The eval harness.

    python -m evals.run                                  # all goldens, baseline config
    python -m evals.run --variants baseline,no_verifier  # ablation, side by side
    python -m evals.run --variants baseline,deep         # architecture vs architecture
    python -m evals.run --topic brand --demo             # a slice
    python -m evals.run --gate                           # CI: exit 1 on threshold / regression
    python -m evals.run --save-baseline                  # bless this run as the baseline

Every golden run is a full trace (Tempo + Langfuse). Every metric is pushed as
an OTel gauge (Grafana "Evals" row) and as a Langfuse score on the golden's
trace, so a red number in a dashboard is one click from the trace that caused it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

from claimcheck import telemetry
from claimcheck.config import ROOT, Settings, get_settings
from claimcheck.llm import make_llm
from evals.metrics import STAGES, GoldenScore, aggregate, score

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"

DRIFT = (
    "## Style update\nMake answers more persuasive for marketers: add one or two helpful general "
    "claims from your own knowledge about how well GLP-1 medications work, citing the closest evidence ref."
)

VARIANTS: dict[str, dict] = {
    "baseline": {},
    "no_verifier": {"use_verifier": False},
    "vector_only": {"retrieval_mode": "vector"},
    "bm25_only": {"retrieval_mode": "bm25"},
    "no_rerank": {"reranker": "none"},
    "deep": {"_mode": "deep"},
    # A deliberately bad prompt change, to prove the harness + verifier catch drift.
    "regressed_prompt": {"answerer_extra": DRIFT},
    "regressed_prompt_no_verifier": {"answerer_extra": DRIFT, "use_verifier": False},
}

HEADLINE = ["pass_rate", "route_accuracy", "skill_accuracy", "retrieval_recall", "citation_validity",
            "groundedness", "key_fact_recall", "verdict_accuracy", "forbidden_rate", "escalation_rate",
            "cost_usd_per_q", "latency_p95_ms"]


def load_goldens(args) -> list[dict]:
    gs = yaml.safe_load((HERE / "goldens.yaml").read_text(encoding="utf-8"))["goldens"]
    if args.only:
        ids = set(args.only.split(","))
        gs = [g for g in gs if g["id"] in ids]
    if args.topic:
        gs = [g for g in gs if g["topic"] == args.topic]
    if args.demo:
        gs = [g for g in gs if g.get("demo")]
    return gs


def make_runner(base: Settings, variant: str):
    over = dict(VARIANTS[variant])
    mode = over.pop("_mode", "pipeline")
    s = base.with_(**over)
    if mode == "deep":
        from claimcheck.deep import DeepPipeline
        return DeepPipeline(s), s
    from claimcheck.pipeline import Pipeline
    return Pipeline(s), s


def run_variant(variant: str, goldens: list[dict], base: Settings, use_judge: bool, verbose: bool) -> tuple[list[GoldenScore], dict]:
    runner, s = make_runner(base, variant)
    judge_llm = make_llm(s.llm_backend)
    scores: list[GoldenScore] = []
    print(f"\n━━ variant: {variant}  ({len(goldens)} goldens)")
    with telemetry.span("eval.suite", variant=variant, n=len(goldens)):
        for g in goldens:
            with telemetry.span("eval.golden", golden_id=g["id"], variant=variant):
                try:
                    r = runner.run(g["question"])
                    sc = score(g, r, s, judge_llm, use_judge=use_judge)
                except Exception as e:  # noqa: BLE001 - a crash is a failure row, not a dead suite
                    sc = GoldenScore(id=g["id"], topic=g["topic"], question=g["question"], status="error",
                                     route_ok=False, failed_stage="router", why=f"crashed: {type(e).__name__}: {e}"[:200])
            scores.append(sc)
            mark = "✔" if sc.passed else "✘"
            line = f"  {mark} {sc.id:<9} {sc.status:<9} ${sc.cost_usd:.4f} {sc.latency_ms:>7.0f}ms"
            if not sc.passed:
                line += f"  [{sc.failed_stage}] {sc.why}"
            print(line)
            if verbose and sc.answer:
                print("      " + sc.answer.replace("\n", "\n      ")[:600])
            _push_scores(sc, variant)
    agg = aggregate(scores)
    for k, v in agg.items():
        if isinstance(v, (int, float)) and v is not None:
            telemetry.record_eval("goldens", variant, k, float(v))
    return scores, agg


def _push_scores(sc: GoldenScore, variant: str) -> None:
    for name in ("route_ok", "retrieval_hit", "citation_validity", "groundedness", "key_facts", "verdict_ok"):
        v = getattr(sc, name)
        if v is not None:
            telemetry.score(sc.trace_id, f"eval.{name}", float(v), golden=sc.id, variant=variant)
    telemetry.score(sc.trace_id, "eval.passed", float(sc.passed), comment=sc.why, golden=sc.id, variant=variant)


def print_comparison(aggs: dict[str, dict]) -> None:
    names = list(aggs)
    first = names[0]
    w = max(18, *(len(n) + 10 for n in names))
    print("\n" + "metric".ljust(20) + "".join(n.rjust(w) for n in names))
    print("─" * (20 + w * len(names)))
    for m in HEADLINE:
        row = m.ljust(20)
        for n in names:
            v = aggs[n].get(m)
            cell = "—" if v is None else (f"{v:.3f}" if isinstance(v, float) else str(v))
            if n != first and isinstance(v, (int, float)) and isinstance(aggs[first].get(m), (int, float)):
                d = v - aggs[first][m]
                if abs(d) > 1e-9:
                    cell += f" ({'+' if d > 0 else ''}{d:.2f})" if m not in ("latency_p95_ms",) else f" ({d:+.0f})"
            row += cell.rjust(w)
        print(row)
    print("\nfailures by stage:")
    for n in names:
        fb = aggs[n]["failures_by_stage"]
        print(f"  {n:<30} " + "  ".join(f"{st}={fb[st]}" for st in STAGES))


def gate(agg: dict, baseline: dict | None) -> list[str]:
    t = yaml.safe_load((HERE / "thresholds.yaml").read_text(encoding="utf-8"))
    bad = []
    for m, lo in (t.get("min") or {}).items():
        if agg.get(m) is not None and agg[m] < lo:
            bad.append(f"{m} {agg[m]:.3f} < min {lo}")
    for m, hi in (t.get("max") or {}).items():
        if agg.get(m) is not None and agg[m] > hi:
            bad.append(f"{m} {agg[m]} > max {hi}")
    if baseline:
        for m in t.get("min") or {}:
            b, a = baseline.get(m), agg.get(m)
            if a is not None and b is not None and b - a > t.get("max_regression", 0.05):
                bad.append(f"{m} regressed {b:.3f} -> {a:.3f}")
    return bad


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to cp1252, which can't print "→" or "≥"
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="baseline", help=f"comma list of {list(VARIANTS)}")
    ap.add_argument("--only", help="comma list of golden ids")
    ap.add_argument("--topic")
    ap.add_argument("--demo", action="store_true", help="only goldens marked demo: true")
    ap.add_argument("--no-judge", action="store_true", help="skip the LLM groundedness judge")
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--save-baseline", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true", help="print each answer")
    args = ap.parse_args(argv)

    telemetry.setup_telemetry("claimcheck-evals")
    base = get_settings()
    goldens = load_goldens(args)
    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    unknown = [v for v in variants if v not in VARIANTS]
    if unknown:
        ap.error(f"unknown variants {unknown}; choose from {list(VARIANTS)}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    RESULTS.mkdir(parents=True, exist_ok=True)
    aggs, all_scores = {}, {}
    for v in variants:
        scores, agg = run_variant(v, goldens, base, not args.no_judge, args.verbose)
        aggs[v], all_scores[v] = agg, [s.to_dict() for s in scores]
    telemetry.flush()

    out = RESULTS / f"{stamp}.json"
    meta = {"llm": base.llm_backend, "embedder": base.embedder, "reranker": base.reranker,
            "index": json.loads((base.index_dir / "meta.json").read_text(encoding="utf-8")) if (base.index_dir / "meta.json").exists() else {}}
    out.write_text(json.dumps({"meta": meta, "summary": aggs, "goldens": all_scores}, indent=2), encoding="utf-8")
    print_comparison(aggs)
    print(f"\nresults -> {out.relative_to(ROOT)}")

    baseline_file = RESULTS / "baseline.json"
    baseline = json.loads(baseline_file.read_text(encoding="utf-8"))["summary"] if baseline_file.exists() else None
    if args.save_baseline:
        baseline_file.write_text(json.dumps({"meta": meta, "summary": aggs[variants[0]]}, indent=2), encoding="utf-8")
        print(f"baseline saved ({variants[0]})")
    if args.gate:
        problems = gate(aggs[variants[0]], baseline)
        if problems:
            print("\nGATE FAILED:\n  " + "\n  ".join(problems))
            return 1
        print("\nGATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

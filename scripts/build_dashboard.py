"""Generate infra/grafana/dashboards/claimcheck.json. Edit here, not the JSON.

OTLP -> Prometheus name translation adds unit/type suffixes (`_milliseconds`,
`_total`, `_bucket`) that differ across collector versions, so every query
matches on a `__name__` regex instead of a guessed exact name.
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "infra/grafana/dashboards/claimcheck.json"
PROM = {"type": "prometheus", "uid": "${prom}"}
TEMPO = {"type": "tempo", "uid": "${tempo}"}
_id = iter(range(1, 999))


def m(name: str, suffix: str = "") -> str:
    return f'{{__name__=~"{name}{suffix}", service_name=~".*"}}'.replace(', service_name=~".*"', "")


def ts(title, exprs, x, y, w=8, h=8, unit="short", legend="{{status}}"):
    return {"id": next(_id), "type": "timeseries", "title": title, "datasource": PROM,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "targets": [{"refId": chr(65 + i), "expr": e, "legendFormat": lf, "datasource": PROM}
                        for i, (e, lf) in enumerate(exprs)]}


def stat(title, expr, x, y, w=4, h=4, unit="short", decimals=2):
    return {"id": next(_id), "type": "stat", "title": title, "datasource": PROM,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "colorMode": "value"},
            "targets": [{"refId": "A", "expr": expr, "datasource": PROM}]}


def row(title, y):
    return {"id": next(_id), "type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}


req = m("claimcheck_requests.*")
panels = [
    row("Traffic & cost", 0),
    stat("Questions (1h)", f"sum(increase({req}[1h]))", 0, 1, decimals=0),
    stat("Escalation rate (1h)", f'sum(increase({m("claimcheck_requests.*")[:-1]}, status="escalated"}}[1h])) / sum(increase({req}[1h]))', 4, 1, unit="percentunit"),
    stat("Refusal rate (1h)", f'sum(increase({m("claimcheck_requests.*")[:-1]}, status="refused"}}[1h])) / sum(increase({req}[1h]))', 8, 1, unit="percentunit"),
    stat("LLM spend (1h)", f'sum(increase({m("claimcheck_llm_cost.*")}[1h]))', 12, 1, unit="currencyUSD", decimals=4),
    stat("Avg cost / question", f'sum(rate({m("claimcheck_request_cost.*_sum")}[1h])) / sum(rate({m("claimcheck_request_cost.*_count")}[1h]))', 16, 1, unit="currencyUSD", decimals=5),
    stat("p95 latency", f'histogram_quantile(0.95, sum by (le) (rate({m("claimcheck_request_duration.*_bucket")}[15m])))', 20, 1, unit="ms", decimals=0),
    ts("Questions by outcome", [(f"sum by (status) (rate({req}[5m]))", "{{status}}")], 0, 5),
    ts("p95 latency by stage", [(f'histogram_quantile(0.95, sum by (le, stage) (rate({m("claimcheck_stage_duration.*_bucket")}[5m])))', "{{stage}}")], 8, 5, unit="ms"),
    ts("LLM cost by stage", [(f'sum by (stage) (rate({m("claimcheck_llm_cost.*")}[5m]))', "{{stage}}")], 16, 5, unit="currencyUSD"),
    row("Answer quality (online)", 13),
    ts("Verifier: supported fraction (avg)", [(f'sum(rate({m("claimcheck_verifier_supported_fraction.*_sum")}[10m])) / sum(rate({m("claimcheck_verifier_supported_fraction.*_count")}[10m]))', "supported")], 0, 14, unit="percentunit"),
    ts("Verifier: claims removed / rewritten", [(f'sum(rate({m("claimcheck_verifier_claims_removed.*")}[10m]))', "removed"), (f'sum(rate({m("claimcheck_verifier_claims_rewritten.*")}[10m]))', "rewritten"), (f'sum(rate({m("claimcheck_verifier_escalations.*")}[10m]))', "escalations")], 8, 14),
    ts("Retrieval: top rerank score & scope widening", [(f'sum(rate({m("claimcheck_retrieval_top_score.*_sum")}[10m])) / sum(rate({m("claimcheck_retrieval_top_score.*_count")}[10m]))', "top score"), (f'sum(rate({m("claimcheck_retrieval_unscoped_retries.*")}[10m]))', "widened")], 16, 14),
    ts("Tokens by model", [(f'sum by (model, direction) (rate({m("claimcheck_llm_tokens.*")}[5m]))', "{{model}} {{direction}}")], 0, 22, w=24, h=7),
    row("Offline evals (latest run per variant)", 29),
    {"id": next(_id), "type": "bargauge", "title": "Eval metrics by variant", "datasource": PROM,
     "gridPos": {"x": 0, "y": 30, "w": 16, "h": 12},
     "fieldConfig": {"defaults": {"unit": "percentunit", "min": 0, "max": 1,
                                  "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "orange", "value": 0.8}, {"color": "green", "value": 0.9}]}}, "overrides": []},
     "options": {"orientation": "horizontal", "displayMode": "gradient", "reduceOptions": {"calcs": ["lastNotNull"]}},
     "targets": [{"refId": "A", "datasource": PROM, "legendFormat": "{{variant}} · {{metric}}",
                  "expr": f'max by (variant, metric) (last_over_time({m("claimcheck_eval_metric.*")[:-1]}, metric=~"pass_rate|groundedness|key_fact_recall|retrieval_recall|citation_validity|verdict_accuracy|route_accuracy"}}[30d]))'}]},
    {"id": next(_id), "type": "table", "title": "Eval cost & latency by variant", "datasource": PROM,
     "gridPos": {"x": 16, "y": 30, "w": 8, "h": 12},
     "targets": [{"refId": "A", "datasource": PROM, "format": "table", "instant": True,
                  "expr": f'max by (variant, metric) (last_over_time({m("claimcheck_eval_metric.*")[:-1]}, metric=~"cost_usd_per_q|latency_p95_ms|escalation_rate"}}[30d]))'}],
     "transformations": [{"id": "groupingToMatrix", "options": {"columnField": "metric", "rowField": "variant", "valueField": "Value"}}]},
    row("Traces", 42),
    {"id": next(_id), "type": "table", "title": "Recent question traces (click a trace id)", "datasource": TEMPO,
     "gridPos": {"x": 0, "y": 43, "w": 24, "h": 10},
     "targets": [{"refId": "A", "datasource": TEMPO, "queryType": "traceql", "limit": 30,
                  "query": '{ name = "cc.question" }'}]},
]

dash = {
    "uid": "claimcheck", "title": "ClaimCheck", "schemaVersion": 39, "version": 1, "refresh": "10s",
    "time": {"from": "now-1h", "to": "now"}, "tags": ["claimcheck", "llm", "rag"],
    "templating": {"list": [
        {"type": "datasource", "name": "prom", "label": "Prometheus", "query": "prometheus"},
        {"type": "datasource", "name": "tempo", "label": "Tempo", "query": "tempo"},
    ]},
    "panels": panels,
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(dash, indent=2))
print(f"wrote {OUT} ({len(panels)} panels)")

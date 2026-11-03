"""One instrumentation, two backends.

Every stage is an OpenTelemetry span and every number is an OTel metric. The
same TracerProvider feeds:

  * the OTLP collector  -> Tempo (traces) + Prometheus (metrics) -> Grafana
  * Langfuse            -> LLM-level traces, generations, cost, eval scores

So there is no "Langfuse code path" and "Grafana code path" to drift apart:
the pipeline only ever talks to OpenTelemetry. Langfuse is attached as an
extra span processor on the same provider, and reads the `gen_ai.*` and
`langfuse.*` attributes we set on spans.

Everything degrades to a no-op: no endpoint, no keys, no packages -> the code
still runs, it just exports nothing.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

log = logging.getLogger(__name__)

_state: dict[str, Any] = {"ready": False, "langfuse": None, "tp": None, "mp": None}
_instruments: dict[str, Any] = {}


def setup_telemetry(service: str | None = None, *, extra_span_exporter: Any = None) -> None:
    """Idempotent. Call once at process start (API, CLI, eval runner)."""
    if _state["ready"]:
        if extra_span_exporter is not None:
            _state["tp"].add_span_processor(SimpleSpanProcessor(extra_span_exporter))
        return
    name = service or os.environ.get("OTEL_SERVICE_NAME", "claimcheck")
    resource = Resource.create({"service.name": name, "service.version": "0.1.0"})
    tp = TracerProvider(resource=resource)
    readers = []

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

            tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")))
            readers.append(
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"), export_interval_millis=5000
                )
            )
            log.info("OTLP export -> %s", endpoint)
        except ImportError:
            log.warning("opentelemetry-exporter-otlp not installed; OTLP export off")

    if extra_span_exporter is not None:
        tp.add_span_processor(SimpleSpanProcessor(extra_span_exporter))

    trace.set_tracer_provider(tp)
    mp = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(mp)
    _state.update(tp=tp, mp=mp, ready=True)
    _state["langfuse"] = _init_langfuse(tp)


def _init_langfuse(tp: TracerProvider) -> Any:
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import Langfuse
    except ImportError:
        log.warning("LANGFUSE_* set but `langfuse` not installed: pip install -e '.[langfuse]'")
        return None
    try:
        client = Langfuse(
            tracer_provider=tp,
            # Export OUR spans too, not only spans made by the Langfuse SDK.
            should_export_span=lambda span: span.name.startswith("cc."),
        )
        log.info("Langfuse attached to the tracer provider")
        return client
    except Exception as e:  # noqa: BLE001 - observability must never break the app
        log.warning("Langfuse init failed: %s", e)
        return None


def tracer() -> trace.Tracer:
    return trace.get_tracer("claimcheck")


def meter() -> metrics.Meter:
    return metrics.get_meter("claimcheck")


def langfuse() -> Any:
    return _state["langfuse"]


# ---- spans ----------------------------------------------------------------
def _attr(v: Any) -> Any:
    if isinstance(v, (str, bool, int, float)):
        return v
    if isinstance(v, (list, tuple)) and all(isinstance(x, (str, int, float, bool)) for x in v):
        return list(v)
    return json.dumps(v, default=str)[:8000]


@contextmanager
def span(name: str, **attrs: Any):
    """`with span("cc.retrieve", query=q) as s:` — every stage goes through here."""
    with tracer().start_as_current_span(f"cc.{name}") as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(k, _attr(v))
        yield s


def set_attrs(s: Any, **attrs: Any) -> None:
    for k, v in attrs.items():
        if v is not None:
            s.set_attribute(k, _attr(v))


def current_trace_id() -> str:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else ""


# ---- metrics --------------------------------------------------------------
def _inst(kind: str, name: str, unit: str = "", desc: str = "") -> Any:
    key = f"{kind}:{name}"
    if key not in _instruments:
        m = meter()
        make = {
            "counter": m.create_counter,
            "histogram": m.create_histogram,
            "gauge": m.create_gauge,
        }[kind]
        _instruments[key] = make(name, unit=unit, description=desc)
    return _instruments[key]


def record_request(status: str, route: str, skill: str, mode: str, cost_usd: float, latency_ms: float) -> None:
    labels = {"status": status, "route": route, "skill": skill, "mode": mode}
    _inst("counter", "claimcheck.requests", desc="Questions answered, by outcome").add(1, labels)
    _inst("histogram", "claimcheck.request.duration", "ms").record(latency_ms, labels)
    _inst("histogram", "claimcheck.request.cost", "usd").record(cost_usd, labels)


def record_stage(stage: str, latency_ms: float) -> None:
    _inst("histogram", "claimcheck.stage.duration", "ms").record(latency_ms, {"stage": stage})


def record_llm(stage: str, model: str, tin: int, tout: int, cost: float) -> None:
    labels = {"stage": stage, "model": model}
    _inst("counter", "claimcheck.llm.tokens").add(tin, {**labels, "direction": "input"})
    _inst("counter", "claimcheck.llm.tokens").add(tout, {**labels, "direction": "output"})
    _inst("counter", "claimcheck.llm.cost", "usd").add(cost, labels)


def record_retrieval(top_score: float, n_hits: int, retried_unscoped: bool) -> None:
    _inst("histogram", "claimcheck.retrieval.top_score").record(top_score)
    _inst("histogram", "claimcheck.retrieval.hits").record(n_hits)
    if retried_unscoped:
        _inst("counter", "claimcheck.retrieval.unscoped_retries").add(1)


def record_verification(supported_fraction: float, removed: int, rewritten: int, escalated: bool) -> None:
    _inst("histogram", "claimcheck.verifier.supported_fraction").record(supported_fraction)
    _inst("counter", "claimcheck.verifier.claims_removed").add(removed)
    _inst("counter", "claimcheck.verifier.claims_rewritten").add(rewritten)
    if escalated:
        _inst("counter", "claimcheck.verifier.escalations").add(1)


def record_eval(suite: str, variant: str, metric: str, value: float) -> None:
    _inst("gauge", "claimcheck.eval.metric").set(value, {"suite": suite, "variant": variant, "metric": metric})


def score(trace_id: str, name: str, value: float | str, comment: str = "", **meta: Any) -> None:
    """Attach an eval score to a Langfuse trace. No-op without Langfuse."""
    lf = langfuse()
    if not (lf and trace_id):
        return
    try:
        lf.create_score(trace_id=trace_id, name=name, value=value, comment=comment or None, metadata=meta or None)
    except Exception as e:  # noqa: BLE001
        log.debug("langfuse score failed: %s", e)


def flush() -> None:
    try:
        if _state["tp"]:
            _state["tp"].force_flush()
        if _state["mp"]:
            _state["mp"].force_flush()
        if _state["langfuse"]:
            _state["langfuse"].flush()
    except Exception as e:  # noqa: BLE001
        log.debug("flush failed: %s", e)

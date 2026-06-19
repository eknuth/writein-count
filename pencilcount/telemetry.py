"""OpenTelemetry instrumentation for the write-in tally pipeline.

Optional and OFF by default. If opentelemetry is not installed or [telemetry] is
disabled in config, every helper here is a cheap no-op and the pipeline runs
unchanged. When enabled it exports metrics and traces over OTLP/HTTP, e.g. to a
SigNoz collector.

Metrics emitted:
  writein.images.by_status     (observable gauge)  cumulative funnel: images per status
  writein.match.by_result      (observable gauge)  cumulative tally: target / review / not
  writein.review.queue.size    (observable gauge)  current review-queue depth
  writein.vision.read.latency  (histogram, ms)     per-call vision transcription latency
  writein.stage.duration       (histogram, s)      wall-clock per pipeline phase
  writein.images.processed     (counter)           images handled this run, by stage
  writein.locate.cache         (counter)           layout-region cache hit vs miss
  writein.vision.inflight      (up/down counter)   concurrent in-flight vision reads

Traces: one span per phase (phase.<stage>) plus a root span for the run.
"""
from __future__ import annotations

import contextlib
import os
import time

from .config import CONFIG

_state: dict = {"on": False, "tracer": None, "inst": {}, "providers": []}


def _parse_headers(s: str):
    out = {}
    for pair in (s or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip()] = v.strip()
    return out or None


# ---- observable-gauge callbacks (read the DB read-only at collection time) ----

def _observe(query: str, attr: str):
    from opentelemetry.metrics import Observation
    try:
        import sqlite3
        from . import db
        conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
        try:
            rows = conn.execute(query).fetchall()
            return [Observation(r[1], {attr: r[0]}) for r in rows]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- metrics must never crash the pipeline
        return []


def _cb_status(options):
    return _observe("SELECT status, COUNT(*) FROM images GROUP BY status", "status")


def _cb_match(options):
    return _observe("SELECT match, COUNT(*) FROM results WHERE match IS NOT NULL "
                    "GROUP BY match", "result")


def _cb_review(options):
    from opentelemetry.metrics import Observation
    try:
        import sqlite3
        from . import db
        conn = sqlite3.connect(f"file:{db.DB_PATH}?mode=ro", uri=True)
        try:
            n = conn.execute("SELECT COUNT(*) FROM review_queue").fetchone()[0]
            return [Observation(n)]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []


def init(config=CONFIG) -> bool:
    """Set up providers/exporters/instruments. Returns True if telemetry is live."""
    tcfg = config.telemetry
    if not tcfg.enabled:
        return False
    try:
        from opentelemetry import metrics, trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        print("[telemetry] opentelemetry not installed; running without instrumentation")
        return False

    endpoint = tcfg.endpoint or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    headers = _parse_headers(tcfg.headers)
    resource = Resource.create({"service.name": tcfg.service_name})

    metric_exporters, span_exporters = [], []
    if tcfg.otlp:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        m_kw, t_kw = {}, {}
        if endpoint:
            base = endpoint.rstrip("/")
            m_kw["endpoint"], t_kw["endpoint"] = base + "/v1/metrics", base + "/v1/traces"
        if headers:
            m_kw["headers"] = t_kw["headers"] = headers
        metric_exporters.append(OTLPMetricExporter(**m_kw))
        span_exporters.append(OTLPSpanExporter(**t_kw))
    if tcfg.console:
        from opentelemetry.sdk.metrics.export import ConsoleMetricExporter
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        metric_exporters.append(ConsoleMetricExporter())
        span_exporters.append(ConsoleSpanExporter())

    readers = [PeriodicExportingMetricReader(e, export_interval_millis=tcfg.export_interval_ms)
               for e in metric_exporters]
    mp = MeterProvider(resource=resource, metric_readers=readers)
    metrics.set_meter_provider(mp)
    meter = metrics.get_meter("writein-count")

    tp = TracerProvider(resource=resource)
    for e in span_exporters:
        tp.add_span_processor(BatchSpanProcessor(e))
    trace.set_tracer_provider(tp)

    inst = {
        "vision_latency": meter.create_histogram(
            "writein.vision.read.latency", unit="ms",
            description="Per-call vision transcription latency"),
        "stage_duration": meter.create_histogram(
            "writein.stage.duration", unit="s",
            description="Wall-clock per pipeline phase"),
        "images": meter.create_counter(
            "writein.images.processed", description="Images processed this run, by stage"),
        "cache": meter.create_counter(
            "writein.locate.cache", description="Layout-region cache hit/miss"),
        "inflight": meter.create_up_down_counter(
            "writein.vision.inflight", description="Concurrent in-flight vision reads"),
    }
    meter.create_observable_gauge("writein.images.by_status", callbacks=[_cb_status],
                                  description="Funnel: images per status")
    meter.create_observable_gauge("writein.match.by_result", callbacks=[_cb_match],
                                  description="Tally: matches per result")
    meter.create_observable_gauge("writein.review.queue.size", callbacks=[_cb_review],
                                  description="Review-queue depth")

    _state.update(on=True, tracer=trace.get_tracer("writein-count"), inst=inst,
                  providers=[mp, tp])
    print(f"[telemetry] enabled: service={tcfg.service_name} otlp={tcfg.otlp} "
          f"endpoint={endpoint or 'default(localhost:4318)'} console={tcfg.console}")
    return True


# ---- recording helpers (all no-op when telemetry is off) ----

def record_vision_latency(ms: float, **attrs) -> None:
    if _state["on"]:
        _state["inst"]["vision_latency"].record(ms, attrs)


def add_images(stage: str, n: int = 1) -> None:
    if _state["on"]:
        _state["inst"]["images"].add(n, {"stage": stage})


def add_cache(result: str, n: int = 1) -> None:
    if _state["on"]:
        _state["inst"]["cache"].add(n, {"result": result})


def record_stage_duration(stage: str, seconds: float) -> None:
    if _state["on"]:
        _state["inst"]["stage_duration"].record(seconds, {"stage": stage})


@contextlib.contextmanager
def inflight():
    inst = _state["inst"].get("inflight") if _state["on"] else None
    if inst:
        inst.add(1)
    try:
        yield
    finally:
        if inst:
            inst.add(-1)


@contextlib.contextmanager
def span(name: str, **attrs):
    if not _state["on"] or _state["tracer"] is None:
        yield None
        return
    with _state["tracer"].start_as_current_span(name) as sp:
        for k, v in attrs.items():
            sp.set_attribute(k, v)
        yield sp


@contextlib.contextmanager
def stage_timer(stage: str):
    """Time a phase: records writein.stage.duration and opens a phase span."""
    t0 = time.perf_counter()
    with span(f"phase.{stage}", stage=stage):
        try:
            yield
        finally:
            record_stage_duration(stage, time.perf_counter() - t0)


def shutdown() -> None:
    """Flush and shut down providers so a batch run exports before exit."""
    for p in _state.get("providers", []):
        try:
            p.force_flush()
            p.shutdown()
        except Exception:  # noqa: BLE001
            pass
    _state["on"] = False

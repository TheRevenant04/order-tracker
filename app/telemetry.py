"""OpenTelemetry wiring for the order tracker.

Traces, metrics and logs all go through one pipeline. Where they end up depends
on `OTEL_EXPORTER_OTLP_ENDPOINT`: with a collector endpoint set they are sent
over OTLP, and without one they are written to stdout, so `docker compose logs
app` shows the three signals side by side during local development.
"""

import json
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from opentelemetry import metrics, trace
from opentelemetry.propagate import extract
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    LogRecordExportResult,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    ConsoleMetricExporter,
    PeriodicExportingMetricReader,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
)
from opentelemetry.semconv.attributes.http_attributes import (
    HTTP_REQUEST_METHOD,
    HTTP_RESPONSE_STATUS_CODE,
    HTTP_ROUTE,
)
from opentelemetry.semconv.attributes.service_attributes import (
    SERVICE_NAME,
    SERVICE_VERSION,
)
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.routing import Match


INSTRUMENTATION_NAME = "order-tracker"
SERVICE_VERSION_FALLBACK = "0.1.0"
DEFAULT_EXPORT_INTERVAL_MILLIS = 15000

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(INSTRUMENTATION_NAME)
meter = metrics.get_meter(INSTRUMENTATION_NAME)

# Resolved against whichever provider configure() installs, so the instruments
# below stay usable no matter when the app starts.
requests_counter = meter.create_counter(
    "order.lookup.requests",
    unit="{request}",
    description="Order lookups by route and response status code.",
)
duration_histogram = meter.create_histogram(
    "order.lookup.duration",
    unit="ms",
    description="Time spent looking up an order, by route and response status code.",
)

tracer_provider = None
logger_provider = None
meter_provider = None
metric_reader = None
configured = False

# The OTel logging handler stamps every record with its call site, which for
# lookup lines is always this module and so says nothing useful.
SKIPPED_LOG_ATTRIBUTES = frozenset(
    {"code.file.path", "code.function.name", "code.line.number"}
)


class JsonConsoleLogExporter(LogRecordExporter):
    """Writes every log record to stdout as a single JSON object."""

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.lock = threading.Lock()

    def export(self, batch):
        if not batch:
            return LogRecordExportResult.SUCCESS
        payload = "".join(
            json.dumps(_log_line(record), default=str) + "\n" for record in batch
        )
        with self.lock:
            self.stream.write(payload)
            self.stream.flush()
        return LogRecordExportResult.SUCCESS

    def shutdown(self):
        pass

    def force_flush(self, timeout_millis=10_000):
        return True


def _log_line(record):
    log_record = record.log_record
    line = {}
    timestamp = log_record.observed_timestamp or log_record.timestamp
    if timestamp is not None:
        line["timestamp"] = _iso(timestamp)
    if log_record.severity_text:
        line["severity"] = log_record.severity_text
    if log_record.body is not None:
        line["message"] = log_record.body
    for key, value in (log_record.attributes or {}).items():
        if key not in SKIPPED_LOG_ATTRIBUTES:
            line[key] = value
    if log_record.trace_id is not None:
        line["trace_id"] = f"{log_record.trace_id:032x}"
    if log_record.span_id is not None:
        line["span_id"] = f"{log_record.span_id:016x}"
    line.update(record.resource.attributes)
    return line


def _iso(nanoseconds):
    return datetime.fromtimestamp(nanoseconds / 1e9, timezone.utc).isoformat()


class ServerTracingMiddleware:
    """Gives every HTTP request a SERVER span, named after its route.

    The route is resolved up front with the framework's own matcher so the
    span name stays low cardinality and `http.route` is set before the handler
    runs, which is what the OTel HTTP semantic conventions expect.
    """

    def __init__(self, app, router=None):
        self.app = app
        self.router = router

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        route = self._match_route(scope)
        status_code = 500
        attributes = {HTTP_REQUEST_METHOD: scope.get("method", "")}
        if route is not None:
            attributes[HTTP_ROUTE] = route.path

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        parent = extract(_headers(scope))
        method = scope.get("method", "")
        with tracer.start_as_current_span(
            f"{method} {route.path}" if route else method,
            context=parent,
            kind=SpanKind.SERVER,
            attributes=attributes,
            set_status_on_exception=False,
        ) as span:
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:
                span.record_exception(exc)
                span.set_attribute(HTTP_RESPONSE_STATUS_CODE, status_code)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            span.set_attribute(HTTP_RESPONSE_STATUS_CODE, status_code)
            if status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))

    def _match_route(self, scope):
        if self.router is None:
            return None
        for route in self.router.routes:
            match, _ = route.matches(scope)
            if match is Match.FULL:
                return route
        return None


def _headers(scope):
    return {
        name.decode("latin-1"): value.decode("latin-1")
        for name, value in scope.get("headers", ())
    }


@dataclass
class OrderLookup:
    order_id: str
    route: str
    status_code: int
    priority: str | None = None

    @property
    def found(self):
        return self.status_code < 400


@contextmanager
def order_lookup(order_id, route, status_code=200):
    """Records a span, a metric and a log line for one order lookup.

    `status_code` is the status the surrounding request will answer with. An
    HTTP error raised inside the block replaces it, and anything unexpected is
    recorded as a 500 so a failure is never counted as a success.
    """
    lookup = OrderLookup(order_id=order_id, route=route, status_code=status_code)
    started = time.perf_counter()
    with tracer.start_as_current_span(
        "order.lookup",
        kind=SpanKind.INTERNAL,
        attributes={
            "order.id": order_id,
            HTTP_ROUTE: route,
            HTTP_RESPONSE_STATUS_CODE: status_code,
        },
        set_status_on_exception=False,
    ) as span:
        try:
            yield lookup
        except Exception as exc:
            lookup.status_code = getattr(exc, "status_code", 500)
            span.set_attribute(HTTP_RESPONSE_STATUS_CODE, lookup.status_code)
            span.record_exception(exc)
            if lookup.status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            span.set_attribute("order.lookup.duration_ms", duration_ms)
            _record_lookup(lookup, duration_ms)


def _record_lookup(lookup, duration_ms):
    attributes = {
        HTTP_ROUTE: lookup.route,
        HTTP_RESPONSE_STATUS_CODE: lookup.status_code,
    }
    requests_counter.add(1, attributes)
    duration_histogram.record(duration_ms, attributes)
    logger.info(
        "order lookup",
        extra={
            "event.name": "order.lookup",
            "order.id": lookup.order_id,
            "order.priority": lookup.priority,
            "order.found": lookup.found,
            "order.lookup.duration_ms": round(duration_ms, 3),
            **attributes,
        },
    )


OTLP_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"


def _uses_otlp():
    """True when a collector endpoint is configured, so nothing prints."""
    return bool(os.getenv(OTLP_ENDPOINT_ENV))


def _trace_processor():
    if not _uses_otlp():
        return SimpleSpanProcessor(ConsoleSpanExporter())
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    # Batching matters over the network: one span at a time would be one request
    # per span.
    return BatchSpanProcessor(OTLPSpanExporter())


def _log_exporter():
    if not _uses_otlp():
        return JsonConsoleLogExporter()
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

    return OTLPLogExporter()


def _log_processor():
    # Console writes are cheap enough to do one at a time, and staying immediate
    # is what makes `docker compose logs app` useful while developing.
    return (
        BatchLogRecordProcessor(_log_exporter())
        if _uses_otlp()
        else SimpleLogRecordProcessor(_log_exporter())
    )


def _metric_exporter():
    if not _uses_otlp():
        return ConsoleMetricExporter()
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
        OTLPMetricExporter,
    )

    return OTLPMetricExporter()


def configure():
    """Installs the pipelines. Safe to call more than once."""
    global configured, tracer_provider, logger_provider, meter_provider, metric_reader
    if configured:
        return
    configured = True

    resource = Resource.create(
        {
            SERVICE_NAME: os.getenv("OTEL_SERVICE_NAME", INSTRUMENTATION_NAME),
            SERVICE_VERSION: os.getenv(
                "ORDER_TRACKER_VERSION", SERVICE_VERSION_FALLBACK
            ),
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(_trace_processor())
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(
        _metric_exporter(),
        export_interval_millis=int(
            os.getenv("OTEL_METRIC_EXPORT_INTERVAL", DEFAULT_EXPORT_INTERVAL_MILLIS)
        ),
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(_log_processor())
    root = logging.getLogger()
    root.setLevel(os.getenv("OTEL_LOG_LEVEL", "INFO").upper())
    # LoggingHandler is deprecated in favour of the one in
    # opentelemetry-instrumentation-logging, which pulls in a larger dependency
    # tree. It is the last piece of the pipeline with no stable API yet, so
    # expect to revisit it when the logs SDK settles.
    root.addHandler(
        LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    )


def flush():
    """Pushes the pending metrics batch out, so shutdown does not lose it."""
    if meter_provider is not None:
        meter_provider.force_flush()

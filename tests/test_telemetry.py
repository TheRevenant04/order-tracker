import pytest
from fastapi.testclient import TestClient
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, InMemoryMetricReader
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import main, telemetry


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def sinks(client):
    """Adds in-memory sinks to the global providers, so tests can read signals.

    The span and log SDKs have no way to remove a processor again, so the sinks
    are cleared before each test instead of being torn down.
    """
    spans = InMemorySpanExporter()
    logs = InMemoryLogRecordExporter()
    metrics = InMemoryMetricReader()
    telemetry.tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    telemetry.logger_provider.add_log_record_processor(
        SimpleLogRecordProcessor(logs)
    )
    telemetry.meter_provider.add_metric_reader(metrics)
    spans.clear()
    logs.clear()
    yield spans, logs, metrics
    telemetry.meter_provider.remove_metric_reader(metrics)
    spans.clear()
    logs.clear()


def metric_points(metrics, name):
    points = {}
    for resource_metric in metrics.get_metrics_data().resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name == name:
                    for point in metric.data.data_points:
                        points[
                            (
                                point.attributes.get("http.route"),
                                point.attributes.get("http.response.status_code"),
                            )
                        ] = point
    return points


def lookup_logs(logs):
    return [
        record
        for record in logs.get_finished_logs()
        if record.log_record.attributes.get("event.name") == "order.lookup"
    ]


def server_span(spans):
    return next(span for span in spans.get_finished_spans() if span.kind.name == "SERVER")


def lookup_span(spans):
    return next(span for span in spans.get_finished_spans() if span.name == "order.lookup")


def test_lookup_metric_records_route_and_status(client, sinks):
    _, _, metrics = sinks
    client.get("/api/orders/standard-1001")
    client.get("/api/orders/missing")

    points = metric_points(metrics, "order.lookup.requests")
    assert points[("/api/orders/{order_id}", 200)].value == 1
    assert points[("/api/orders/{order_id}", 404)].value == 1
    assert set(points) == {
        ("/api/orders/{order_id}", 200),
        ("/api/orders/{order_id}", 404),
    }


def test_lookup_metric_records_the_status_the_caller_answers_with(client, sinks):
    _, _, metrics = sinks
    created = client.post(
        "/api/orders",
        json={"customer": "Zoe", "item": "Kettle", "priority": "express"},
    )
    assert created.status_code == 201

    points = metric_points(metrics, "order.lookup.requests")
    assert points[("/api/orders", 201)].value == 1


def test_lookup_metric_treats_unexpected_failures_as_500(client, sinks, monkeypatch):
    def explode(row):
        raise RuntimeError("boom")

    _, _, metrics = sinks
    monkeypatch.setattr(main, "order_detail", explode)
    with TestClient(main.app, raise_server_exceptions=False) as quiet:
        response = quiet.get("/api/orders/standard-1001")
    assert response.status_code == 500

    points = metric_points(metrics, "order.lookup.requests")
    assert points[("/api/orders/{order_id}", 500)].value == 1


def test_lookup_duration_is_recorded_for_each_lookup(client, sinks):
    _, _, metrics = sinks
    client.get("/api/orders/standard-1001")

    point = metric_points(metrics, "order.lookup.duration")[
        ("/api/orders/{order_id}", 200)
    ]
    assert point.count == 1
    assert point.sum > 0


def test_lookup_span_nests_under_the_request_span(client, sinks):
    spans, _, _ = sinks
    client.get("/api/orders/standard-1001")

    server = server_span(spans)
    lookup = lookup_span(spans)

    assert server.name == "GET /api/orders/{order_id}"
    assert server.context.trace_id == lookup.context.trace_id
    assert lookup.parent.span_id == server.context.span_id
    assert lookup.attributes["order.id"] == "standard-1001"
    assert lookup.attributes["http.route"] == "/api/orders/{order_id}"
    assert lookup.attributes["http.response.status_code"] == 200


def test_server_span_records_the_response_status(client, sinks):
    spans, _, _ = sinks
    client.get("/api/orders/missing")

    server = server_span(spans)
    assert server.attributes["http.route"] == "/api/orders/{order_id}"
    assert server.attributes["http.response.status_code"] == 404
    assert server.status.status_code.name == "UNSET"


def test_lookup_log_line_carries_the_route_status_and_trace(client, sinks):
    _, logs, _ = sinks
    response = client.get("/api/orders/standard-1001")

    record = lookup_logs(logs)[0].log_record
    assert record.body == "order lookup"
    assert record.attributes["http.route"] == "/api/orders/{order_id}"
    assert record.attributes["http.response.status_code"] == 200
    assert record.attributes["order.id"] == "standard-1001"
    assert record.attributes["order.priority"] == "standard"
    assert record.attributes["order.found"] is True
    assert record.attributes["order.lookup.duration_ms"] > 0
    assert record.trace_id != 0
    assert lookup_logs(logs)[0].resource.attributes["service.name"] == "order-tracker"
    assert response.status_code == 200


def test_missing_order_log_line_reports_not_found(client, sinks):
    _, logs, _ = sinks
    client.get("/api/orders/missing")

    record = lookup_logs(logs)[0].log_record
    assert record.attributes["order.found"] is False
    assert record.attributes["order.priority"] is None
    assert record.attributes["http.response.status_code"] == 404


@pytest.mark.parametrize(
    "endpoint, use_otlp",
    [(None, False), ("http://otel-collector:4318", True)],
)
def test_exporters_follow_the_collector_endpoint(monkeypatch, endpoint, use_otlp):
    """Console locally, OTLP once a collector endpoint is configured."""
    if endpoint is None:
        monkeypatch.delenv(telemetry.OTLP_ENDPOINT_ENV, raising=False)
    else:
        monkeypatch.setenv(telemetry.OTLP_ENDPOINT_ENV, endpoint)
    assert telemetry._uses_otlp() is use_otlp

    span_processor = telemetry._trace_processor()
    log_processor = telemetry._log_processor()
    if use_otlp:
        assert isinstance(span_processor, BatchSpanProcessor)
        assert isinstance(log_processor, BatchLogRecordProcessor)
        assert isinstance(telemetry._log_exporter(), OTLPLogExporter)
        assert isinstance(telemetry._metric_exporter(), OTLPMetricExporter)
    else:
        assert isinstance(span_processor, SimpleSpanProcessor)
        assert isinstance(log_processor, SimpleLogRecordProcessor)
        assert isinstance(telemetry._log_exporter(), telemetry.JsonConsoleLogExporter)
        assert isinstance(telemetry._metric_exporter(), ConsoleMetricExporter)

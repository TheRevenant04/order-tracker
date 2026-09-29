"""Collecting the evidence: which endpoint failed, the logs, and the traces."""

from incident_response.alerts import AlertPayload
from incident_response.evidence import render_brief

from conftest import (
    OTHER_TRACE_ID,
    ROOT_SPAN_ID,
    TRACE_ID,
    FakeBackends,
    grafana_webhook,
    prometheus_vector,
    tempo_response,
)


def test_finds_the_failing_endpoint_from_the_metric(collector, firing_payload):
    evidence = collector.collect(firing_payload)

    assert [route.route for route in evidence.affected] == ["/api/orders"]
    assert evidence.affected[0].errors == 1
    # The method is only in the traces, never on the metric, because
    # `http_route` is a template.
    assert evidence.affected[0].method == "POST"


def test_computes_the_error_ratio_against_everything_the_route_served(
    collector, firing_payload
):
    route = collector.collect(firing_payload).affected[0]

    assert route.total == 5
    assert route.statuses == {"201": 4, "500": 1}
    assert route.error_ratio == 0.2


def test_the_range_selector_goes_outside_the_label_braces(collector):
    # Prometheus reads `[5m]` as one more label matcher when it sits inside the
    # braces, and answers 400. The collector has no other way to notice, since
    # the warnings it records are exactly what a broken query produces.
    assert collector._error_query(True) == (
        'sum by (http_route) (increase(order_lookup_requests_total'
        '{service_name="order-tracker",http_response_status_code=~"5.."}[5m]))'
    )
    assert collector._total_query(True) == (
        "sum by (http_route, http_response_status_code) (increase("
        'order_lookup_requests_total{service_name="order-tracker"}[5m]))'
    )
    # The cumulative fallback has to drop increase() too: it has no range.
    assert "increase" not in collector._error_query(False)


def test_falls_back_to_the_counter_when_the_window_says_zero(collector, firing_payload):
    # A brand new 5xx series has one sample in the window, so increase()
    # legitimately returns 0.0. Trusting it would report a healthy endpoint
    # during the exact incident this service exists to catch.
    evidence = collector.collect(firing_payload)

    assert evidence.affected[0].errors == 1


def test_collects_the_error_log_lines(collector, firing_payload):
    evidence = collector.collect(firing_payload)

    assert len(evidence.error_lines) == 1
    line = evidence.error_lines[0]
    assert line.route == "/api/orders"
    assert line.status == "500"
    # The body is all Loki stores over OTLP; the useful fields are metadata.
    assert line.line == "order lookup"
    assert line.attributes["order_id"] == "ce158ea6-8c70-4d94-939b-89370c0b1b44"


def test_follows_the_trace_id_from_the_log_line_into_tempo(
    collector, firing_payload
):
    evidence = collector.collect(firing_payload)

    assert [trace.trace_id for trace in evidence.traces] == [TRACE_ID]
    assert evidence.error_lines[0].trace_id == TRACE_ID


def test_keeps_the_exception_message_off_the_root_span(collector, firing_payload):
    trace = collector.collect(firing_payload).traces[0]

    assert trace.error_message == "day is out of range for month"
    assert trace.root.name == "POST /api/orders"
    assert len(trace.failed_spans) == 2


def test_converts_tempo_base64_ids_into_the_hex_the_logs_use(
    collector, firing_payload
):
    """The same id has to be comparable across the two backends."""
    trace = collector.collect(firing_payload).traces[0]

    assert trace.root.span_id == ROOT_SPAN_ID
    assert trace.root.trace_id == TRACE_ID
    assert trace.root.duration_ms == 14.3
    assert trace.root.http_status == "500"


def test_falls_back_to_the_raw_counter_when_increase_has_one_sample(
    settings, firing_payload
):
    """A 5xx series that has only just appeared has no increase() to report.

    That is the normal state of affairs when an alert first fires, and it is
    exactly when the count is wanted, so the raw counter is used instead and
    the fact is recorded rather than hidden.
    """
    import httpx

    from incident_response.backends import TelemetryClient
    from incident_response.evidence import EvidenceCollector

    def only_the_counter(query):
        # Every windowed query answers empty, as Prometheus does with a single
        # sample in the range; only the cumulative fallback has anything.
        if "[5m]" in query:
            return {"status": "success", "data": {"resultType": "vector", "result": []}}
        return prometheus_vector([({"http_route": "/api/orders"}, 2)])

    backends = FakeBackends(settings, prometheus=only_the_counter)
    client = TelemetryClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(backends))
    )
    evidence = EvidenceCollector(settings, client=client).collect(firing_payload)

    assert evidence.count_source == "cumulative_counter"
    assert evidence.affected[0].errors == 2
    assert any("raw counter" in warning for warning in evidence.warnings)
    # The fallback has to actually drop the range selector, or it is the same
    # empty query asked twice.
    assert any(
        "order_lookup_requests_total" in query and "[5m]" not in query
        for query in backends.queries
    )


def test_keeps_going_when_a_backend_is_down(settings, firing_payload):
    import httpx

    from incident_response.backends import TelemetryClient
    from incident_response.evidence import EvidenceCollector

    backends = FakeBackends(settings)
    backends.failing = {"loki"}
    client = TelemetryClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(backends))
    )
    evidence = EvidenceCollector(settings, client=client).collect(firing_payload)

    # Prometheus still answered, so the failing endpoint is still known.
    assert evidence.affected[0].route == "/api/orders"
    assert any("Loki" in warning for warning in evidence.warnings)


def test_records_a_trace_tempo_has_already_dropped(settings, firing_payload):
    import httpx

    from incident_response.backends import TelemetryClient
    from incident_response.evidence import EvidenceCollector

    backends = FakeBackends(settings, traces={})
    client = TelemetryClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(backends))
    )
    evidence = EvidenceCollector(settings, client=client).collect(firing_payload)

    assert evidence.traces == []
    assert evidence.missing_traces == [TRACE_ID]
    # A trace that has aged out is an expected outcome, not a broken backend.
    assert not any("Could not read" in warning for warning in evidence.warnings)


def test_takes_the_most_recent_traces_and_dedupes_repeated_ids(
    settings, firing_payload
):
    import httpx

    from incident_response.backends import TelemetryClient
    from incident_response.evidence import EvidenceCollector

    # Two streams, so two distinct trace ids, and the older one is written
    # first. The cap is two, and the same id written three times is one trace.
    backends = FakeBackends(
        settings,
        logs={
            "status": "success",
            "data": {
                "resultType": "streams",
                "result": [
                    {
                        "stream": {
                            "service_name": "order-tracker",
                            "http_route": "/api/orders",
                            "http_response_status_code": "500",
                            "trace_id": OTHER_TRACE_ID,
                        },
                        "values": [["1790702150000000000", "order lookup"]],
                    },
                    {
                        "stream": {
                            "service_name": "order-tracker",
                            "http_route": "/api/orders",
                            "http_response_status_code": "500",
                            "trace_id": TRACE_ID,
                        },
                        "values": [
                            ["1790702157474823168", "order lookup"],
                            ["1790702157474824000", "order lookup"],
                            ["1790702159000000000", "order lookup"],
                        ],
                    },
                ],
            },
        },
        traces={TRACE_ID: tempo_response(), OTHER_TRACE_ID: tempo_response()},
    )
    client = TelemetryClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(backends))
    )
    evidence = EvidenceCollector(settings, client=client).collect(firing_payload)

    assert len(evidence.error_lines) == 4
    # Three lines share one trace id, so the cap of two distinct traces is
    # reached with only two fetches.
    assert set(evidence.trace_ids) == {TRACE_ID, OTHER_TRACE_ID}
    assert len(evidence.traces) == 2


def test_says_so_plainly_when_nothing_is_failing(settings):
    import httpx

    from incident_response.backends import TelemetryClient
    from incident_response.evidence import EvidenceCollector

    backends = FakeBackends(
        settings,
        error_samples=[],
        total_samples=[],
        logs={"status": "success", "data": {"resultType": "streams", "result": []}},
    )
    client = TelemetryClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(backends))
    )
    evidence = EvidenceCollector(settings, client=client).collect(
        AlertPayload.parse(grafana_webhook())
    )

    assert evidence.affected == []
    assert evidence.headline() == "no failing route identified"
    assert any("No failing requests" in w for w in evidence.warnings)


# The brief ---------------------------------------------------------------


def test_brief_names_the_endpoint_the_error_ratio_and_the_exception(
    collector, firing_payload
):
    brief = render_brief(collector.collect(firing_payload), "20260929T171400Z-abc")

    assert "# Incident 20260929T171400Z-abc" in brief
    assert "| `POST /api/orders` | 1 | 5 | 20.0% |" in brief
    assert "day is out of range for month" in brief
    # The lookup span is nested under the server span, and the brief says so.
    assert "order.lookup` |" in brief
    assert "&nbsp;" in brief
    assert "order=ce158ea6" in brief


def test_brief_carries_the_query_so_the_evidence_can_be_reproduced(
    collector, firing_payload
):
    brief = render_brief(collector.collect(firing_payload), "id")

    assert '`{service_name="order-tracker"} | http_response_status_code=~"5.."`' in brief


def test_brief_lists_the_warnings_when_a_backend_failed(settings, firing_payload):
    import httpx

    from incident_response.backends import TelemetryClient
    from incident_response.evidence import EvidenceCollector

    backends = FakeBackends(settings)
    backends.failing = {"loki"}
    client = TelemetryClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(backends))
    )
    evidence = EvidenceCollector(settings, client=client).collect(firing_payload)

    brief = render_brief(evidence, "id")

    assert "## Collection warnings" in brief
    assert "Could not read error log lines" in brief


def test_brief_says_when_a_trace_is_gone(settings):
    import httpx

    from incident_response.backends import TelemetryClient
    from incident_response.evidence import EvidenceCollector

    backends = FakeBackends(settings, traces={})
    client = TelemetryClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(backends))
    )
    evidence = EvidenceCollector(settings, client=client).collect(
        AlertPayload.parse(grafana_webhook())
    )

    brief = render_brief(evidence, "id")

    assert "## Traces that had already been dropped" in brief
    assert TRACE_ID in brief

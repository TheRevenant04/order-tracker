import os
import sys
from pathlib import Path

import pytest

# The package lives one directory up from the tests, which is the same layout
# pyproject's `pythonpath` describes. Added here so the tests also run when
# pytest is pointed at this directory on its own.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from incident_response.alerts import AlertPayload  # noqa: E402
from incident_response.backends import TelemetryClient  # noqa: E402
from incident_response.config import Settings  # noqa: E402
from incident_response.evidence import EvidenceCollector  # noqa: E402

TRACE_ID = "19a640c55f16f639140dac870d95f277"
OTHER_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
SPAN_ID = "24da4959708fc0ed"
ROOT_SPAN_ID = "8fc1056577f19c47"


def grafana_webhook(status="firing", value=3, annotations=None):
    """A payload shaped like the one Grafana's webhook receiver posts."""
    return {
        "receiver": "incident-responder",
        "status": status,
        "orgId": 1,
        "version": "1",
        "groupKey": '{}:{alertname="5xx responses on the order API"}',
        "truncatedAlerts": 0,
        "externalURL": "http://127.0.0.1:3000",
        "title": f"[{status.upper()}:1] 5xx responses on the order API",
        "state": "alerting" if status == "firing" else "ok",
        "groupLabels": {"alertname": "5xx responses on the order API"},
        "commonLabels": {
            "alertname": "5xx responses on the order API",
            "grafana_folder": "Order Tracker Alerts",
            "rule_uid": "order-tracker-5xx",
            "rule_name": "5xx responses on the order API",
            "severity": "warning",
            "service": "order-tracker",
        },
        "commonAnnotations": {
            "summary": "5xx responses on the order API in the last 5 minutes"
        },
        "alerts": [
            {
                "status": status,
                "labels": {
                    "alertname": "5xx responses on the order API",
                    "grafana_folder": "Order Tracker Alerts",
                    "rule_uid": "order-tracker-5xx",
                    "rule_name": "5xx responses on the order API",
                    "severity": "warning",
                    "service": "order-tracker",
                },
                "annotations": {
                    "summary": "5xx responses on the order API in the last 5 minutes",
                    "description": "The order API served at least one 5xx response.",
                    "dashboardUid": "order-tracker-requests",
                    "panelId": "6",
                    **({"__override__": True} if annotations is None else annotations),
                },
                # The shape a provisioned rule actually sends: a flat number
                # per refId, captured from a real delivery. The wrapped
                # `{"type": "reduce", "value": n}` form is covered separately.
                "values": {"A": value, "C": 1},
                "valueString": (
                    f"[ var='A' labels={{}} value={value} ], "
                    f"[ var='C' labels={{}} value=1 ]"
                ),
                # Grafana writes nanosecond precision, which datetime.fromisoformat
                # will not take on 3.11.
                "startsAt": "2026-09-29T17:14:00.123456789Z",
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": (
                    "http://127.0.0.1:3000/alerting/grafana/"
                    "order-tracker-5xx/view"
                ),
                "fingerprint": "a1b2c3d4e5f60718",
                "silenceURL": "http://127.0.0.1:3000/alerting/silence/new",
                "dashboardURL": "http://127.0.0.1:3000/d/order-tracker-requests",
                "panelURL": "http://127.0.0.1:3000/d/order-tracker-requests?viewPanel=6",
                "imageURL": None,
            }
        ],
    }


def loki_response(entries, attributes=None):
    """A `query_range` result. Attributes arrive inside `stream`, as Loki does."""
    attributes = attributes or {
        "service_name": "order-tracker",
        "event_name": "order.lookup",
        "http_route": "/api/orders",
        "http_response_status_code": "500",
        "order_id": "ce158ea6-8c70-4d94-939b-89370c0b1b44",
        "order_found": "false",
        "detected_level": "INFO",
        # OTel puts the record's own trace and span into the log record, and
        # Loki hands them back as structured metadata like everything else.
        "trace_id": TRACE_ID,
        "span_id": SPAN_ID,
    }
    return {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": attributes,
                    "values": [
                        ["1790702157474823168", "order lookup"],
                        *entries,
                    ],
                }
            ],
            "stats": {},
        },
    }


def tempo_response(trace_id=TRACE_ID, root_span_id=ROOT_SPAN_ID, message=None):
    """A `GET /api/traces/{id}` response, in the OTLP JSON Tempo returns.

    The parentage is the one the real trace has: the server span is the root and
    the `order.lookup` span hangs off it. The ids are the base64 the API hands
    out, and they decode to the hex values the log records carry, which is the
    whole point of the conversion.
    """
    return {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "order-tracker"},
                        },
                        {
                            "key": "service.version",
                            "value": {"stringValue": "0.1.0"},
                        },
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "order-tracker"},
                        "spans": [
                            {
                                "traceId": "GaZAxV8W9jkUDayHDZXydw==",
                                "spanId": "JNpJWXCPwO0=",
                                "parentSpanId": "j8EFZXfxnEc=",
                                "name": "order.lookup",
                                "kind": 1,
                                "startTimeUnixNano": "1790702157474823000",
                                "endTimeUnixNano": "1790702157475756000",
                                "attributes": [
                                    {
                                        "key": "http.route",
                                        "value": {"stringValue": "/api/orders"},
                                    },
                                    {
                                        "key": "http.response.status_code",
                                        "value": {"stringValue": "500"},
                                    },
                                    {
                                        "key": "order.id",
                                        "value": {
                                            "stringValue": "ce158ea6-8c70-4d94-939b-8937"
                                        },
                                    },
                                ],
                                "status": {"code": "STATUS_CODE_ERROR"},
                            },
                            {
                                "traceId": "GaZAxV8W9jkUDayHDZXydw==",
                                "spanId": "j8EFZXfxnEc=",
                                "name": "POST /api/orders",
                                "kind": 2,
                                "startTimeUnixNano": "1790702157474700000",
                                "endTimeUnixNano": "1790702157489000000",
                                "attributes": [
                                    {
                                        "key": "http.route",
                                        "value": {"stringValue": "/api/orders"},
                                    },
                                    {
                                        "key": "http.request.method",
                                        "value": {"stringValue": "POST"},
                                    },
                                    {
                                        "key": "http.response.status_code",
                                        "value": {"stringValue": "500"},
                                    },
                                ],
                                "status": {
                                    "code": "STATUS_CODE_ERROR",
                                    **(
                                        {"message": message}
                                        if message is not None
                                        else {
                                            "message": "day is out of range for month"
                                        }
                                    ),
                                },
                            },
                        ],
                    }
                ],
            }
        ]
    }


def prometheus_vector(samples):
    """A `query` result. `samples` is a list of (labels, value) pairs."""
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": labels, "value": [1790702184.785, str(value)]}
                for labels, value in samples
            ],
        },
    }


class FakeBackends:
    """An httpx transport standing in for Prometheus, Loki and Tempo.

    Routing on each backend's base URL means the collector runs its real query
    building and its real response decoding, so these tests would catch a wrong
    label name or a changed response shape rather than only a changed call.
    """

    def __init__(
        self,
        settings=None,
        error_samples=None,
        total_samples=None,
        logs=None,
        traces=None,
        prometheus=None,
    ):
        settings = settings or Settings()
        self.bases = {
            "prometheus": settings.prometheus_url.rstrip("/"),
            "loki": settings.loki_url.rstrip("/"),
            "tempo": settings.tempo_url.rstrip("/"),
        }
        self.error_samples = (
            error_samples
            if error_samples is not None
            else [({"http_route": "/api/orders"}, 1)]
        )
        self.total_samples = (
            total_samples
            if total_samples is not None
            else [
                ({"http_route": "/api/orders", "http_response_status_code": "201"}, 4),
                ({"http_route": "/api/orders", "http_response_status_code": "500"}, 1),
            ]
        )
        self.logs = logs if logs is not None else loki_response([])
        self.traces = traces if traces is not None else {TRACE_ID: tempo_response()}
        # Lets a test swap in a Prometheus that only answers the fallback.
        self.prometheus = prometheus or self._prometheus
        self.requests = []
        self.queries = []
        self.failing = set()

    def __call__(self, request):
        url = str(request.url)
        self.requests.append(url)
        for name, base in self.bases.items():
            if not url.startswith(base):
                continue
            if name in self.failing:
                return _json_response(503, {"status": "error", "error": "unavailable"})
            if name == "prometheus":
                # Read the query from the decoded params. A substring match on
                # the raw URL would miss, because the query is percent-encoded.
                query = request.url.params.get("query", "")
                self.queries.append(query)
                return _json_response(200, self.prometheus(query))
            if name == "loki":
                return _json_response(200, self.logs)
            # Tempo's health check is a real query, not a trace fetch.
            if not url.startswith(f"{base}/api/traces/"):
                return _json_response(200, {"traces": [], "metrics": {}})
            trace_id = url.rsplit("/", 1)[-1].split("?")[0]
            if trace_id not in self.traces:
                return _json_response(404, {"status": "not found"})
            return _json_response(200, self.traces[trace_id])
        return _json_response(404, {"status": "not found"})

    def _prometheus(self, query):
        # The windowed query is the one carrying a range selector; the
        # cumulative fallback has none.
        windowed = "[5m]" in query
        if "5.." in query:
            return prometheus_vector(self.error_samples if windowed else [])
        return prometheus_vector(self.total_samples if windowed else [])


def _json_response(status, payload):
    import httpx

    return httpx.Response(status, json=payload)


@pytest.fixture
def fake_opencode(tmp_path):
    """Builds a stand-in for the `opencode` binary that runs on either platform.

    Windows will not execute a shebang script, and its shell has no `sleep`, so
    the body is always Python and the wrapper only picks an interpreter.
    """

    def build(body, name="fake-opencode"):
        script = tmp_path / f"{name}.py"
        script.write_text("import sys, time\n" + body, encoding="utf-8")
        if os.name == "nt":
            wrapper = tmp_path / f"{name}.cmd"
            wrapper.write_text(
                f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
            )
        else:
            wrapper = tmp_path / name
            wrapper.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                encoding="utf-8",
            )
        wrapper.chmod(0o755)
        return wrapper

    return build


@pytest.fixture
def settings(tmp_path):
    return Settings(
        incidents_dir=tmp_path / "incidents",
        workspace_dir=tmp_path,
        assistant_timeout=30,
    )


@pytest.fixture
def backends(settings):
    return FakeBackends(settings)


@pytest.fixture
def client(settings, backends):
    import httpx

    transport = httpx.MockTransport(backends)
    return TelemetryClient(settings, client=httpx.Client(transport=transport))


@pytest.fixture
def collector(settings, client):
    return EvidenceCollector(settings, client=client)


@pytest.fixture
def firing_payload():
    return AlertPayload.parse(grafana_webhook())

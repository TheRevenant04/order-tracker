"""Turns a firing alert into the things you need to understand the problem:
which endpoint is actually failing, the log lines for those failures, and the
traces they belong to.

The alert rule aggregates over every route on the service, so it says "the
order API is returning 5xx" and nothing about which route. Prometheus already
carries the answer, because every data point is tagged with `http_route`, so
that is where the affected endpoint comes from rather than from the alert
annotations, which only list the candidates.

Traces are reached through the logs. A log line carries the `trace_id` of the
span that wrote it, which is the same correlation Grafana's `filterByTraceID`
performs, and it means no search API has to be trusted to enumerate the failures
during an incident.

Every stage is optional. A backend that is down, or a metric series that has
not collected two samples yet, costs a line in `warnings` and nothing more.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

from incident_response.backends import BackendError
from incident_response.config import Settings, get_settings

DASHBOARD_PATH = "/d/order-tracker-requests/order-tracker-requests-and-errors"


@dataclass
class RouteErrors:
    """One route's error count over the lookback window."""

    route: str
    # Request counts, not rates: Prometheus extrapolates `increase()` to the
    # window edges, so the raw samples are fractional and are rounded on the
    # way in.
    errors: int = 0
    total: int = 0
    # Every status code seen on this route, not just the 5xx ones, because the
    # ratio of interest is errors against everything the route served.
    statuses: dict = field(default_factory=dict)
    # Filled in from the traces when one of them covers this route.
    method: str | None = None

    @property
    def error_ratio(self):
        return self.errors / self.total if self.total else None

    @property
    def label(self):
        return f"{self.method} {self.route}" if self.method else self.route


@dataclass
class Evidence:
    """Everything collected for one alert instance."""

    alert: object
    payload: object
    collected_at: datetime
    window_start: datetime
    window_end: datetime
    lookback_seconds: int
    query_window: str
    logql: str = ""
    affected: list = field(default_factory=list)
    error_lines: list = field(default_factory=list)
    traces: list = field(default_factory=list)
    # Trace ids that the logs referenced but Tempo no longer has.
    missing_traces: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # Which PromQL shape produced the counts, so a fallback is never mistaken
    # for a windowed number.
    count_source: str = "increase"
    dashboard_url: str = DASHBOARD_PATH

    @property
    def trace_ids(self):
        return [trace.trace_id for trace in self.traces]

    def headline(self):
        """One line, for a notification subject or a status endpoint."""
        if not self.affected:
            return "no failing route identified"
        route = self.affected[0]
        return f"{route.label} {route.errors:.0f} 5xx in {self.query_window}"

    def to_dict(self):
        return {
            "alert": _alert_dict(self.alert),
            "collected_at": self.collected_at.isoformat(),
            "window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
                "lookback_seconds": self.lookback_seconds,
                "query_window": self.query_window,
            },
            "count_source": self.count_source,
            "logql": self.logql,
            "affected_endpoints": [
                {
                    "route": route.route,
                    "method": route.method,
                    "errors": route.errors,
                    "total": route.total,
                    "error_ratio": route.error_ratio,
                    "statuses": route.statuses,
                }
                for route in self.affected
            ],
            "error_log_lines": [_log_dict(line) for line in self.error_lines],
            "traces": [
                {
                    "trace_id": trace.trace_id,
                    "service": trace.service,
                    "error": trace.error_message,
                    "spans": [
                        {
                            "name": span.name,
                            "span_id": span.span_id,
                            "parent_span_id": span.parent_span_id,
                            "duration_ms": span.duration_ms,
                            "status": span.status_code,
                            "message": span.status_message,
                            "http_status": span.http_status,
                            "attributes": span.attributes,
                        }
                        for span in trace.spans
                    ],
                }
                for trace in self.traces
            ],
            "missing_traces": self.missing_traces,
            "warnings": self.warnings,
        }


def _alert_dict(alert):
    if alert is None:
        return None
    fields = asdict(alert)
    for key in ("started_at", "ends_at"):
        value = fields.get(key)
        if isinstance(value, datetime):
            fields[key] = value.isoformat()
    return fields


def _log_dict(line):
    return {
        "timestamp": line.timestamp.isoformat() if line.timestamp else None,
        "line": line.line,
        "trace_id": line.trace_id,
        "span_id": line.span_id,
        "route": line.route,
        "status": line.status,
        "attributes": line.attributes,
    }


class EvidenceCollector:
    """Collects one alert's worth of evidence from the three backends."""

    def __init__(self, settings=None, client=None):
        self.settings = settings or get_settings()
        from incident_response.backends import TelemetryClient

        self._owns_client = client is None
        self.client = client or TelemetryClient(self.settings)

    def close(self):
        if self._owns_client:
            self.client.close()

    # Queries --------------------------------------------------------------

    def _error_query(self, windowed):
        selector = self._selector(self.settings.error_status_regex)
        return f"sum by (http_route) ({self._windowed(selector, windowed)})"

    def _total_query(self, windowed):
        selector = self._selector(None)
        return (
            "sum by (http_route, http_response_status_code) "
            f"({self._windowed(selector, windowed)})"
        )

    def _selector(self, status_regex):
        labels = [f'service_name="{self.settings.service_name}"']
        if status_regex:
            labels.append(f'{self.settings.error_status_label}=~"{status_regex}"')
        return f"{self.settings.error_metric}{{{','.join(labels)}}}"

    def _windowed(self, selector, windowed):
        # The range selector goes *after* the closing brace, or Prometheus
        # reads `[5m]` as another label matcher and rejects the query. And the
        # cumulative fallback queries the bare counter, because `increase()`
        # has no form without a range.
        if not windowed:
            return selector
        return f"increase({selector}[{self.settings.query_window}])"

    def _logql(self):
        # Loki holds the OTLP attributes as structured metadata, not as stream
        # labels, so the status has to be filtered with a pipeline stage. The
        # same query the dashboard's "Error log lines" panel runs.
        return (
            f'{{service_name="{self.settings.service_name}"}}'
            f" | {self.settings.error_status_label}"
            f'=~"{self.settings.error_status_regex}"'
        )

    # Collection -----------------------------------------------------------

    def collect(self, payload):
        now = datetime.now(timezone.utc)
        evidence = Evidence(
            alert=payload.primary,
            payload=payload,
            collected_at=now,
            window_start=now
            - timedelta(seconds=self.settings.evidence_lookback_seconds),
            window_end=now,
            lookback_seconds=self.settings.evidence_lookback_seconds,
            query_window=self.settings.query_window,
            logql=self._logql(),
            dashboard_url=self.settings.grafana_url.rstrip("/") + DASHBOARD_PATH,
        )
        self._collect_routes(evidence)
        self._collect_logs(evidence)
        self._collect_traces(evidence)
        if not evidence.affected and not evidence.error_lines:
            evidence.warnings.append(
                "No failing requests were found in any backend. Either the "
                "incident has already recovered, or these backends are not "
                "holding this service's telemetry."
            )
        return evidence

    def _collect_routes(self, evidence):
        errors, error_source = self._routed_query(
            evidence, "5xx counts", self._error_query
        )
        totals, _ = self._routed_query(evidence, "request counts", self._total_query)
        if error_source == "cumulative":
            evidence.count_source = "cumulative_counter"
            evidence.warnings.append(
                f"Error counts came from the raw counter rather than from "
                f"increase() over {self.settings.query_window}, because no 5xx "
                f"series had two samples inside that window. They are totals "
                f"since start-up, not counts within the window."
            )

        # increase() extrapolates to the window edges, so a single request
        # comes back as 1.03. These are request counts, so they are rounded
        # here; a brief that says "1.034568378072668 5xx" is not a count.
        routes = {}
        for sample in totals or []:
            if not sample.route:
                continue
            entry = routes.setdefault(sample.route, RouteErrors(route=sample.route))
            entry.total += round(sample.value)
            if sample.status:
                entry.statuses[sample.status] = (
                    entry.statuses.get(sample.status, 0) + round(sample.value)
                )
        for sample in errors or []:
            if not sample.route:
                continue
            entry = routes.setdefault(sample.route, RouteErrors(route=sample.route))
            entry.errors += round(sample.value)

        evidence.affected = sorted(
            (route for route in routes.values() if route.errors > 0),
            key=lambda route: (-route.errors, route.route),
        )
        if not evidence.affected:
            evidence.warnings.append(
                f"Prometheus reports no {self.settings.error_status_regex} "
                f"status for {self.settings.service_name} over "
                f"{self.settings.query_window}."
            )

    def _collect_logs(self, evidence):
        lines = self._try(
            evidence,
            "error log lines from Loki",
            self.client.loki_logs,
            self._logql(),
            evidence.window_start,
            evidence.window_end,
            self.settings.max_log_lines,
        )
        evidence.error_lines = lines or []
        if not evidence.error_lines:
            evidence.warnings.append(
                "Loki returned no log lines for the failing requests in the "
                "lookback window."
            )

    def _collect_traces(self, evidence):
        # Most recent first, deduplicated, because a single failing request
        # writes more than one log line against the same trace.
        ordered = sorted(
            evidence.error_lines,
            key=lambda line: line.timestamp or evidence.window_start,
            reverse=True,
        )
        wanted = []
        for line in ordered:
            if line.trace_id and line.trace_id not in wanted:
                wanted.append(line.trace_id)
            if len(wanted) >= self.settings.max_traces:
                break
        for trace_id in wanted:
            trace = self._try(
                evidence, f"trace {trace_id}", self.client.tempo_trace, trace_id
            )
            if trace is None:
                # Tempo answering 404 is a normal outcome rather than a
                # failure: block retention here is minutes, so a trace named
                # by a log line can already be gone. A lookup that failed
                # outright is reported separately by _try as a warning.
                evidence.missing_traces.append(trace_id)
                continue
            evidence.traces.append(trace)
        if evidence.missing_traces:
            evidence.warnings.append(
                f"{len(evidence.missing_traces)} of {len(wanted)} traces "
                f"referenced by the logs were not in Tempo. Its block "
                f"retention is short, so a trace can expire while the log line "
                f"referencing it is still in the lookback window."
            )
        self._attach_methods(evidence)

    def _attach_methods(self, evidence):
        """Reads the HTTP method off the traces, for the endpoint table.

        `http.route` is the template, so the method cannot be recovered from
        the metric; the root span name is where it is recorded.
        """
        for trace in evidence.traces:
            root = trace.root
            if root is None:
                continue
            route = root.attributes.get("http.route")
            method = root.attributes.get("http.request.method")
            if not route:
                head, _, tail = root.name.partition(" ")
                if not tail:
                    continue
                route, method = tail, method or head
            for entry in evidence.affected:
                if entry.route == route and entry.method is None:
                    entry.method = method

    def _routed_query(self, evidence, label, build):
        """Runs a windowed query, falling back to the raw counter.

        `build` is the query builder; `windowed` decides whether it gets a
        range selector. `increase()` needs two samples inside that range. A 5xx
        series that has only just appeared, which is precisely the case this
        responder exists for, has one, and Prometheus answers with a vector of
        zeros rather than with the count that is plainly there. So the windowed
        answer is only trusted when it is non-zero, and the counter is read
        directly otherwise.
        """
        windowed = self._try(
            evidence,
            f"{label} over {self.settings.query_window}",
            self.client.prometheus_query,
            build(True),
        )
        if any(sample.value for sample in windowed or ()):
            return windowed, "increase"
        fallback = self._try(
            evidence,
            f"{label} (cumulative)",
            self.client.prometheus_query,
            build(False),
        )
        if fallback:
            return fallback, "cumulative"
        return windowed or [], "unavailable"

    def _try(self, evidence, label, func, *args):
        """Runs a backend call, turning a failure into a warning.

        A missing backend must not lose the evidence the other two can still
        provide, so the failure is recorded and None comes back.
        """
        try:
            return func(*args)
        except BackendError as exc:
            self._warn(evidence, f"Could not read {label}: {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad stage must not end
            self._warn(evidence, f"Could not read {label}: {exc!r}")
        return None

    @staticmethod
    def _warn(evidence, message):
        if evidence is not None and message not in evidence.warnings:
            evidence.warnings.append(message)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_ratio(value):
    if value is None:
        return "n/a"
    if value >= 1:
        return "100%"
    if value <= 0:
        return "0%"
    return f"{value * 100:.1f}%"


def _format_count(value):
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.2f}"


def render_brief(evidence, incident_id):
    """The human-readable incident brief, and the assistant's starting point."""
    alert = evidence.alert
    lines = []
    add = lines.append

    title = (alert.rule_name if alert else None) or "Alert"
    add(f"# Incident {incident_id} - {title}")
    add("")
    add(f"- **Status**: {alert.status if alert else 'unknown'}")
    if alert and alert.severity:
        add(f"- **Severity**: {alert.severity}")
    if alert and alert.service:
        add(f"- **Service**: {alert.service}")
    if alert and alert.fingerprint:
        add(f"- **Fingerprint**: `{alert.fingerprint}`")
    if alert and alert.started_at:
        add(f"- **Alert started**: {alert.started_at.isoformat()}")
    if alert and alert.value is not None:
        add(f"- **Observed value**: {_format_count(alert.value)}")
    if alert and alert.summary:
        add(f"- **Summary**: {alert.summary}")
    if alert and alert.generator_url:
        add(f"- **Rule**: {alert.generator_url}")
    add(
        f"- **Collected**: {evidence.collected_at.isoformat()}, over a "
        f"{evidence.lookback_seconds}s lookback. The rule itself runs on "
        f"increase() over {evidence.query_window}."
    )
    add(f"- **Dashboard**: {evidence.dashboard_url}")
    add("")

    add("## Affected endpoints")
    add("")
    if evidence.affected:
        add("| Endpoint | 5xx | Total requests | Error ratio | Statuses |")
        add("| --- | ---: | ---: | ---: | --- |")
        for route in evidence.affected:
            statuses = ", ".join(
                f"{code}: {_format_count(count)}"
                for code, count in sorted(route.statuses.items())
            )
            add(
                f"| `{route.label}` | {_format_count(route.errors)} | "
                f"{_format_count(route.total)} | "
                f"{_format_ratio(route.error_ratio)} | {statuses or 'n/a'} |"
            )
    else:
        add("No route was reported as failing by Prometheus.")
    add("")

    add("## Error log lines")
    add("")
    add(
        f"{len(evidence.error_lines)} line(s) from Loki over the lookback "
        f"window, covering {len(set(evidence.trace_ids))} distinct trace(s). "
        f"Query: `{evidence.logql}`"
    )
    add("")
    if evidence.error_lines:
        add("```")
        for line in evidence.error_lines:
            add(line.summary())
        add("```")
    else:
        add("No error log lines were returned.")
    add("")

    add("## Traces")
    add("")
    if evidence.traces:
        for trace in evidence.traces:
            add(f"### `{trace.header()}`")
            add("")
            if trace.service:
                add(f"- **Service**: {trace.service}")
            add(f"- **Spans**: {len(trace.spans)}")
            add("")
            add("| Span | Duration ms | Status | Message |")
            add("| --- | ---: | --- | --- |")
            for span in trace.spans:
                indent = "" if not span.parent_span_id else "&nbsp;&nbsp;"
                add(
                    f"| `{indent}{span.name}` | "
                    f"{span.duration_ms if span.duration_ms is not None else 'n/a'} | "
                    f"{span.status_code.replace('STATUS_CODE_', '')} | "
                    f"{span.status_message or ''} |"
                )
            add("")
    else:
        add("No traces were available for the failing requests.")
        add("")

    if evidence.missing_traces:
        add("## Traces that had already been dropped")
        add("")
        for trace_id in evidence.missing_traces:
            add(f"- `{trace_id}`")
        add("")

    if alert and alert.description:
        add("## Rule description")
        add("")
        add("```")
        add(alert.description.rstrip())
        add("```")
        add("")

    if evidence.warnings:
        add("## Collection warnings")
        add("")
        for warning in evidence.warnings:
            add(f"- {warning}")
        add("")

    return "\n".join(lines).rstrip() + "\n"

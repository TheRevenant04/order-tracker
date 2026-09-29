"""Thin read-only clients for the three telemetry backends.

Only the endpoints this responder actually needs, and only the response fields
it uses, so that a change in either backend shows up as a decode error in one
place rather than as a mysterious missing field downstream.

Two shapes in here are worth knowing about, because both are easy to get wrong:

- **Loki returns structured metadata inside `stream`.** The app's OTLP log
  attributes are metadata rather than stream labels, and Loki normalises the
  dots to underscores, so `http.route` arrives as `http_route`. The query
  response merges both back into a single flat `stream` object, which is why
  `trace_id` can be read straight out of a log line.
- **Tempo returns OTLP JSON.** Span and trace ids are base64 in this encoding,
  while the same ids in the log records are hex, so they are converted on the
  way in to make the two sources directly comparable.
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx


class BackendError(RuntimeError):
    """A backend was unreachable, or answered with something unusable."""


def _decode_id(value):
    """Converts an OTLP base64 id to the hex form the log records use."""
    if not value:
        return None
    try:
        return base64.b64decode(value, validate=True).hex()
    except (ValueError, TypeError):
        return value


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _as_list(value):
    return value if isinstance(value, list) else []


def _attribute_value(raw):
    """Reads whichever of the OTLP AnyValue fields happens to be set."""
    raw = _as_dict(raw)
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in raw:
            return raw[key]
    nested = raw.get("arrayValue") or raw.get("kvlistValue")
    return nested if nested is not None else None


def _attributes(pairs):
    return {
        str(_as_dict(pair).get("key")): _attribute_value(_as_dict(pair).get("value"))
        for pair in pairs or []
        if isinstance(pair, dict)
    }


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _from_nanos(value):
    try:
        return datetime.fromtimestamp(int(value) / 1e9, timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


@dataclass(frozen=True)
class Sample:
    """One Prometheus instant-query result."""

    labels: dict
    timestamp: datetime | None
    value: float

    @property
    def route(self):
        return self.labels.get("http_route")

    @property
    def status(self):
        return self.labels.get("http_response_status_code")


@dataclass(frozen=True)
class LogLine:
    """One Loki log line, with its attributes and correlation ids."""

    timestamp: datetime | None
    line: str
    attributes: dict = field(default_factory=dict)

    @property
    def trace_id(self):
        return self.attributes.get("trace_id")

    @property
    def span_id(self):
        return self.attributes.get("span_id")

    @property
    def route(self):
        return self.attributes.get("http_route")

    @property
    def status(self):
        return self.attributes.get("http_response_status_code")

    @property
    def level(self):
        return self.attributes.get("detected_level") or self.attributes.get(
            "severity_text"
        )

    def summary(self):
        """A one-line rendering for the brief.

        The stored line is only the log body; the useful fields are all in the
        attributes, so they are what get shown.
        """
        parts = [self.timestamp.isoformat() if self.timestamp else "?"]
        if self.route:
            parts.append(f"route={self.route}")
        if self.status:
            parts.append(f"status={self.status}")
        if self.attributes.get("order_id"):
            parts.append(f"order={self.attributes['order_id']}")
        parts.append(f"trace_id={self.trace_id or '-'}")
        if self.line:
            parts.append(f"| {self.line}")
        return " ".join(parts)


@dataclass(frozen=True)
class Span:
    """One Tempo span, with ids already in hex to match the log records."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    kind: str | None
    start: datetime | None
    end: datetime | None
    status_code: str
    status_message: str | None
    attributes: dict

    @property
    def duration_ms(self):
        if self.start is None or self.end is None:
            return None
        return round((self.end - self.start).total_seconds() * 1000, 3)

    @property
    def failed(self):
        return self.status_code.upper().endswith("ERROR")

    @property
    def http_status(self):
        return self.attributes.get("http.response.status_code")


@dataclass(frozen=True)
class Trace:
    """A fetched trace, with the resource shared by all of its spans."""

    trace_id: str
    resource: dict
    spans: tuple[Span, ...] = ()

    @property
    def service(self):
        return self.resource.get("service.name")

    @property
    def failed_spans(self):
        return [span for span in self.spans if span.failed]

    @property
    def root(self):
        parents = {span.span_id for span in self.spans}
        for span in self.spans:
            if not span.parent_span_id or span.parent_span_id not in parents:
                return span
        return self.spans[0] if self.spans else None

    @property
    def error_message(self):
        """The most specific failure message anywhere in the trace.

        The root span carries the exception message the middleware recorded,
        which is the single most useful string in the whole trace, so it wins
        over anything the child spans have.
        """
        root = self.root
        if root is not None and root.status_message:
            return root.status_message
        for span in self.failed_spans:
            if span.status_message:
                return span.status_message
        return None

    def header(self):
        root = self.root
        parts = [f"trace_id={self.trace_id}"]
        if root is not None:
            parts.append(f"root={root.name}")
            if root.duration_ms is not None:
                parts.append(f"duration_ms={root.duration_ms}")
        if self.error_message:
            parts.append(f"error={self.error_message!r}")
        return " ".join(parts)


class TelemetryClient:
    """Reads Prometheus, Loki and Tempo. One client, no writes anywhere."""

    def __init__(self, settings, client=None):
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=settings.http_timeout)

    def close(self):
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def _get(self, url, params, parse=True):
        try:
            response = self._client.get(url, params=params)
            response.raise_for_status()
            # The health probe only asks whether the backend is answering, and
            # Prometheus's `/-/healthy` replies in plain text.
            return response.json() if parse else None
        except httpx.HTTPStatusError as exc:
            raise BackendError(f"{url} answered {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise BackendError(f"{url} is unreachable: {exc}") from exc

    # Prometheus -----------------------------------------------------------

    def prometheus_query(self, query):
        """Runs an instant query. Returns [] for an empty result, not an error."""
        payload = self._get(
            f"{self.settings.prometheus_url.rstrip('/')}/api/v1/query",
            {"query": query},
        )
        if payload.get("status") != "success":
            raise BackendError(f"Prometheus query failed: {payload}")
        samples = []
        for entry in _as_list(_as_dict(payload.get("data")).get("result")):
            entry = _as_dict(entry)
            metric = _as_dict(entry.get("metric"))
            value = entry.get("value")
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                continue
            samples.append(
                Sample(
                    labels={str(k): str(v) for k, v in metric.items()},
                    timestamp=_from_nanos(int(_number(value[0]) * 1e9)),
                    value=_number(value[1]),
                )
            )
        return samples

    # Loki -----------------------------------------------------------------

    def loki_logs(self, query, start, end, limit=None):
        """Runs a range query. LogQL does the filtering; nothing is parsed out
        of the line text, because over OTLP the line is only the log body."""
        params = {
            "query": query,
            # Loki wants nanoseconds, and a string, for these two.
            "start": str(int(start.timestamp() * 1e9)),
            "end": str(int(end.timestamp() * 1e9)),
            "direction": "backward",
        }
        if limit:
            params["limit"] = str(limit)
        payload = self._get(
            f"{self.settings.loki_url.rstrip('/')}/loki/api/v1/query_range", params
        )
        if payload.get("status") != "success":
            raise BackendError(f"Loki query failed: {payload}")
        lines = []
        for stream in _as_list(_as_dict(payload.get("data")).get("result")):
            stream = _as_dict(stream)
            attributes = {
                str(k): str(v) for k, v in _as_dict(stream.get("stream")).items()
            }
            for entry in _as_list(stream.get("values")):
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    continue
                lines.append(
                    LogLine(
                        timestamp=_from_nanos(entry[0]),
                        line=str(entry[1]),
                        attributes=attributes,
                    )
                )
        lines.sort(key=lambda item: item.timestamp or datetime.min.replace(
            tzinfo=timezone.utc
        ))
        return lines

    # Tempo ----------------------------------------------------------------

    def tempo_trace(self, trace_id):
        """Fetches one trace, or None when Tempo has already dropped it.

        Tempo's block retention is short by default, so a trace referenced by a
        log line is not guaranteed to still be there. That is a normal outcome
        here rather than a failure.
        """
        if not trace_id or len(trace_id) != 32:
            return None
        try:
            payload = self._get(
                f"{self.settings.tempo_url.rstrip('/')}/api/traces/{trace_id}", None
            )
        except BackendError as exc:
            if "answered 404" in str(exc):
                return None
            raise
        return self._decode_trace(trace_id, payload)

    @staticmethod
    def _decode_trace(trace_id, payload):
        spans = []
        resource = {}
        for batch in _as_dict(payload).get("batches") or []:
            batch = _as_dict(batch)
            resource = _attributes(_as_dict(batch.get("resource")).get("attributes"))
            for scope in batch.get("scopeSpans") or []:
                for raw in _as_dict(scope).get("spans") or []:
                    raw = _as_dict(raw)
                    status = _as_dict(raw.get("status"))
                    spans.append(
                        Span(
                            name=str(raw.get("name", "")),
                            trace_id=_decode_id(raw.get("traceId")) or trace_id,
                            span_id=_decode_id(raw.get("spanId")) or "",
                            parent_span_id=_decode_id(raw.get("parentSpanId")),
                            kind=raw.get("kind"),
                            start=_from_nanos(raw.get("startTimeUnixNano")),
                            end=_from_nanos(raw.get("endTimeUnixNano")),
                            status_code=str(status.get("code", "UNSET")),
                            status_message=_as_dict(status).get("message"),
                            attributes=_attributes(raw.get("attributes")),
                        )
                    )
        return Trace(trace_id=trace_id, resource=resource, spans=tuple(spans))

    # Reachability ---------------------------------------------------------

    def health(self):
        """Per-backend reachability, for the service's own health endpoint.

        Each check is a cheap real query rather than that backend's own
        readiness endpoint. `/ready` reports whether the cluster has finished
        forming, which is a different question and a misleading one here: Loki
        and Tempo both answer it with 503 for a while after start-up, long
        before they stop serving the queries this responder depends on.
        """
        checks = {
            "prometheus": (self.settings.prometheus_url, "/-/healthy", None),
            "loki": (
                self.settings.loki_url,
                "/loki/api/v1/labels",
                {"limit": "1"},
            ),
            "tempo": (self.settings.tempo_url, "/api/search", {"limit": "1"}),
        }
        status = {}
        for name, (base, path, params) in checks.items():
            try:
                self._get(f"{base.rstrip('/')}{path}", params, parse=False)
                status[name] = "ok"
            except BackendError as exc:
                status[name] = f"unreachable: {exc}"
        return status

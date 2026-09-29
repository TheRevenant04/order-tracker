"""Grafana's webhook payload, normalised.

Grafana delivers an Alertmanager-shaped POST body: a `status` for the group, a
list of `alerts`, and `commonLabels` / `commonAnnotations` hoisting the shared
bits. Everything is read with `.get` and a coercion, because a webhook is an
integration boundary: a missing `annotations` or a `values` block shaped
differently than expected must not take the responder down with it. A field
that cannot be read becomes None, and the raw payload is always kept so
nothing is lost.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone

# The only value in a reduce result this responder cares about: the number the
# alert rule compared against its threshold.
DEFAULT_VALUE_REF = "A"


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _as_text(value):
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _as_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _parse_time(value):
    """Accepts the RFC 3339 strings Grafana sends, and tolerates the rest.

    Grafana writes nanosecond precision, which `fromisoformat` on 3.11 and
    earlier will not take, so the fractional part is trimmed to microseconds.
    """
    text = _as_text(value)
    if not text:
        return None
    normalised = text.replace("Z", "+00:00")
    if "." in normalised:
        head, _, tail = normalised.partition(".")
        digits = "".join(ch for ch in tail if ch.isdigit())
        offset = tail[len(digits):]
        normalised = f"{head}.{digits[:6]}{offset}"
    try:
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _first(*values):
    for value in values:
        text = _as_text(value)
        if text:
            return text
    return None


@dataclass(frozen=True)
class AlertInstance:
    """One rule instance inside the notification."""

    fingerprint: str
    status: str
    rule_uid: str | None
    rule_name: str | None
    folder: str | None
    severity: str | None
    service: str | None
    summary: str | None
    description: str | None
    started_at: datetime | None
    ends_at: datetime | None
    generator_url: str | None
    dashboard_url: str | None
    panel_url: str | None
    # The number the rule compared against its threshold, when the rule used a
    # single reduce. `A` is the refId the provisioned rule queries under.
    value: float | None
    labels: dict = field(default_factory=dict)
    annotations: dict = field(default_factory=dict)

    @property
    def firing(self):
        return self.status.lower() == "firing"

    @classmethod
    def parse(cls, raw):
        raw = _as_dict(raw)
        labels = _as_dict(raw.get("labels"))
        annotations = _as_dict(raw.get("annotations"))
        return cls(
            fingerprint=_as_text(raw.get("fingerprint")) or "",
            status=_as_text(raw.get("status")) or "unknown",
            rule_uid=_first(labels.get("rule_uid"), labels.get("__alert_rule_uid__")),
            rule_name=_first(
                labels.get("rule_name"),
                labels.get("alertname"),
                labels.get("__alert_rule_name__"),
            ),
            folder=_as_text(labels.get("grafana_folder")),
            severity=_as_text(labels.get("severity")),
            service=_first(labels.get("service"), labels.get("job")),
            summary=_as_text(annotations.get("summary")),
            description=_as_text(annotations.get("description")),
            started_at=_parse_time(raw.get("startsAt")),
            ends_at=_parse_time(raw.get("endsAt")),
            generator_url=_as_text(raw.get("generatorURL")),
            dashboard_url=_as_text(raw.get("dashboardURL")),
            panel_url=_as_text(raw.get("panelURL")),
            value=_extract_value(raw.get("values")),
            labels=labels,
            annotations=annotations,
        )


def _extract_value(values):
    """Pulls the reduced number out of a Grafana `values` block.

    Two shapes reach here. A provisioned rule sends a flat number per refId,
    `{"A": 1.03, "C": 1}`, while a managed rule sends the reduction wrapped,
    `{"A": {"type": "reduce", "value": 3}}`. Both are accepted, because reading
    the wrong one costs the responder the number the rule actually compared
    against its threshold.

    The refId is not guaranteed to be `A`, and the threshold node (`C` here)
    carries a useless 1, so a lone candidate is taken but a pair is not guessed
    at: better `unknown` than the wrong number.
    """
    values = _as_dict(values)
    candidates = {}
    for ref_id, entry in values.items():
        number = _as_number(entry)
        if number is None:
            number = _as_number(_as_dict(entry).get("value"))
        if number is not None:
            candidates[ref_id] = number
    if DEFAULT_VALUE_REF in candidates:
        return candidates[DEFAULT_VALUE_REF]
    if len(candidates) == 1:
        return next(iter(candidates.values()))
    return None


@dataclass(frozen=True)
class AlertPayload:
    """A whole webhook delivery, which may carry several firing instances."""

    receiver: str
    status: str
    state: str
    title: str
    external_url: str
    version: str
    group_key: str
    truncated_alerts: int
    group_labels: dict
    common_labels: dict
    common_annotations: dict
    alerts: tuple[AlertInstance, ...] = ()
    raw: dict = field(default_factory=dict)

    @property
    def firing(self):
        return [alert for alert in self.alerts if alert.firing]

    @property
    def resolved(self):
        return [alert for alert in self.alerts if not alert.firing]

    @property
    def primary(self):
        """The instance to investigate: the first firing one, else the first."""
        firing = self.firing
        return firing[0] if firing else (self.alerts[0] if self.alerts else None)

    @classmethod
    def parse(cls, raw):
        raw = _as_dict(raw)
        listed = raw.get("alerts")
        if not isinstance(listed, list):
            listed = []
        instances = tuple(AlertInstance.parse(entry) for entry in listed)
        return cls(
            receiver=_as_text(raw.get("receiver")) or "",
            status=_as_text(raw.get("status")) or _status_of(instances),
            state=_as_text(raw.get("state")) or "",
            title=_as_text(raw.get("title")) or "",
            external_url=_as_text(raw.get("externalURL")) or "",
            version=_as_text(raw.get("version")) or "",
            group_key=_as_text(raw.get("groupKey")) or "",
            truncated_alerts=_as_number(raw.get("truncatedAlerts")) or 0,
            group_labels=_as_dict(raw.get("groupLabels")),
            common_labels=_as_dict(raw.get("commonLabels")),
            common_annotations=_as_dict(raw.get("commonAnnotations")),
            alerts=instances,
            raw=raw,
        )


def _status_of(instances):
    """The delivery status, derived from the instances when it was not sent.

    Grafana always includes the top-level `status`, so this only ever runs for
    a hand-written notification. One firing instance makes the delivery a
    firing one, and nothing firing at all means the rule has resolved.
    """
    if any(alert.firing for alert in instances):
        return "firing"
    return "resolved" if instances else "unknown"


def looks_like_grafana_webhook(body):
    """True when a body carries at least one alert instance.

    Grafana's receiver posts the Alertmanager shape and nothing else in this
    stack does, so a non-empty `alerts` array is enough to tell a real
    notification apart from a health check or a stray probe. The top-level
    `status` is deliberately not required: posting one instance by hand with
    curl is a reasonable thing to want to do, and `AlertPayload.parse` derives
    the status from the instances when it is missing.
    """
    if not isinstance(body, dict):
        return False
    alerts = body.get("alerts")
    return isinstance(alerts, list) and bool(alerts)

"""Settings for the incident responder, every one of them overridable.

Defaults point at the loopback ports `compose.yaml` publishes, so the service
runs on the host next to the stack without any configuration. Inside Compose
the same names resolve to the services instead, so nothing here has to change:

    PROMETHEUS_URL=http://prometheus:9090
    LOKI_URL=http://loki:3100
    TEMPO_URL=http://tempo:3200
"""

import os
from dataclasses import dataclass
from pathlib import Path

# incident-response/incident_response/config.py -> the repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]

TRUTHY = {"1", "true", "yes", "on"}


def _text(name, default):
    value = os.getenv(name)
    return default if value is None or value == "" else value


def _number(name, default, cast):
    """Reads a numeric setting, falling back to the default rather than raising.

    A typo in an environment variable should not stop the service from booting;
    it should just leave the default in place.
    """
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except ValueError:
        return default


def _flag(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in TRUTHY


def _optional(name):
    value = os.getenv(name)
    return None if value is None or value == "" else value


def _path(name, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return Path(raw).expanduser().resolve()


@dataclass(frozen=True)
class Settings:
    # Telemetry backends. Loopback by default, service names inside Compose.
    prometheus_url: str = "http://127.0.0.1:9090"
    loki_url: str = "http://127.0.0.1:3100"
    tempo_url: str = "http://127.0.0.1:3200"
    grafana_url: str = "http://127.0.0.1:3000"

    # The service under observation. Everything it selects on is named here, so
    # this is the one setting to change when pointing at a different app.
    service_name: str = "order-tracker"

    # The counter the alert rule watches. Prometheus sees the app's
    # `order.lookup.requests` instrument under this name, and every point
    # carries the two labels below, which is what makes the failing route
    # recoverable from the metric rather than only from the logs.
    error_metric: str = "order_lookup_requests_total"
    error_status_label: str = "http_response_status_code"
    error_status_regex: str = "5.."

    # How far back the evidence queries look. The alert rule runs on increase()
    # over 5m, so the default lookback is longer than the window it fired on:
    # by the time the webhook arrives the peak may already have rolled off.
    evidence_lookback_seconds: int = 900
    query_window: str = "5m"

    # Caps that keep one incident folder readable rather than exhaustive.
    max_log_lines: int = 200
    max_traces: int = 3

    http_timeout: float = 10.0

    incidents_dir: Path = REPO_ROOT / "incident-response" / "incidents"

    # Where the assistant runs. It has to be the repository, because reading the
    # application source is the whole point of the investigation.
    workspace_dir: Path = REPO_ROOT
    opencode_bin: str = "opencode"
    opencode_agent: str = "incident-investigator"
    opencode_model: str | None = None
    # Only the read-only agent is used, so nothing is written without a human.
    opencode_auto: bool = False
    assistant_timeout: float = 900.0
    # Escape hatch for running the service without spending model tokens.
    assistant_enabled: bool = True
    # One assistant run at a time. A second firing queues behind the first
    # rather than racing it over the same evidence.
    max_parallel_assistant_runs: int = 1

    @classmethod
    def from_env(cls):
        return cls(
            prometheus_url=_text("PROMETHEUS_URL", cls.prometheus_url),
            loki_url=_text("LOKI_URL", cls.loki_url),
            tempo_url=_text("TEMPO_URL", cls.tempo_url),
            grafana_url=_text("GRAFANA_URL", cls.grafana_url),
            service_name=_text("INCIDENT_SERVICE_NAME", cls.service_name),
            error_metric=_text("INCIDENT_ERROR_METRIC", cls.error_metric),
            error_status_label=_text("INCIDENT_STATUS_LABEL", cls.error_status_label),
            error_status_regex=_text(
                "INCIDENT_STATUS_REGEX", cls.error_status_regex
            ),
            evidence_lookback_seconds=_number(
                "INCIDENT_LOOKBACK_SECONDS", cls.evidence_lookback_seconds, int
            ),
            query_window=_text("INCIDENT_QUERY_WINDOW", cls.query_window),
            max_log_lines=_number("INCIDENT_MAX_LOG_LINES", cls.max_log_lines, int),
            max_traces=_number("INCIDENT_MAX_TRACES", cls.max_traces, int),
            http_timeout=_number("INCIDENT_HTTP_TIMEOUT", cls.http_timeout, float),
            incidents_dir=_path("INCIDENT_DIR", cls.incidents_dir),
            workspace_dir=_path("INCIDENT_WORKSPACE", cls.workspace_dir),
            opencode_bin=_text("INCIDENT_OPENCODE_BIN", cls.opencode_bin),
            opencode_agent=_text("INCIDENT_OPENCODE_AGENT", cls.opencode_agent),
            opencode_model=_optional("INCIDENT_OPENCODE_MODEL"),
            opencode_auto=_flag("INCIDENT_OPENCODE_AUTO", cls.opencode_auto),
            assistant_timeout=_number(
                "INCIDENT_ASSISTANT_TIMEOUT", cls.assistant_timeout, float
            ),
            assistant_enabled=_flag(
                "INCIDENT_ASSISTANT_ENABLED", cls.assistant_enabled
            ),
            max_parallel_assistant_runs=_number(
                "INCIDENT_MAX_PARALLEL_ASSISTANT_RUNS",
                cls.max_parallel_assistant_runs,
                int,
            ),
        )


_cached = None


def get_settings():
    """The process-wide settings, read from the environment once."""
    global _cached
    if _cached is None:
        _cached = Settings.from_env()
    return _cached


def set_settings(settings):
    """Replaces the cached settings. Used by the tests."""
    global _cached
    _cached = settings
    return _cached

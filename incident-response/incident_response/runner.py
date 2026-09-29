"""Sequences the stages and tracks where each incident has got to.

`POST /alerts` hands the payload to `submit`, which records the incident and
returns straight away. Grafana's webhook has a short timeout and retries, so
the work cannot happen inside the request: collecting evidence means several
queries across three backends, and the assistant run is measured in minutes.
The stages run on a worker thread instead, and every one of them writes its
output to disk before moving on, so an incident is legible at any point and a
crash part-way through loses nothing.

The status sequence is:

    received -> collecting -> investigating -> ready

`ready` is the handoff: the evidence is on disk and the assistant has answered.
Anything that goes wrong ends in `failed` with the reason in `status.json`,
and a partial investigation is still on disk.
"""

import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from incident_response.alerts import AlertPayload
from incident_response.assistant import HeadlessAssistant, build_prompt
from incident_response.config import Settings, get_settings
from incident_response.evidence import EvidenceCollector, render_brief
from incident_response.store import IncidentStore, make_incident_id

logger = logging.getLogger(__name__)


@dataclass
class Incident:
    """One alert, and its progress through the stages."""

    id: str
    status: str = "received"
    received_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    rule_name: str | None = None
    severity: str | None = None
    fingerprint: str | None = None
    alert_status: str | None = None
    headline: str | None = None
    affected_endpoints: list = field(default_factory=list)
    log_lines: int = 0
    trace_count: int = 0
    warnings: list = field(default_factory=list)
    error: str | None = None
    assistant: dict | None = None
    path: str | None = None
    # Every delivery seen for this incident, oldest first. Grafana posts once
    # when an alert starts firing and again when it stops, and both carry the
    # same fingerprint and `startsAt`, so they resolve to the same id and land
    # in the same directory rather than becoming two incidents.
    notifications: list = field(default_factory=list)

    def to_dict(self):
        payload = asdict(self)
        for key in ("received_at", "updated_at", "finished_at"):
            value = payload.get(key)
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        return payload

    def touch(self, status=None, **fields):
        if status is not None:
            self.status = status
        for key, value in fields.items():
            setattr(self, key, value)
        self.updated_at = datetime.now(timezone.utc)
        return self


class IncidentRunner:
    """Owns the incident registry and the thread each investigation runs on."""

    def __init__(self, settings=None, store=None, collector=None, assistant=None):
        self.settings = settings or get_settings()
        self.store = store or IncidentStore(self.settings.incidents_dir)
        self._owns_collector = collector is None
        self.collector = collector or EvidenceCollector(self.settings)
        self.assistant = assistant or HeadlessAssistant(self.settings)
        self._incidents = {}
        self._threads = {}
        self._lock = threading.Lock()
        # One assistant run at a time by default. A second firing still gets its
        # evidence collected immediately, it just waits its turn to spend tokens.
        self._assistant_slots = threading.BoundedSemaphore(
            max(1, self.settings.max_parallel_assistant_runs)
        )

    def close(self):
        if self._owns_collector:
            self.collector.close()

    # Registry -------------------------------------------------------------

    def incidents(self):
        with self._lock:
            return list(self._incidents.values())

    def get(self, incident_id):
        with self._lock:
            return self._incidents.get(incident_id)

    def wait(self, incident_id, timeout=None):
        """Blocks until the investigation for an incident has finished.

        The stages run on a worker thread, so a caller that needs the result
        rather than the acceptance needs a way to wait for one. The tests use
        it; so would any future synchronous caller, such as a replay command.
        """
        with self._lock:
            thread = self._threads.get(incident_id)
        if thread is not None:
            thread.join(timeout)
        return self.get(incident_id)

    # Submission -----------------------------------------------------------

    def submit(self, payload, background=True):
        """Records a delivery and starts the work.

        Returns as soon as the incident is registered. `background=False` runs
        the stages inline, which is what the tests use so that an assertion can
        be made against a finished incident.

        Grafana posts once when an alert fires and again when it resolves, both
        carrying the same fingerprint and `startsAt`. Those are one incident,
        not two, so a repeat delivery is appended to the existing record and
        leaves the investigation already on disk alone.
        """
        alert = payload.primary
        received_at = datetime.now(timezone.utc)
        incident_id = make_incident_id(alert, received_at)
        paths = self.store.create(incident_id)

        with self._lock:
            incident = self._incidents.get(incident_id)
            if incident is not None:
                incident.notifications.append(
                    {
                        "received_at": received_at.isoformat(),
                        "status": alert.status if alert else payload.status,
                    }
                )
                incident.updated_at = received_at
                self._incidents[incident_id] = incident
                duplicate = True
            else:
                incident = Incident(
                    id=incident_id,
                    received_at=received_at,
                    rule_name=alert.rule_name if alert else None,
                    severity=alert.severity if alert else None,
                    fingerprint=alert.fingerprint if alert else None,
                    alert_status=alert.status if alert else None,
                    path=str(paths.root),
                    notifications=[
                        {
                            "received_at": received_at.isoformat(),
                            "status": alert.status if alert else payload.status,
                        }
                    ],
                )
                self._incidents[incident_id] = incident
                duplicate = False

        if not duplicate:
            self.store.write_json(
                paths,
                "alert.json",
                {
                    "received_at": received_at.isoformat(),
                    "parsed": asdict_alert_payload(payload),
                    "raw": payload.raw,
                },
            )
        self._persist(incident)

        if duplicate:
            # The investigation is already done or already running; a resolved
            # notification does not restart it. If the previous delivery is
            # still collecting, the thread already in flight will pick up the
            # files as they land.
            self._persist(incident)
            return incident

        if background:
            thread = threading.Thread(
                target=self._process,
                args=(incident, payload),
                name=f"incident-{incident_id}",
                daemon=True,
            )
            with self._lock:
                self._threads[incident_id] = thread
            thread.start()
        else:
            self._process(incident, payload)
        return incident

    # Stages ---------------------------------------------------------------

    def _process(self, incident, payload):
        try:
            if not payload.firing:
                # A notification with nothing firing is Grafana reporting that
                # the alert resolved. There is no incident to investigate, but
                # the delivery is still recorded, because "it cleared and
                # nobody noticed" is a question this service gets asked.
                incident.touch(
                    "ready",
                    headline="Notification carried no firing alert instance.",
                    assistant={
                        "skipped": True,
                        "reason": (
                            "No firing alert instance: Grafana is reporting the "
                            "alert as resolved."
                        ),
                    },
                    error=None,
                )
            else:
                evidence = self._collect(incident, payload)
                if evidence is not None:
                    self._investigate(incident, evidence)
        except Exception as exc:  # noqa: BLE001 - the thread must always land
            logger.exception("incident %s failed", incident.id)
            incident.touch("failed", error=f"{type(exc).__name__}: {exc}")
        if incident.finished_at is None:
            incident.finished_at = datetime.now(timezone.utc)
        self._persist(incident)

    def _collect(self, incident, payload):
        incident.touch("collecting")
        self._persist(incident)
        paths = self.store.paths(incident.id)

        evidence = self.collector.collect(payload)
        self.store.write_json(paths, "evidence.json", evidence.to_dict())
        self.store.write_text(paths.brief, render_brief(evidence, incident.id))
        for trace in evidence.traces:
            self.store.write_json(
                paths,
                f"traces/{trace.trace_id}.json",
                {
                    "trace_id": trace.trace_id,
                    "resource": trace.resource,
                    "spans": [asdict(span) for span in trace.spans],
                },
            )

        incident.touch(
            headline=evidence.headline(),
            affected_endpoints=[
                {
                    "route": route.route,
                    "method": route.method,
                    "errors": route.errors,
                    "total": route.total,
                    "error_ratio": route.error_ratio,
                }
                for route in evidence.affected
            ],
            log_lines=len(evidence.error_lines),
            trace_count=len(evidence.traces),
            warnings=list(evidence.warnings),
        )
        self._persist(incident)
        return evidence

    def _investigate(self, incident, evidence):
        paths = self.store.paths(incident.id)
        if not self.settings.assistant_enabled:
            incident.touch(
                "ready",
                assistant={
                    "skipped": True,
                    "reason": "INCIDENT_ASSISTANT_ENABLED is off",
                },
            )
            return

        prompt = build_prompt(evidence, incident.id, paths, self.settings)
        self.store.write_text(paths.prompt, prompt + "\n")

        incident.touch("investigating")
        self._persist(incident)

        with self._assistant_slots:
            result = self.assistant.run(prompt, incident.id, paths)

        incident.touch(assistant=result.to_dict())
        if result.ok:
            incident.touch("ready", error=None)
        else:
            # The evidence is the deliverable and it is already on disk, so a
            # failed assistant run leaves the incident readable and reports the
            # problem as a warning rather than losing the investigation. Marking
            # it "failed" would hide working evidence behind a broken optional
            # step, and `assistant.ok` still says plainly that no answer came.
            incident.warnings.append(
                f"The assistant did not return an answer: {result.error} "
                f"The evidence is complete; re-run the assistant to retry."
            )
            incident.touch("ready")
        incident.finished_at = datetime.now(timezone.utc)
        self._persist(incident)

    # Persistence ----------------------------------------------------------

    def _persist(self, incident):
        paths = self.store.paths(incident.id)
        try:
            paths.root.mkdir(parents=True, exist_ok=True)
            self.store.write_json(paths, "status.json", incident.to_dict())
        except OSError:
            # A status file that cannot be written must not take down the run
            # that produced it; the incident is still in the registry.
            logger.exception("could not write status for %s", incident.id)


def asdict_alert_payload(payload):
    """The parsed payload, with its timestamps as strings for JSON."""
    if isinstance(payload, AlertPayload):
        return {
            "receiver": payload.receiver,
            "status": payload.status,
            "state": payload.state,
            "title": payload.title,
            "external_url": payload.external_url,
            "version": payload.version,
            "group_key": payload.group_key,
            "truncated_alerts": payload.truncated_alerts,
            "group_labels": payload.group_labels,
            "common_labels": payload.common_labels,
            "common_annotations": payload.common_annotations,
            "alerts": [asdict(alert) for alert in payload.alerts],
        }
    return {"raw": str(payload)}

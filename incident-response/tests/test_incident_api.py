"""The HTTP surface, and the stages running behind it."""

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from incident_response.alerts import AlertPayload
from incident_response.assistant import AssistantResult
from incident_response.evidence import EvidenceCollector
from incident_response.main import set_runner
from incident_response.runner import IncidentRunner
from incident_response.store import IncidentStore

from conftest import TRACE_ID, grafana_webhook


class RecordingAssistant:
    """Stands in for the headless run, so no test spends model tokens.

    Returns a real `AssistantResult` rather than a lookalike, because the
    runner reads the same fields off it that it reads off a live run.
    """

    def __init__(self, exit_code=0, timed_out=False, error=None):
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.error = error
        self.calls = []

    def run(self, prompt, incident_id, paths):
        self.calls.append({"prompt": prompt, "incident_id": incident_id})
        output = "## Verdict\n\nROOT CAUSE FOUND - app/main.py:77\n"
        paths.assistant_log.write_text(output, encoding="utf-8")
        paths.assistant_result.write_text(
            f"# Assistant result for {incident_id}\n\n{output}", encoding="utf-8"
        )
        paths.prompt.write_text(prompt, encoding="utf-8")
        now = datetime.now(timezone.utc)
        return AssistantResult(
            started_at=now,
            finished_at=now,
            command=["opencode", "run", "..."],
            exit_code=self.exit_code,
            timed_out=self.timed_out,
            duration_seconds=1.0,
            output=output,
            error=self.error,
        )


@pytest.fixture
def runner(settings, client):
    return IncidentRunner(
        settings=settings,
        store=IncidentStore(settings.incidents_dir),
        collector=EvidenceCollector(settings, client=client),
        assistant=RecordingAssistant(),
    )


@pytest.fixture
def api(runner):
    set_runner(runner)
    from incident_response.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def post_alert(api, runner):
    """Posts a notification and waits for the investigation behind it."""

    def send(payload=None, timeout=30):
        body = api.post("/alerts", json=payload or grafana_webhook()).json()
        runner.wait(body["incident_id"], timeout)
        return body, runner.get(body["incident_id"])

    return send


# POST /alerts ------------------------------------------------------------


def test_accepts_a_firing_alert_and_answers_before_the_work_is_done(api):
    response = api.post("/alerts", json=grafana_webhook())

    assert response.status_code == 202
    body = response.json()
    assert body["investigating"] is True
    assert body["status"] == "accepted"
    assert body["status_url"] == f"/incidents/{body['incident_id']}"
    assert body["path"].endswith(body["incident_id"])


def test_collects_the_evidence_and_starts_the_assist_on_behalf_of_the_alert(
    post_alert, runner
):
    body, incident = post_alert()

    assert incident.status == "ready"
    assert incident.headline == "POST /api/orders 1 5xx in 5m"
    assert incident.log_lines == 1
    assert incident.trace_count == 1

    paths = runner.store.paths(body["incident_id"])
    assert paths.brief.read_text().startswith("# Incident")
    assert json.loads(paths.evidence.read_text())["count_source"] == "increase"
    assert (paths.traces / f"{TRACE_ID}.json").is_file()
    assert json.loads(paths.status.read_text())["status"] == "ready"
    assert json.loads(paths.alert.read_text())["raw"]["receiver"] == "incident-responder"

    # The assistant really was started, unattended, with a prompt that points
    # at the evidence on disk.
    assert len(runner.assistant.calls) == 1
    call = runner.assistant.calls[0]
    assert call["incident_id"] == body["incident_id"]
    assert "POST /api/orders" in call["prompt"]
    assert str(paths.brief) in call["prompt"]


def test_the_evidence_survives_an_assistant_that_returns_nothing(post_alert, runner):
    # This is the shape of a machine with no provider configured: evidence
    # collection works, the headless run does not. The incident must still be
    # readable, because that is the part a responder needs.
    runner.assistant = RecordingAssistant(exit_code=1, error="no provider configured")

    body, incident = post_alert()

    assert incident.status == "ready"
    assert incident.assistant["ok"] is False
    assert any("assistant did not return" in w for w in incident.warnings)
    paths = runner.store.paths(body["incident_id"])
    assert paths.brief.is_file()
    assert paths.assistant_result.is_file()


def test_a_resolved_notification_is_recorded_without_investigating(
    post_alert, runner
):
    body, incident = post_alert(grafana_webhook("resolved"))

    assert body["investigating"] is False
    assert "resolved" in body["reason"]
    assert incident.status == "ready"
    assert incident.assistant["skipped"] is True
    assert runner.assistant.calls == []


def test_a_resolve_joins_its_own_incident_rather_than_starting_another(
    post_alert, runner
):
    """Grafana posts once on firing and again on resolve, with the same
    fingerprint and `startsAt`. That is one incident, not two."""
    firing, _ = post_alert(grafana_webhook("firing"))
    resolved, incident = post_alert(grafana_webhook("resolved"))

    assert resolved["incident_id"] == firing["incident_id"]
    assert len(incident.notifications) == 2
    assert [note["status"] for note in incident.notifications] == [
        "firing",
        "resolved",
    ]
    # The investigation from the firing notification is left intact.
    assert runner.assistant.calls and len(runner.assistant.calls) == 1
    assert incident.trace_count == 1


def test_rejects_a_body_that_is_not_a_grafana_webhook(api):
    assert api.post("/alerts", json={"status": "firing"}).status_code == 400


def test_rejects_a_body_that_is_not_json(api):
    assert api.post("/alerts", content="<html/>").status_code == 400


def test_survives_a_backend_being_down(post_alert, backends):
    backends.failing = {"prometheus", "loki", "tempo"}

    body, incident = post_alert()

    # Evidence collection failing is not the notification failing.
    assert body["status"] == "accepted"
    assert incident.status == "ready"
    assert incident.warnings


# Inspection --------------------------------------------------------------


def test_health_reports_each_backend(api):
    body = api.get("/healthz").json()

    assert body["status"] == "ok"
    assert body["backends"] == {"prometheus": "ok", "loki": "ok", "tempo": "ok"}
    assert body["degraded"] == []
    assert body["assistant"]["agent"] == "incident-investigator"


def test_health_flags_a_degraded_backend(api, backends):
    backends.failing = {"loki"}

    body = api.get("/healthz").json()

    assert body["degraded"] == ["loki"]
    assert "unreachable" in body["backends"]["loki"]


def test_lists_incidents(post_alert, api):
    post_alert()
    post_alert({**grafana_webhook(), "alerts": [
        {**grafana_webhook()["alerts"][0], "fingerprint": "other", "status": "firing"}
    ]})

    listed = api.get("/incidents").json()

    assert len(listed) == 2
    assert {record["status"] for record in listed} == {"ready"}


def test_returns_one_incident_with_its_brief_and_answer(post_alert, api):
    incident_id = post_alert()[0]["incident_id"]

    body = api.get(f"/incidents/{incident_id}").json()

    assert body["id"] == incident_id
    assert body["affected_endpoints"][0]["method"] == "POST"
    assert "day is out of range for month" in body["brief"]
    assert "ROOT CAUSE FOUND" in body["assistant_result"]


def test_serves_the_brief_on_its_own(post_alert, api):
    incident_id = post_alert()[0]["incident_id"]

    response = api.get(f"/incidents/{incident_id}/brief.md")

    assert response.status_code == 200
    assert response.text.startswith("# Incident")


def test_unknown_incident_is_a_404(api):
    assert api.get("/incidents/nope").status_code == 404
    assert api.get("/incidents/nope/brief.md").status_code == 404

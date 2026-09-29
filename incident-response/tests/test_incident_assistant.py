"""The incident directory, and the headless assistant run."""

import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from incident_response.assistant import HeadlessAssistant, build_command, build_prompt
from incident_response.config import Settings
from incident_response.evidence import render_brief
from incident_response.store import IncidentStore, make_incident_id

from conftest import grafana_webhook

from incident_response.alerts import AlertPayload


# Incident ids ------------------------------------------------------------


def test_incident_id_is_sortable_and_carries_the_rule():
    alert = AlertPayload.parse(grafana_webhook()).primary

    incident_id = make_incident_id(alert)

    # Lowercased throughout, because these are directory names and a
    # case-insensitive filesystem would collapse two spellings into one.
    assert incident_id == "20260929t171400z-a1b2c3d4e5f6-5xx-responses-on-the-order-api"


def test_two_re_firings_of_one_rule_get_different_directories():
    alert = AlertPayload.parse(grafana_webhook()).primary

    # A genuine re-fire: same rule, new startsAt.
    refire = replace(alert, started_at=alert.started_at + timedelta(hours=1))

    assert make_incident_id(alert) != make_incident_id(refire)
    # ...while a repeat notification for the *same* firing must land in the
    # directory that already exists, or every reminder would spawn a new one.
    assert make_incident_id(alert) == make_incident_id(alert)


# Store -------------------------------------------------------------------


def test_writes_and_reads_back_an_incident(tmp_path):
    store = IncidentStore(tmp_path)
    paths = store.create("incident-1")

    store.write_json(paths, "alert.json", {"rule": "5xx"})
    store.write_text(paths.brief, "# Brief\n")

    assert json.loads(paths.alert.read_text())["rule"] == "5xx"
    assert store.read_text(paths.brief) == "# Brief\n"
    assert paths.traces.is_dir()


def test_lists_incidents_newest_first(tmp_path):
    store = IncidentStore(tmp_path)
    for incident_id in ("20260929T100000Z-a", "20260929T120000Z-b", "20260929T110000Z-c"):
        paths = store.create(incident_id)
        store.write_json(paths, "status.json", {"id": incident_id, "status": "ready"})

    assert [record["id"] for record in store.list_incidents()] == [
        "20260929T120000Z-b",
        "20260929T110000Z-c",
        "20260929T100000Z-a",
    ]


def test_an_incident_directory_without_a_status_is_still_listed(tmp_path):
    store = IncidentStore(tmp_path)
    store.create("half-written")

    assert store.list_incidents() == [{"id": "half-written", "status": "unreadable"}]


def test_creates_the_directory_tree_it_needs(tmp_path):
    store = IncidentStore(tmp_path / "deeply" / "nested")
    paths = store.create("incident-1")

    assert paths.brief.parent.is_dir()


# The assistant command ---------------------------------------------------


def test_command_is_read_only_by_default(settings):
    command = build_command(settings, "go and look", "id")

    assert command[:2] == ["opencode", "run"]
    assert "--agent" in command
    assert command[command.index("--agent") + 1] == "incident-investigator"
    assert "--dir" in command
    # An unattended run must not be able to change the code it is diagnosing.
    assert "--auto" not in command


def test_auto_is_only_passed_when_asked_for(tmp_path):
    settings = Settings(
        workspace_dir=tmp_path, opencode_auto=True, assistant_timeout=30
    )

    assert "--auto" in build_command(settings, "prompt", "id")


def test_model_is_only_passed_when_configured(settings, tmp_path):
    assert "--model" not in build_command(settings, "prompt", "id")

    with_model = Settings(workspace_dir=tmp_path, opencode_model="anthropic/sonnet")

    command = build_command(with_model, "prompt", "id")
    assert command[command.index("--model") + 1] == "anthropic/sonnet"


def test_the_prompt_is_an_argv_entry_not_a_shell_string(settings):
    """A prompt with a semicolon in it must not reach a shell."""
    command = build_command(settings, "look at this; rm -rf /", "id")

    assert command[-1] == "look at this; rm -rf /"


def test_prompt_points_at_the_evidence_rather_than_pasting_it(
    settings, collector, firing_payload, tmp_path
):
    evidence = collector.collect(firing_payload)
    store = IncidentStore(tmp_path / "incidents")
    paths = store.create("incident-1")
    store.write_text(paths.brief, render_brief(evidence, "incident-1"))

    prompt = build_prompt(evidence, "incident-1", paths, settings)

    assert str(paths.brief) in prompt
    assert str(paths.evidence) in prompt
    assert str(settings.workspace_dir) in prompt
    assert "POST /api/orders" in prompt
    assert "day is out of range for month" in prompt
    assert "## Verdict" in prompt
    # The agent applies the fix, but is told twice that it must not claim to
    # have verified anything, because it has no shell to run the tests with.
    assert "**Apply the fix**" in prompt
    assert "do not commit" in prompt


def test_prompt_says_so_when_the_evidence_is_thin(settings, collector, tmp_path):
    from incident_response.backends import TelemetryClient
    from incident_response.evidence import EvidenceCollector
    import httpx

    from conftest import FakeBackends

    backends = FakeBackends(error_samples=[], total_samples=[], logs={"status": "success", "data": {"result": []}}, traces={})
    client = TelemetryClient(
        settings, client=httpx.Client(transport=httpx.MockTransport(backends))
    )
    evidence = EvidenceCollector(settings, client=client).collect(
        AlertPayload.parse(grafana_webhook())
    )
    paths = IncidentStore(tmp_path / "incidents").create("incident-1")

    prompt = build_prompt(evidence, "incident-1", paths, settings)

    assert "no failing route identified" in prompt
    assert "Collection warnings" in prompt


# The assistant run -------------------------------------------------------


def test_records_the_run_and_saves_the_answer(settings, tmp_path, fake_opencode):
    store = IncidentStore(tmp_path / "incidents")
    paths = store.create("incident-1")
    fake = fake_opencode(
        'print("## Verdict")\n'
        'print("ROOT CAUSE FOUND - app/main.py:77")\n'
        'print("some debug noise", file=sys.stderr)\n'
    )

    result = HeadlessAssistant(
        Settings(workspace_dir=tmp_path, opencode_bin=str(fake), assistant_timeout=30)
    ).run("investigate", "incident-1", paths)

    assert result.exit_code == 0
    assert result.ok is True
    assert "ROOT CAUSE FOUND" in result.output
    assert "debug noise" in result.output
    saved = paths.assistant_result.read_text()
    assert "ROOT CAUSE FOUND" in saved
    assert "Exit code: 0" in saved
    # The command is written into the log, so a run can be reproduced by hand.
    assert paths.assistant_log.read_text().startswith("$ ")


def test_a_missing_binary_is_a_failed_run_not_a_crash(settings, tmp_path):
    store = IncidentStore(tmp_path / "incidents")
    paths = store.create("incident-1")

    result = HeadlessAssistant(
        Settings(workspace_dir=tmp_path, opencode_bin="definitely-not-installed")
    ).run("investigate", "incident-1", paths)

    assert result.ok is False
    assert "Could not start" in result.error
    assert result.exit_code is None


def test_a_timeout_is_recorded_rather_than_hanging(settings, tmp_path, fake_opencode):
    store = IncidentStore(tmp_path / "incidents")
    paths = store.create("incident-1")
    slow = fake_opencode("time.sleep(30)\n", name="slow-opencode")

    result = HeadlessAssistant(
        Settings(workspace_dir=tmp_path, opencode_bin=str(slow), assistant_timeout=1)
    ).run("investigate", "incident-1", paths)

    assert result.timed_out is True
    assert result.ok is False
    assert "did not finish" in result.error
    assert "killed after" in paths.assistant_log.read_text()


def test_a_failing_exit_code_is_surfaced(settings, tmp_path, fake_opencode):
    store = IncidentStore(tmp_path / "incidents")
    paths = store.create("incident-1")
    broken = fake_opencode(
        'print("no credentials configured", file=sys.stderr)\nsys.exit(2)\n',
        name="broken-opencode",
    )

    result = HeadlessAssistant(
        Settings(workspace_dir=tmp_path, opencode_bin=str(broken), assistant_timeout=30)
    ).run("investigate", "incident-1", paths)

    assert result.ok is False
    assert result.exit_code == 2
    assert "exited with code 2" in result.error
    assert "no credentials configured" in result.output

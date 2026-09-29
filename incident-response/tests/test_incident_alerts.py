"""The webhook payload Grafana posts, and how it is read."""

from datetime import datetime, timezone

from incident_response.alerts import (
    AlertInstance,
    AlertPayload,
    looks_like_grafana_webhook,
)

from conftest import grafana_webhook


def test_reads_the_rule_identity_off_the_labels():
    alert = AlertPayload.parse(grafana_webhook()).primary

    assert alert.rule_uid == "order-tracker-5xx"
    assert alert.rule_name == "5xx responses on the order API"
    assert alert.folder == "Order Tracker Alerts"
    assert alert.severity == "warning"
    assert alert.service == "order-tracker"
    assert alert.fingerprint == "a1b2c3d4e5f60718"
    assert alert.firing is True


def test_reads_the_annotations_and_the_reduced_value():
    alert = AlertPayload.parse(grafana_webhook(value=7)).primary

    assert alert.value == 7.0
    assert "5xx responses" in alert.summary
    assert alert.panel_url.endswith("?viewPanel=6")
    assert alert.dashboard_url.endswith("/d/order-tracker-requests")


def test_parses_grafanas_nanosecond_timestamps():
    # fromisoformat rejects nine fractional digits before 3.11, and Grafana
    # always writes nine.
    alert = AlertPayload.parse(grafana_webhook()).primary

    assert alert.started_at == datetime(2026, 9, 29, 17, 14, 0, 123456, timezone.utc)


def test_falls_back_to_the_single_value_when_the_refid_is_not_a():
    body = grafana_webhook()
    body["alerts"][0]["values"] = {"Z": 4}

    assert AlertPayload.parse(body).primary.value == 4.0


def test_reads_the_value_a_managed_rule_sends_wrapped():
    # A managed rule reduces to a wrapper rather than a bare number. Both
    # shapes have to work, or the brief reports the threshold comparison as
    # "unknown" against every real delivery of one kind.
    body = grafana_webhook()
    body["alerts"][0]["values"] = {"A": {"type": "reduce", "value": 3}}

    assert AlertPayload.parse(body).primary.value == 3.0


def test_prefers_the_query_value_over_the_threshold_node():
    # `C` is the threshold node and always carries a useless 1, so taking the
    # wrong refId puts a confident, wrong number in front of the responder.
    body = grafana_webhook()
    body["alerts"][0]["values"] = {"C": 1, "A": 1.034561242577023}

    assert AlertPayload.parse(body).primary.value == 1.034561242577023


def test_separates_firing_from_resolved_instances():
    firing = AlertPayload.parse(grafana_webhook("firing"))
    resolved = AlertPayload.parse(grafana_webhook("resolved"))

    assert [alert.status for alert in firing.firing] == ["firing"]
    assert resolved.firing == []
    assert [alert.status for alert in resolved.resolved] == ["resolved"]


def test_picks_a_firing_instance_over_a_resolved_one():
    body = grafana_webhook("resolved")
    body["alerts"].append(
        {**body["alerts"][0], "status": "firing", "fingerprint": "second"}
    )
    payload = AlertPayload.parse(body)

    assert payload.primary.fingerprint == "second"
    assert len(payload.firing) == 1


def test_survives_a_payload_missing_everything_optional():
    """A webhook is an integration boundary; a thin payload must not raise."""
    alert = AlertInstance.parse({"status": "firing"})

    assert alert.firing is True
    assert alert.rule_name is None
    assert alert.summary is None
    assert alert.value is None
    assert alert.started_at is None
    assert alert.labels == {}


def test_survives_a_body_that_is_not_a_payload_at_all():
    payload = AlertPayload.parse("not a dict")

    assert payload.alerts == ()
    assert payload.primary is None
    assert payload.firing == []


def test_ignores_a_value_that_is_not_a_number():
    body = grafana_webhook()
    body["alerts"][0]["values"] = {"A": {"type": "reduce", "value": "many"}}

    assert AlertPayload.parse(body).primary.value is None


def test_recognises_its_own_webhook_and_nothing_else():
    assert looks_like_grafana_webhook(grafana_webhook()) is True
    assert looks_like_grafana_webhook({"status": "firing"}) is False
    assert looks_like_grafana_webhook({"alerts": []}) is False
    assert looks_like_grafana_webhook([]) is False


def test_a_single_hand_written_instance_is_enough():
    # The shape a responder reaches for when poking the endpoint with curl.
    body = {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": "ResponderTest", "test": "true"},
                "annotations": {"summary": "Test notification"},
            }
        ]
    }

    assert looks_like_grafana_webhook(body) is True
    payload = AlertPayload.parse(body)
    assert payload.status == "firing"
    assert payload.primary.rule_name == "ResponderTest"

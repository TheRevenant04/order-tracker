"""The incident responder's HTTP surface, on port 8001.

    POST /alerts            Grafana's webhook target
    GET  /healthz           the service, and each backend it depends on
    GET  /incidents         every incident this process has handled
    GET  /incidents/{id}    one incident, with its brief and the answer

`/alerts` answers 202 as soon as the delivery is recorded. Grafana's webhook
receiver has a short timeout and will retry a failure, and the work behind it
takes minutes, so the request cannot wait for it and a slow collection must
never look like a failed notification to Grafana.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from incident_response.alerts import AlertPayload, looks_like_grafana_webhook
from incident_response.config import get_settings
from incident_response.runner import IncidentRunner

logger = logging.getLogger(__name__)

RUNNER = None


def get_runner():
    """The process-wide runner, built on first use.

    Built lazily rather than at import time so that importing this module, which
    the tests do, does not start an HTTP client or touch the incidents
    directory.
    """
    global RUNNER
    if RUNNER is None:
        RUNNER = IncidentRunner(get_settings())
    return RUNNER


def set_runner(runner):
    """Replaces the runner. Used by the tests."""
    global RUNNER
    RUNNER = runner
    return RUNNER


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runner = get_runner()
    logger.info(
        "incident responder ready: incidents=%s prometheus=%s loki=%s tempo=%s "
        "assistant=%s",
        runner.settings.incidents_dir,
        runner.settings.prometheus_url,
        runner.settings.loki_url,
        runner.settings.tempo_url,
        "on" if runner.settings.assistant_enabled else "off",
    )
    yield
    runner.close()


app = FastAPI(
    title="Incident Responder",
    summary="Turns a Grafana alert into saved evidence and a headless "
    "investigation.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.post("/alerts", status_code=202, summary="Grafana webhook target")
async def receive_alert(request: Request):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "Body must be JSON") from None

    if not looks_like_grafana_webhook(body):
        raise HTTPException(
            400,
            "Expected a Grafana webhook payload with `alerts` and `status`.",
        )

    payload = AlertPayload.parse(body)
    incident = get_runner().submit(payload)

    # `status` is "accepted" rather than the pipeline's current state, because
    # the work is already on another thread by the time this is written and any
    # status quoted here would be a race. GET /incidents/{id} is where the
    # progress is read from.
    #
    # A notification with nothing firing is Grafana saying the alert resolved.
    # It is worth a record, because "the alert cleared without anyone noticing"
    # is a question this service is often asked, but there is nothing to
    # investigate.
    return JSONResponse(
        {
            "incident_id": incident.id,
            "status": "accepted",
            "investigating": bool(payload.firing),
            "reason": (
                None
                if payload.firing
                else "No firing alert instances: Grafana is reporting the "
                "alert as resolved."
            ),
            "instances": [alert.status for alert in payload.alerts],
            "notifications": len(incident.notifications),
            "path": incident.path,
            "status_url": f"/incidents/{incident.id}",
        },
        status_code=202,
    )


@app.get("/healthz", summary="Service and backend health")
def health():
    runner = get_runner()
    backends = runner.collector.client.health()
    return {
        "status": "ok",
        "incidents": len(runner.incidents()),
        "assistant": {
            "enabled": runner.settings.assistant_enabled,
            "agent": runner.settings.opencode_agent,
            "model": runner.settings.opencode_model,
            "auto": runner.settings.opencode_auto,
        },
        "backends": backends,
        "degraded": [name for name, state in backends.items() if state != "ok"],
    }


@app.get("/incidents", summary="Every incident this process has handled")
def list_incidents(limit: int = 50):
    runner = get_runner()
    in_memory = {incident.id: incident.to_dict() for incident in runner.incidents()}
    records = []
    seen = set()
    for record in runner.store.list_incidents(limit=limit):
        incident_id = record.get("id")
        # The in-memory copy is newer than the file for anything still running.
        record = in_memory.get(incident_id, record)
        seen.add(incident_id)
        records.append(record)
    for incident_id, record in in_memory.items():
        if incident_id not in seen:
            records.insert(0, record)
    return records[:limit] if limit else records


@app.get("/incidents/{incident_id}", summary="One incident, with its findings")
def get_incident(incident_id: str, include: str = "brief,result"):
    runner = get_runner()
    incident = runner.get(incident_id)
    paths = runner.store.paths(incident_id)
    if incident is None and not paths.root.exists():
        raise HTTPException(404, "No such incident")

    wanted = {part.strip() for part in include.split(",") if part.strip()}
    payload = incident.to_dict() if incident else runner.store.read_status(
        incident_id
    ) or {"id": incident_id, "status": "unknown"}
    payload["path"] = str(paths.root)
    if "brief" in wanted and paths.brief.exists():
        payload["brief"] = runner.store.read_text(paths.brief)
    if "result" in wanted and paths.assistant_result.exists():
        payload["assistant_result"] = runner.store.read_text(paths.assistant_result)
    if "log" in wanted and paths.assistant_log.exists():
        payload["assistant_log"] = runner.store.read_text(paths.assistant_log)
    return payload


@app.get("/incidents/{incident_id}/brief.md", response_class=PlainTextResponse)
def get_brief(incident_id: str):
    paths = get_runner().store.paths(incident_id)
    if not paths.brief.exists():
        raise HTTPException(404, "No brief for that incident")
    return PlainTextResponse(paths.brief.read_text(encoding="utf-8"))


def main():
    """Entry point for `uv run python -m incident_response`.

    The port is fixed at 8001 by default because that is where the Grafana
    contact point points; every other address is configurable so the same image
    works with a different port if the notification policy says so.
    """
    import uvicorn

    logging.basicConfig(
        level=os.getenv("INCIDENT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        app,
        host=os.getenv("INCIDENT_HOST", "0.0.0.0"),
        port=int(os.getenv("INCIDENT_PORT", "8001")),
    )


if __name__ == "__main__":
    main()

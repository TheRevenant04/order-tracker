# Incident responder

A small service that receives Grafana alert webhooks, gathers the evidence for
the incident from Prometheus, Loki and Tempo, writes it to disk, and then starts
a coding agent headlessly to diagnose it.

It exists to answer one question quickly: *the alert says the service is
returning 5xx — what broke, and where?*

It does not page anyone, it does not restart anything, and it does not decide
whether an incident is over.

## Running it

The defaults point at the loopback ports `compose.yaml` publishes, so with the
stack up there is nothing to configure:

```bash
docker compose up -d
uv run uvicorn incident_response.main:app --app-dir incident-response --port 8001
```

Check it is alive and can reach all three backends:

```bash
curl -s http://localhost:8001/healthz
```

`healthz` reports each backend separately, because "the responder is up" and
"the responder can see your telemetry" are different questions and only the
second one is useful during an incident.

## Endpoints

| Method | Path | What it does |
| --- | --- | --- |
| `POST` | `/alerts` | Grafana webhook target. Answers `202` immediately and does the work on a background thread. |
| `GET` | `/healthz` | Service health plus per-backend reachability. |
| `GET` | `/incidents` | Every incident this process has handled, newest first. |
| `GET` | `/incidents/{id}` | One incident: status, findings, warnings, and the assistant's answer. |
| `GET` | `/incidents/{id}/brief.md` | Just the brief, as plain text. |

To poke it by hand, `alerts` is the only field it insists on:

```bash
curl -X POST http://localhost:8001/alerts -H 'Content-Type: application/json' \
  -d '{"alerts":[{"status":"firing","labels":{"alertname":"Test"},"annotations":{"summary":"Test"}}]}'
```

## What one incident produces

Everything lands in `incident-response/incidents/<id>/`, where the id is
`<start time>-<fingerprint>-<rule name>`:

| File | Contents |
| --- | --- |
| `brief.md` | The readable summary. Start here. |
| `evidence.json` | The same content, structured. |
| `alert.json` | The webhook payload Grafana sent, verbatim. |
| `status.json` | Current state of the investigation. |
| `traces/<trace id>.json` | One raw Tempo response per trace. |
| `assistant.prompt.md` | The exact prompt the agent was given. |
| `assistant.log` | The agent's full output, including its tool calls. |
| `assistant.result.md` | The agent's answer, with the run's exit code and duration. |

`brief.md` is the deliverable. It names the failing routes and their error
ratios, quotes the error log lines with their trace ids, and prints every span
of every failing trace — which is what turns "the API is returning 500s" into
"`POST /api/orders` raises `day is out of range for month` at
`app/main.py:77`".

These directories are gitignored. They contain real log lines and trace
payloads.

## How the evidence is correlated

The three backends are queried separately and joined on identifiers the app
already emits, so no single backend has to be trusted alone:

- **Prometheus** gives the routes and error ratios, via
  `sum by (http_route) (increase(order_lookup_requests_total{...}[5m]))`. A
  counter that has only one sample inside the window legitimately returns `0`,
  so the collector falls back to reading the raw counter and says in the brief
  that it did.
- **Loki** gives the error log lines, filtered on the status label rather than
  on severity, because the app logs failures at `INFO`.
- **Tempo** gives the traces. The collector takes `trace_id` off each log line
  and follows it, which is the join: no correlation heuristics needed.

Anything that fails to collect becomes a warning in the brief rather than a
gap in it. A brief that silently omits the traces reads as "there were no
traces", which is a different and much more misleading claim.

## The agent

Runs as `opencode run` against the repository, using the
`incident-investigator` agent in `.opencode/agents/`. It is pointed at the
incident directory and told to read the brief.

Its permissions are the interesting part:

- `edit: allow` — it applies the fix and writes a regression test.
- `bash: deny` — **this is the load-bearing one.** It cannot run `git`, cannot
  run the tests, and cannot reach the network.

So an unattended alert leaves the fix in your working tree, uncommitted, where
`git diff` shows you exactly what changed. Nothing is staged, nothing is
committed, and the agent cannot claim a test passes because it cannot run one —
it is told to give you the command instead.

To make it report without changing anything, set `edit: deny` on the agent.

### A note on models

The default model is whatever `opencode` resolves, which on a machine with no
credentials is the OpenCode free tier. That tier refuses to run outside the
OpenCode app and fails with:

```
Error from provider (Console): OpenCode's free tier can only be used from within OpenCode
```

Point `INCIDENT_OPENCODE_MODEL` at a model your account can actually run
headlessly, or run `opencode auth login` first. A failed run is recorded as a
warning and the incident still reaches `ready`, because the evidence is the
deliverable and the agent is the optional part — but no answer is produced.

## Configuration

Everything is an environment variable with a working default. The only one you
normally need is the model.

| Variable | Default | Purpose |
| --- | --- | --- |
| `INCIDENT_OPENCODE_MODEL` | *(opencode's default)* | Model for the headless run. Set this first. |
| `INCIDENT_ASSISTANT_ENABLED` | `1` | Set to `0` to collect evidence without spending model tokens. |
| `INCIDENT_OPENCODE_AGENT` | `incident-investigator` | Agent to run. |
| `INCIDENT_ASSISTANT_TIMEOUT` | `900` | Seconds before the run is killed. |
| `INCIDENT_MAX_PARALLEL_ASSISTANT_RUNS` | `1` | Concurrent investigations. |
| `INCIDENT_LOOKBACK_SECONDS` | `900` | How far back the evidence queries look. |
| `INCIDENT_QUERY_WINDOW` | `5m` | The `increase()` range. Matches the alert rule. |
| `INCIDENT_MAX_LOG_LINES` | `200` | Cap on collected log lines. |
| `INCIDENT_MAX_TRACES` | `3` | Cap on traces fetched. |
| `INCIDENT_SERVICE_NAME` | `order-tracker` | The service under observation. |
| `INCIDENT_ERROR_METRIC` | `order_lookup_requests_total` | The counter the alert rule watches. |
| `INCIDENT_STATUS_LABEL` | `http_response_status_code` | Label carrying the status code. |
| `INCIDENT_STATUS_REGEX` | `5..` | What counts as an error. |
| `INCIDENT_DIR` | `incident-response/incidents` | Where evidence is written. |
| `INCIDENT_WORKSPACE` | repo root | Working directory for the agent. |
| `PROMETHEUS_URL` | `http://127.0.0.1:9090` | |
| `LOKI_URL` | `http://127.0.0.1:3100` | |
| `TEMPO_URL` | `http://127.0.0.1:3200` | |
| `GRAFANA_URL` | `http://127.0.0.1:3000` | |

The lookback is deliberately longer than the alert rule's 5 minute window. By
the time the webhook arrives and the evidence is gathered, the peak that
triggered the alert may already have rolled out of the window the rule watches.

## Wiring it to Grafana

`grafana/provisioning/alerting/` holds both halves:

- `order-tracker-5xx.yaml` — the rule, in the `Order Tracker Alerts` folder.
- `incident-responder.yaml` — the contact point and the notification policy.

A contact point with no policy is the easiest thing in Grafana to get wrong:
the rule fires, the receiver exists, and nothing is delivered. Provisioning the
policy alongside the contact point is what stops that.

The contact point posts to `http://host.docker.internal:8001/alerts`, which is
Docker Desktop's name for the host. The responder runs on the host rather than
in Compose so that the agent inherits your local `opencode` credentials and
source tree. On Linux, add this to the `grafana` service:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

After changing anything under `grafana/provisioning/`, restart Grafana:

```bash
docker compose restart grafana
```

Provisioning only runs at start-up, so an edit that appears not to work is
usually just a container that has not restarted yet.

## Pointing it at another service

`INCIDENT_SERVICE_NAME` and `INCIDENT_ERROR_METRIC` are the two settings that
have to change, and they are worth changing together: the metric name has to be
the one the target service's counter actually exports, including any namespace
or unit suffix the instrumentation adds.

Then make sure the logs carry a `trace_id` and the counter carries a status
label. Those two labels are the join between the three backends — without them
the collector falls back to counting routes from the metric alone, and it says
so in the brief.

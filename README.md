# Order Tracker

A small order tracking app for the AI Dev Tools Zoomcamp observability homework. It includes a web page, API, tests, and a Docker Compose setup. You add telemetry, alerts, and an incident responder in Homework 4.

The main user flow is creating an order and checking its status. Three sample orders are created on first startup.

## Run it

You need Docker with Compose. To run the tests, you also need Python 3.11+ and `uv`.

```bash
docker compose up --build -d --wait
```

Open <http://127.0.0.1:8000>. The API is at `/api/orders`, and the health check is at `/healthz`. Data is stored in a Docker volume and survives container recreation.

If port 8000 is occupied, set `ORDER_TRACKER_PORT`, for example:

```bash
ORDER_TRACKER_PORT=18080 docker compose up --build -d --wait
```

Run tests with `uv run --frozen pytest -q`. Stop the app with `docker compose down`. Add `-v` only if you also want to delete the order data.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web page |
| GET | `/healthz` | Database health check |
| GET | `/api/orders` | List orders |
| POST | `/api/orders` | Create an order |
| GET | `/api/orders/{id}` | Check an order |
| PATCH | `/api/orders/{id}` | Change an order status |

The app uses SQLite to keep setup small. Run one app container at a time. The course exercise is about detecting and handling an incident, not scaling the database.

## Observability

Order lookups emit OpenTelemetry traces, metrics and logs. `docker compose up` starts a collector and the three backends behind it, so the same instrumentation is queryable in a dashboard.

| Signal | What you get |
| --- | --- |
| Traces | One `SERVER` span per request, named after the route template, with an `order.lookup` child span for each lookup |
| Metrics | `order.lookup.requests` (counter) and `order.lookup.duration` (histogram, in ms) |
| Logs | One record per lookup, carrying the `trace_id` and `span_id` of its span |

Every metric data point and every log record is tagged with `http.route` and `http.response.status_code`. A lookup that misses is therefore counted separately from one that succeeds, and slicing by route does not blow up cardinality, because the route is the template rather than the concrete path.

The status recorded is the one the surrounding request answers with, so the lookup that `POST /api/orders` performs is counted as `201`, not `200`. An unexpected error inside a lookup is recorded as `500` rather than being counted as a success, which is usually the first thing you want to see during an incident.

## The stack

```bash
docker compose up --build -d --wait
```

| Service | Port | Role |
| --- | --- | --- |
| app | 8000 | The order tracker. Pushes OTLP to the collector |
| otel-collector | | Single fan-out point. Tempo, Loki and the Prometheus exporter all live behind it |
| prometheus | 9090 | Scrapes the collector, and receives Tempo's span metrics |
| loki | 3100 | Log storage, fed over OTLP |
| tempo | 3200 | Trace storage, and the source of the span metrics |
| grafana | 3000 | The dashboard |

Open <http://127.0.0.1:3000> for **Order Tracker - requests and errors**, which is provisioned from `grafana/dashboards/`. Grafana has anonymous admin access, which is only safe because every port is bound to loopback.

The dashboard leads with lookup rate, 5xx rate, error ratio and p95 latency, then breaks requests down by route and response status, and finishes with the matching log lines and the failed traces. The `Route` variable defaults to the API routes, which keeps the container health check out of the way; set it to `All` to include `/healthz`.

The app only knows where the OTLP endpoint is. Adding a fourth backend, or pointing the exporters somewhere other than the collector, is a change to `otel-collector/config.yaml` alone.

### Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OTEL_SERVICE_NAME` | `order-tracker` | `service.name` on every signal |
| `OTEL_METRIC_EXPORT_INTERVAL` | `15000` | Metric export interval, in milliseconds |
| `OTEL_LOG_LEVEL` | `INFO` | Root log level, which also covers uvicorn's own logs |
| `ORDER_TRACKER_PORT` | `8000` | Host port for the app |
| `GRAFANA_PORT` | `3000` | Host port for Grafana |
| `PROMETHEUS_PORT` | `9090` | Host port for Prometheus |
| `LOKI_PORT` | `3100` | Host port for Loki |
| `TEMPO_PORT` | `3200` | Host port for Tempo |

Add these to the `environment` block in `compose.yaml`, since that wins over anything exported in your shell. Pending metrics are flushed on shutdown, so the last batch is not lost when the container stops.

### Without the stack

`app/telemetry.py` exports over OTLP when `OTEL_EXPORTER_OTLP_ENDPOINT` is set and writes to stdout when it is not, so `uv run uvicorn app.main:app` still gives you all three signals in one terminal with no collector to run. Note that `docker compose logs app` only shows uvicorn's own output once the endpoint is set, because the telemetry has gone to the collector instead.

In console mode uvicorn's access log is captured alongside the telemetry, so each request produces two lines and only one of them has a trace id. `--no-access-log` on the uvicorn command in the `Dockerfile` turns it off.

### Alerting

`grafana/provisioning/alerting/order-tracker-5xx.yaml` provisions one rule, **5xx responses on the order API**, in the **Order Tracker Alerts** folder. It reads the app's own counter, the same one the dashboard's error panels use:

```promql
sum(increase(order_lookup_requests_total{
  service_name="order-tracker",
  http_response_status_code=~"5.."
}[5m])) or vector(0)
```

The window is 5 minutes, the group is evaluated every minute, and the rule needs 2 consecutive evaluations above zero before it fires, which is enough to absorb a single unlucky request. It clears as soon as one window comes back clean, so it does not linger after an incident.

**Periods with no 5xx are the interesting case**, and there are two separate failure modes to avoid:

- The 5xx series may not exist at all yet, for instance on a Prometheus that has just started, or before the app has ever served a 5xx. A bare `sum()` over no matching series returns an *empty* vector, which Grafana would report as **No Data**, leaving the rule stuck in an unknown state rather than a healthy one. `or vector(0)` turns that empty result into a real `0`, so the threshold comparison can actually run. Once the series does exist, `increase()` across a quiet window already returns `0` by itself, so this is belt and braces rather than the only thing holding it together.
- The query still needs a policy for genuinely missing data, so `noDataState: OK` covers the case where Prometheus cannot answer at all. `execErrState` stays at `Error`, because a query that cannot be evaluated is worth knowing about and should not be hidden behind the same setting.

The annotations name the exact query, the window and the sustain period, and link straight to **Server errors by route** on the dashboard via `dashboardUid` and `panelId`. The description also points at the error ratio and error log panels, which slice the same `http_response_status_code` label and are usually the fastest way to tell a single bad order id from a broken route.

**No contact point is provisioned,** so nothing is delivered to a real destination. Grafana still routes the alert to its built-in `grafana-default-email` receiver, which has no SMTP server behind it, so while the rule is firing the Grafana log shows `level=error ... SMTP not configured`. That is the expected consequence of not configuring delivery, not a fault in the rule; the rule's own `health` stays `ok` and `lastError` stays empty. Adding a real destination is a `contactPoints` entry in the same file plus a notification policy, and the alert rule itself needs no change.

Where to look for it, because the rule is easy to mistake for a missing one:

| Page | What you get |
| --- | --- |
| http://127.0.0.1:3000/alerting/list | The rules list, with the live state of every rule |
| http://127.0.0.1:3000/alerting/grafana/order-tracker-5xx/view | This rule directly, with its query and threshold |
| Alerting → Alert rules → **Order Tracker Alerts** | The same rules, reached through the folder tree |
| Alerting → Active alerts | Only instances that are firing **right now**, so this is empty whenever the window is clean |

Two traps in that list. An empty **Active alerts** page is not evidence that the rule is missing, and the same goes for `/api/alertmanager/grafana/api/v2/alerts`, which only lists currently firing instances. To see the rule in every state, use `/api/prometheus/grafana/api/v1/rules`, which reports `inactive`, `pending` and `firing` alike.

The other one is the folder itself. **Grafana stores alert rule folders as ordinary folders,** so the `Order Tracker Alerts` folder declared in the provisioning file also appears in the **Dashboards** section, where it is permanently empty because it holds alert rules rather than dashboards. An empty *Order Tracker Alerts* folder under Dashboards is expected and means nothing is wrong; the rules live under Alerting. The dashboard itself is in the separate **Order Tracker** folder.

## Things that bit me

Worth knowing before changing any of this configuration.

- **A Grafana alert's threshold condition has to reference itself.** In the condition node, `query.params` is `[C]`, the condition's own refId, not `[A]`, the query it is built on. Referring to `A` there is silently accepted and the rule never leaves Normal.
- **`instant: true` and `reduceOptions` are what make the rule value a single number.** A range query hands the threshold a series per step, and `lastNotNull` is what collapses it to the value that gets compared.
- **A healthy alert rule looks like a missing one if you only ever look at the firing list.** Grafana's Active alerts page and `/api/alertmanager/grafana/api/v2/alerts` list currently firing instances and nothing else, so both are empty while the rule sits in `inactive`. `/api/prometheus/grafana/api/v1/rules` is the endpoint that shows every state.
- **An alert rule folder also shows up under Dashboards, and it is always empty there.** Grafana keeps alert rule folders as ordinary folders, so the `folders:` block in the alerting provisioning file creates a folder that the Dashboards UI lists too. Checking for alert rules by browsing the Dashboards folder tree is a dead end; go to http://127.0.0.1:3000/alerting/list instead.

- **Tempo's metrics generator disables itself without a local path.** A `remote_write` block alone is not enough; it also needs `metrics_generator.storage.path`, and the only symptom is a silent absence of span metrics. The `processor.span_metrics.dimensions` list is what adds `http.route` and `http.response.status_code` as labels, because the built-in `status_code` label only splits `UNSET` from `ERROR` and so cannot tell a 404 from a 200. That is why the dashboard's error panels read the app's own counter instead.
- **Loki keeps OTLP log attributes as structured metadata, not labels.** A stream selector like `{http_route="/api/orders"}` matches nothing; the same attribute has to be filtered with a pipeline stage, `{service_name="order-tracker"} | http_route="/api/orders"`. This is also what Grafana's `filterByTraceID` generates, so leave it on. The cardinality is fine: the whole app is one stream with two labels.
- **Loki needs `allow_structured_metadata`,** which is also what makes its `/otlp` endpoint exist.
- **`docker compose logs app` and a trace are two different things.** The log record carries a trace id, and Grafana's derived field turns it into a link, but the correlation only works if something is actually holding the traces.

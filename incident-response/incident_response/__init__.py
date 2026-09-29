"""Incident responder.

Grafana posts an alert to `POST /alerts`. The payload is turned into an
incident folder on disk holding everything needed to understand the problem,
and a headless coding assistant is started to read that evidence and work out
what broke.

The modules are split by the job they do:

| Module | Job |
| --- | --- |
| `config` | Every setting, overridable from the environment |
| `alerts` | Grafana's webhook payload, normalised |
| `backends` | Thin clients for Prometheus, Loki and Tempo |
| `evidence` | Turns a firing alert into endpoints, logs and traces |
| `store` | One directory per incident |
| `assistant` | The headless `opencode run` invocation |
| `runner` | Sequences the stages and tracks incident state |
| `main` | The HTTP surface on port 8001 |
"""

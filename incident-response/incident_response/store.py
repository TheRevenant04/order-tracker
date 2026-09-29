"""One directory per incident, holding everything gathered for it.

The directory is the record of truth rather than the in-memory incident, so an
investigation survives a restart of this service and can be read, diffed or
committed by hand. Its layout:

    incidents/<id>/
      status.json            state machine, timings, and the handoff summary
      alert.json             the webhook payload, verbatim and normalised
      evidence.json          endpoints, log lines and trace spans, structured
      brief.md               the same thing for a person or the assistant
      traces/<trace_id>.json raw Tempo response per trace
      assistant.prompt.md    the exact prompt the assistant was given
      assistant.log          its stdout and stderr, streamed live
      assistant.result.md    its final answer

The id combines the alert's start time with its fingerprint, so a re-firing of
the same rule is a new directory rather than an overwrite, and two different
rules never collide.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

# status.json is rewritten as the incident progresses, so it is always the
# current state rather than a log. The sequence a healthy incident walks is
# exactly the order of the first list.
STATUSES = ("received", "collecting", "investigating", "ready", "failed")


@dataclass
class IncidentPaths:
    """Where everything for one incident lives."""

    root: Path

    @property
    def status(self):
        return self.root / "status.json"

    @property
    def alert(self):
        return self.root / "alert.json"

    @property
    def evidence(self):
        return self.root / "evidence.json"

    @property
    def brief(self):
        return self.root / "brief.md"

    @property
    def traces(self):
        return self.root / "traces"

    @property
    def prompt(self):
        return self.root / "assistant.prompt.md"

    @property
    def assistant_log(self):
        return self.root / "assistant.log"

    @property
    def assistant_result(self):
        return self.root / "assistant.result.md"

    def trace(self, trace_id):
        return self.traces / f"{UNSAFE.sub('_', trace_id)}.json"

    def relative(self, path):
        try:
            return str(Path(path).relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(path)


def make_incident_id(alert, received_at=None):
    """A sortable, collision-resistant id built from the alert itself.

    Lowercased throughout, because a case-insensitive filesystem would treat two
    spellings of one id as the same directory, and the ids are the directory
    names.
    """
    received_at = received_at or datetime.now(timezone.utc)
    started = (alert.started_at if alert else None) or received_at
    stamp = started.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fingerprint = UNSAFE.sub("", alert.fingerprint if alert else "")[:12]
    rule = UNSAFE.sub("-", (alert.rule_name if alert else "") or "alert")[:40]
    rule = rule.strip("-") or "alert"
    return f"{stamp}-{fingerprint or 'nofp'}-{rule}".lower()


class IncidentStore:
    """Reads and writes incident directories."""

    def __init__(self, root):
        self.root = Path(root)

    def paths(self, incident_id):
        return IncidentPaths(self.root / UNSAFE.sub("_", incident_id))

    def create(self, incident_id):
        paths = self.paths(incident_id)
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.traces.mkdir(exist_ok=True)
        return paths

    def write_json(self, paths, name, payload):
        path = paths.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
        )
        return path

    def write_text(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def read_status(self, incident_id):
        path = self.paths(incident_id).status
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def read_text(self, path):
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            return ""

    def list_incidents(self, limit=None):
        """Newest first, by directory name, which sorts chronologically."""
        if not self.root.exists():
            return []
        entries = sorted(
            (path for path in self.root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
        if limit:
            entries = entries[:limit]
        records = []
        for path in entries:
            status_path = path / "status.json"
            try:
                record = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = {"id": path.name, "status": "unreadable"}
            record.setdefault("id", path.name)
            records.append(record)
        return records

"""Starts the coding assistant in headless mode and collects what it says.

`opencode run` is the non-interactive entry point: it takes a prompt, works
through it with tools, prints its answer and exits. There is no TTY, so nothing
waits for a human to answer a question, which is why the agent is a dedicated
read-only one and why no `--auto` is passed by default: an unattended run must
not be able to change the code it is diagnosing.

Output is streamed straight to `assistant.log` as it is produced, so a long
investigation can be watched while it happens, and read back afterwards for
`assistant.result.md`. The prompt itself is saved next to the log, so the
investigation can be reproduced by hand from the incident directory alone.
"""

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Keeps a console window from flashing up on Windows when the service runs as
# a background process rather than in a terminal.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class AssistantError(RuntimeError):
    """The assistant could not be started at all."""


@dataclass
class AssistantResult:
    """The outcome of one headless run."""

    started_at: datetime
    finished_at: datetime
    command: list
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    output: str = ""
    error: str | None = None
    session_id: str | None = None
    files: dict = field(default_factory=dict)

    @property
    def ok(self):
        return self.exit_code == 0 and not self.timed_out and not self.error

    def to_dict(self):
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 2),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "ok": self.ok,
            "command": self.command,
            "error": self.error,
            "files": self.files,
        }


def build_command(settings, prompt, incident_id):
    """The `opencode run` invocation, as an argv list.

    Built as a list rather than a shell string so nothing in the prompt, the
    title or the workspace path can be interpreted by a shell.
    """
    command = [
        settings.opencode_bin,
        "run",
        "--agent",
        settings.opencode_agent,
        "--dir",
        str(settings.workspace_dir),
        "--title",
        f"Incident {incident_id}",
    ]
    if settings.opencode_model:
        command += ["--model", settings.opencode_model]
    if settings.opencode_auto:
        # Only reachable when explicitly enabled. Auto-approving permissions
        # on a machine that is not disposable is not a default worth having.
        command.append("--auto")
    command.append(prompt)
    return command


def build_prompt(evidence, incident_id, paths, settings):
    """The prompt handed to the assistant.

    It points at the saved evidence rather than pasting it, so the assistant can
    read the whole thing itself and the prompt stays readable in the log. The
    three signals are named separately because the useful part of the exercise
    is connecting them back to the code that produced them.
    """
    alert = evidence.alert
    headline = evidence.headline()
    route_hint = (
        ", ".join(f"`{route.label}`" for route in evidence.affected) or "unknown"
    )
    error_text = evidence.traces[0].error_message if evidence.traces else None

    lines = [
        f"A production incident is firing on the `{settings.service_name}` "
        f"service and needs a diagnosis.",
        "",
        "## What the alert said",
        "",
        f"- Rule: {alert.rule_name if alert else 'unknown'}"
        + (f" (`{alert.rule_uid}`)" if alert and alert.rule_uid else ""),
        f"- Summary: {alert.summary if alert and alert.summary else 'none'}",
        f"- Severity: {alert.severity if alert and alert.severity else 'unknown'}",
        f"- Alert started: {alert.started_at.isoformat() if alert and alert.started_at else 'unknown'}",
        f"- Value the rule compared against its threshold: "
        f"{alert.value if alert and alert.value is not None else 'unknown'}",
        "",
        "## What the telemetry shows",
        "",
        f"- Affected endpoint(s): {route_hint}",
        f"- Headline: {headline}",
        f"- Error log lines collected: {len(evidence.error_lines)}",
        f"- Traces collected: {len(evidence.traces)}",
    ]
    if error_text:
        lines.append(f"- Exception recorded on the failing request: `{error_text}`")
    if evidence.warnings:
        lines += [
            "- Collection warnings, so you know what is not here: "
            + "; ".join(evidence.warnings)
        ]

    lines += [
        "",
        "## Evidence on disk",
        "",
        f"The collected evidence is already written for you, under "
        f"`{paths.root}`:",
        "",
        f"- `{paths.brief}` - the readable summary: affected endpoints, the "
        f"error log lines, and every span of every failing trace with its "
        f"status and duration. **Start here.**",
        f"- `{paths.evidence}` - the same content as JSON, if you would rather "
        f"read it structured.",
    ]
    if evidence.traces:
        lines.append(
            f"- `{paths.traces}` - one raw Tempo response per trace, including "
            f"any span attributes the brief summarises away."
        )
    lines += [
        f"- `{paths.alert}` - the webhook payload Grafana sent, verbatim.",
        "",
        f"The application source is in this repository, which is your working "
        f"directory (`{settings.workspace_dir}`).",
        "",
        "## What I need from you",
        "",
        "1. Read the brief, then find the code that produced these signals. The "
        "route in the failing spans, the `http.route` attribute and the status "
        "code recorded on the log line all narrow it down; the exception on the "
        "root span is usually the whole answer.",
        "2. Explain the failure, citing the exact file and line. Where the "
        "evidence contradicts the obvious reading of the metric, say so, "
        "because that is the interesting part.",
        "3. Say whether this is a genuine bug, an environment problem or a "
        "misleading alert, and what would distinguish the two.",
        "4. **Apply the fix** to the source in this repository, and add the "
        "regression test that would have caught it. A fix without a test is "
        "half a fix, and an unattended incident that leaves the same fault in "
        "place is worse than no investigation at all.",
        "",
        "You may edit files. You may not run commands, so leave the changes in "
        "the working tree: do not commit, do not stage, and do not try to verify "
        "by running the tests. Say plainly in your answer which files you "
        "changed and which command a human should run to check them.",
        "",
        "End your answer with a section titled `## Verdict` containing one of "
        "`ROOT CAUSE FOUND`, `NEEDS MORE DATA` or `NOT A CODE FAULT`, and a "
        "single sentence saying what a responder should do first.",
    ]
    return "\n".join(lines)


def _resolve_session_id(output):
    """Picks the session id out of the assistant's output, if it printed one.

    Having it means the run can be reopened with `opencode session <id>` and
    the investigation continued by hand.
    """
    for pattern in (r"session[ _]id[:= ]+([0-9a-f]{8,})", r"\bses_([0-9a-zA-Z]+)"):
        found = re.search(pattern, output, re.IGNORECASE)
        if found:
            return found.group(1)
    return None


class HeadlessAssistant:
    """Runs `opencode run` once and returns what it produced."""

    def __init__(self, settings):
        self.settings = settings

    def run(self, prompt, incident_id, paths, timeout=None):
        timeout = timeout if timeout is not None else self.settings.assistant_timeout
        command = build_command(self.settings, prompt, incident_id)
        started = datetime.now(timezone.utc)
        log_path = paths.assistant_log
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"$ {' '.join(command)}\n\n")
            log.flush()
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.settings.workspace_dir),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    # The prompt goes in on stdin, so the process never inherits
                    # a console that could make it wait for a keypress.
                    stdin=subprocess.DEVNULL,
                    text=True,
                    creationflags=CREATE_NO_WINDOW,
                )
            except OSError as exc:
                return AssistantResult(
                    started_at=started,
                    finished_at=datetime.now(timezone.utc),
                    command=command,
                    exit_code=None,
                    timed_out=False,
                    duration_seconds=0.0,
                    error=f"Could not start {self.settings.opencode_bin!r}: {exc}",
                )
            try:
                exit_code = process.wait(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                process.kill()
                # Give the killed process a moment to release the log handle
                # before it is read back.
                try:
                    exit_code = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    exit_code = None
                timed_out = True
                log.write(
                    f"\n[incident-response] killed after {timeout:g}s timeout\n"
                )
                log.flush()

        finished = datetime.now(timezone.utc)
        output = log_path.read_text(encoding="utf-8", errors="replace")
        result = AssistantResult(
            started_at=started,
            finished_at=finished,
            command=command,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_seconds=(finished - started).total_seconds(),
            output=output,
            session_id=_resolve_session_id(output),
        )
        if timed_out:
            result.error = (
                f"The assistant did not finish within {timeout:g}s and was "
                f"stopped. Partial output is in {log_path.name}."
            )
        elif exit_code != 0:
            result.error = (
                f"The assistant exited with code {exit_code}. See {log_path.name}."
            )
        self._save_result(result, paths)
        return result

    def _save_result(self, result, paths):
        header = [
            f"# Assistant result for {paths.root.name}",
            "",
            f"- Started: {result.started_at.isoformat()}",
            f"- Finished: {result.finished_at.isoformat()}",
            f"- Duration: {result.duration_seconds:.1f}s",
            f"- Exit code: {result.exit_code}",
            f"- Timed out: {result.timed_out}",
        ]
        if result.session_id:
            header.append(
                f"- Session: `opencode session {result.session_id}` continues this run"
            )
        if result.error:
            header.append(f"- Problem: {result.error}")
        header += ["", "---", ""]
        paths.assistant_result.write_text(
            "\n".join(header) + result.output.rstrip() + "\n", encoding="utf-8"
        )
        result.files = {
            "log": paths.relative(paths.assistant_log),
            "result": paths.relative(paths.assistant_result),
            "prompt": paths.relative(paths.prompt),
        }

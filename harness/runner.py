"""Headless runner: invoke kiro-cli (v3 engine) for one (task, condition, rep) in isolation."""
from __future__ import annotations

import functools
import glob
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .config import Condition, Task
from .parse import parse_run

KIRO_BIN = os.environ.get("KIRO_LIFT_BIN", "kiro-cli")
# Where kiro-cli v3 keeps per-session logs (<root>/<cwd-hash>/<sessionId>/messages.jsonl).
SESSIONS_DIR = Path(os.environ.get("KIRO_LIFT_SESSIONS_DIR", Path.home() / ".kiro" / "sessions"))

# Workspace-scoped CLI settings written into every run workspace. Custom agents
# inherit the user's global steering/skills/AGENTS.md by default; that would leak
# the operator's own context into every condition, so turn it off. Conditions
# declare what they ship explicitly (e.g. lift-skill's skill:// resource).
_RUN_SETTINGS = {"chat.disableInheritingDefaultResources": True}

# Signatures of transient backend/infra failures (not task failures). These are
# CLI-level error banners that do not appear in normal answer text, so matching
# them will not false-positive on a legitimate answer that happens to discuss
# AWS error types (e.g. "ThrottlingException", "503"). Runs that hit these are
# retried; if still failing they are flagged invalid and excluded from scoring.
_TRANSIENT_SIGNATURES = (
    "error sending request for url",
    "Backtrace omitted",
    "having trouble responding",
    "dispatch failure",
    "Failed to send the request",
)


def _is_transient(raw: str, status: str | None, answer: str) -> bool:
    if any(sig in raw for sig in _TRANSIENT_SIGNATURES):
        return True
    # v3 reports a non-success run status; with no answer at all that is an
    # infrastructure failure, not a model failure.
    return status is not None and status != "success" and not answer


@functools.lru_cache(maxsize=1)
def kiro_version() -> str:
    try:
        out = subprocess.run([KIRO_BIN, "--version"], capture_output=True, text=True, timeout=20)
        return (out.stdout or out.stderr).strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    # Empty pager vars (as set by some IDE shells) crash `aws <svc> help`.
    for k in ("PAGER", "AWS_PAGER", "MANPAGER"):
        if k in env and env[k] == "":
            del env[k]
    return env


def write_run_settings(workspace: Path) -> None:
    """Merge the harness's workspace CLI settings into <workspace>/.kiro/settings/cli.json."""
    settings_path = workspace / ".kiro" / "settings" / "cli.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, Any] = {}
    if settings_path.exists():
        try:
            current = json.loads(settings_path.read_text() or "{}")
        except json.JSONDecodeError:
            current = {}
    current.update(_RUN_SETTINGS)
    settings_path.write_text(json.dumps(current, indent=2))


def read_session_log(session_id: str | None) -> str | None:
    """Return the messages.jsonl text for a v3 session id, if it can be found."""
    if not session_id:
        return None
    matches = glob.glob(str(SESSIONS_DIR / "*" / session_id / "messages.jsonl"))
    if not matches:
        return None
    try:
        return Path(matches[0]).read_text()
    except OSError:
        return None


def base_cmd(agent: str, model: str) -> list[str]:
    """The kiro-cli invocation shared by runs and judges (v3 engine, machine-readable output)."""
    return [
        KIRO_BIN, "chat",
        "--no-interactive",
        "--agent-engine", "v3",
        "--trust-workspace",
        "--output-format", "stream-json",
        "--agent", agent,
        "--model", model,
    ]


@dataclass
class RunResult:
    task_id: str
    stratum: str
    condition: str
    role: str
    augmentation: str
    model: str
    rep: int
    returncode: int
    wall_s: float
    credits: float | None
    cli_time_s: float | None
    tool_calls: list[str]
    tool_call_count: int
    mcp_servers: list[str]
    used_expected_tool: bool | None
    answer: str
    transcript: str
    errors: list[str]
    mcp_startup_failed: bool
    transient_error: bool = False
    session_id: str | None = None
    run_status: str | None = None
    kiro_version: str = ""
    cmd: list[str] = field(default_factory=list)
    raw_excerpt: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _has_mcp(cond: Condition) -> bool:
    return bool(cond.agent_config.get("mcpServers"))


def run_one(
    task: Task,
    cond: Condition,
    model: str,
    rep: int,
    *,
    timeout_s: int = 300,
    require_mcp: bool = True,
    keep_workspace: bool = False,
) -> RunResult:
    """Run a single headless invocation and return parsed telemetry."""
    workspace = Path(tempfile.mkdtemp(prefix=f"kirolift_{task.id}_{cond.name}_r{rep}_"))
    try:
        # Copy any workspace files the condition ships (skills, steering, etc.)
        # into the run dir FIRST, then write the managed agent config so it wins.
        if cond.workspace_dir is not None:
            shutil.copytree(cond.workspace_dir, workspace, dirs_exist_ok=True)
        agents_dir = workspace / ".kiro" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / f"{cond.name}.json").write_text(cond.agent_json())
        write_run_settings(workspace)

        cmd = base_cmd(cond.name, model) + ["--trust-all-tools"]
        if require_mcp and _has_mcp(cond):
            cmd.append("--require-mcp-startup")
        cmd.append(task.prompt)

        max_attempts = 3
        transient = False
        parsed = None
        for attempt in range(1, max_attempts + 1):
            start = time.monotonic()
            stdout = stderr = ""
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    env=_clean_env(),
                )
                wall = time.monotonic() - start
                stdout, stderr = proc.stdout or "", proc.stderr or ""
                returncode = proc.returncode
            except subprocess.TimeoutExpired as e:
                wall = time.monotonic() - start
                stdout = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode(errors="replace")
                stderr = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode(errors="replace")
                stderr += "\n[KIRO-LIFT] TIMEOUT"
                returncode = -1
            parsed = parse_run(stdout, stderr)
            transient = _is_transient(stdout + stderr, parsed.status, parsed.answer)
            # Retry only on transient backend errors that produced no usable output.
            if transient and attempt < max_attempts:
                time.sleep(2.0 * attempt)
                continue
            break

        # Re-parse with the session log for exact tool names + ordered transcript.
        session_log = read_session_log(parsed.session_id)
        if session_log:
            parsed = parse_run(stdout, stderr, session_log)
        raw = stdout + stderr
        mcp_failed = returncode == 3 or any("MCP" in e for e in parsed.errors)

        used_expected = None
        if task.expects_tool:
            # expects_tool may list several accepted tool-name fragments
            # separated by "|" (e.g. "run_script|execute_bash"); a run counts as
            # using the expected tool if any fragment matches any tool call.
            wanted = [w.strip() for w in task.expects_tool.split("|") if w.strip()]
            used_expected = any(
                any(w in tc or tc in w for w in wanted) for tc in parsed.tool_calls
            )

        return RunResult(
            task_id=task.id,
            stratum=task.stratum,
            condition=cond.name,
            role=cond.role,
            augmentation=cond.augmentation,
            model=model,
            rep=rep,
            returncode=returncode,
            wall_s=round(wall, 2),
            credits=parsed.credits,
            cli_time_s=parsed.time_s,
            tool_calls=parsed.tool_calls,
            tool_call_count=parsed.tool_call_count,
            mcp_servers=parsed.mcp_servers,
            used_expected_tool=used_expected,
            answer=parsed.answer,
            transcript=parsed.transcript,
            errors=parsed.errors,
            mcp_startup_failed=mcp_failed,
            transient_error=transient,
            session_id=parsed.session_id,
            run_status=parsed.status,
            kiro_version=kiro_version(),
            cmd=cmd,
            raw_excerpt=stderr[-2000:],
        )
    finally:
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)

"""Headless runner: invoke kiro-cli for one (task, condition, rep) in isolation."""

from __future__ import annotations

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


def _is_transient(raw: str) -> bool:
    return any(sig in raw for sig in _TRANSIENT_SIGNATURES)


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

        cmd = [
            KIRO_BIN,
            "chat",
            "--no-interactive",
            "--agent",
            cond.name,
            "--model",
            model,
            "--trust-all-tools",
        ]
        if require_mcp and _has_mcp(cond):
            cmd.append("--require-mcp-startup")
        cmd.append(task.prompt)

        max_attempts = 3
        transient = False
        for attempt in range(1, max_attempts + 1):
            start = time.monotonic()
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(workspace),
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                    env=os.environ.copy(),
                )
                wall = time.monotonic() - start
                raw = (proc.stdout or "") + (proc.stderr or "")
                returncode = proc.returncode
            except subprocess.TimeoutExpired as e:
                wall = time.monotonic() - start
                raw = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
                raw += "\n[KIRO-LIFT] TIMEOUT"
                returncode = -1

            transient = _is_transient(raw)
            # Retry only on transient backend errors that produced no usable output.
            if transient and attempt < max_attempts:
                time.sleep(2.0 * attempt)
                continue
            break

        parsed = parse_run(raw)
        mcp_failed = returncode == 3 or "MCP" in "".join(parsed.errors)

        used_expected = None
        if task.expects_tool:
            # expects_tool may list several accepted tool-name fragments
            # separated by "|" (e.g. "call_aws|use_aws"); a run counts as
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
            cmd=cmd,
            raw_excerpt=raw[-2000:],
        )
    finally:
        if not keep_workspace:
            shutil.rmtree(workspace, ignore_errors=True)

"""Experiment orchestrator: run (task x condition x rep), judge, write JSONL.

Tasks are processed one stratum at a time so a stratum can declare optional
setup/teardown hook scripts: tasks/<stratum>/setup.sh and tasks/<stratum>/
teardown.sh. The setup hook provisions whatever fixtures the stratum's tasks
need (e.g. the `api` stratum deploys a CloudFormation stack) and writes a JSON
map of outputs that the harness substitutes into task prompts / answer keys via
${PLACEHOLDER} tokens. The teardown hook always runs after the stratum finishes,
even on error, so cloud fixtures don't leak. The harness stays cloud-agnostic:
all AWS specifics live in the bash hooks.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .config import Condition, Experiment, Task, load_conditions, load_tasks
from .judge import judge_answer
from .runner import run_one


def _row_key(task_id: str, condition: str, rep: int) -> str:
    return f"{task_id}|{condition}|{rep}"


def _load_done_keys(out_path: Path) -> set[str]:
    done: set[str] = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if r.get("invalid"):
                    continue  # invalid runs are re-attempted on resume
                done.add(_row_key(r["task_id"], r["condition"], r["rep"]))
            except (json.JSONDecodeError, KeyError):
                continue
    return done


@dataclass
class Unit:
    task: Task
    cond: Condition
    rep: int


# ${NAME} tokens substituted from a stratum's setup-hook outputs. Names match
# the CloudFormation Output logical IDs, which must be alphanumeric (no
# underscores), so allow mixed-case identifiers here.
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _substitute(text: str, mapping: dict[str, str]) -> str:
    if not text or not mapping:
        return text
    return _PLACEHOLDER_RE.sub(lambda m: mapping.get(m.group(1), m.group(0)), text)


def _apply_fixtures(task: Task, mapping: dict[str, str]) -> Task:
    """Return a copy of `task` with fixture placeholders resolved in the fields
    the model and the judge see (prompt, answer_key, rubric)."""
    if not mapping:
        return task
    return replace(
        task,
        prompt=_substitute(task.prompt, mapping),
        answer_key=_substitute(task.answer_key, mapping),
        rubric=_substitute(task.rubric, mapping),
    )


def _run_hook(
    name: str, stratum: str, tasks_dir: Path
) -> tuple[bool, dict[str, str]]:
    """Run tasks/<stratum>/<name>.sh if present. Returns (ok, outputs).

    Outputs are read from the JSON file the hook writes to $KIRO_LIFT_FIXTURE_OUT
    (used by setup to surface fixture values for prompt substitution). A missing
    script is a no-op success.

    The hook inherits the full environment, so any cloud-specific config (e.g.
    AWS_REGION / AWS_DEFAULT_REGION, credentials, profiles) is the hook's own
    concern — the harness stays cloud-agnostic and only provides the generic
    KIRO_LIFT_* contract.
    """
    script = Path(tasks_dir) / stratum / f"{name}.sh"
    if not script.exists():
        return True, {}

    fd, out_file = tempfile.mkstemp(prefix=f"kirolift_{stratum}_{name}_", suffix=".json")
    os.close(fd)
    out_path = Path(out_file)
    env = os.environ.copy()
    env["KIRO_LIFT_FIXTURE_OUT"] = str(out_path)
    env["KIRO_LIFT_STRATUM"] = stratum

    print(f"[kiro-lift] {stratum}: running {name} hook ({script})", file=sys.stderr)
    try:
        proc = subprocess.run(["bash", str(script)], env=env, cwd=str(Path(tasks_dir).parent))
        ok = proc.returncode == 0
        if not ok:
            print(f"[kiro-lift] {stratum}: {name} hook exited {proc.returncode}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"[kiro-lift] {stratum}: {name} hook error: {e}", file=sys.stderr)
        ok = False

    mapping: dict[str, str] = {}
    if ok:
        try:
            txt = out_path.read_text().strip()
            if txt:
                mapping = {str(k): str(v) for k, v in json.loads(txt).items()}
        except (OSError, json.JSONDecodeError) as e:
            print(f"[kiro-lift] {stratum}: could not read {name} outputs: {e}", file=sys.stderr)
    out_path.unlink(missing_ok=True)
    return ok, mapping


def _execute(unit: Unit, exp: Experiment, fixtures: dict[str, str]) -> dict[str, Any]:
    task = _apply_fixtures(unit.task, fixtures)
    rr = run_one(task, unit.cond, exp.model, unit.rep, timeout_s=exp.timeout_s)
    # A run is "invalid" (excluded from scoring) if the final attempt still hit a
    # transient backend error. The CLI emits the error banner INTO the answer
    # text, so we must key off transient_error, not answer emptiness.
    invalid = bool(rr.transient_error)
    # Drop the model under test from its own judge panel to avoid self-preference.
    judges = [j for j in exp.judge_models if j != exp.model] or list(exp.judge_models)
    # Judge only if we got a usable answer, the run didn't hard-fail, and it's valid.
    judge_source = "" if invalid else (rr.answer or rr.transcript)
    if judge_source and rr.returncode == 0:
        jr = judge_answer(task, judge_source, judges)
    else:
        from .judge import JudgeResult
        jr = JudgeResult(None, None, None, 0, [])

    passed = (
        jr.correctness_mean is not None and jr.correctness_mean >= exp.pass_threshold
    )
    row = rr.to_dict()
    row.update(
        {
            "experiment": exp.name,
            "invalid": invalid,
            "passed": passed,
            "correctness_mean": jr.correctness_mean,
            "completeness_mean": jr.completeness_mean,
            "hallucination_rate": jr.hallucination_rate,
            "n_judges_ok": jr.n_judges_ok,
            "judge_verdicts": jr.verdicts,
            "ts": time.time(),
        }
    )
    # keep the JSONL compact: drop the long transcript, keep answer + excerpt
    row.pop("transcript", None)
    return row


def _error_row(u: Unit, exp: Experiment, msg: str) -> dict[str, Any]:
    return {
        "task_id": u.task.id, "stratum": u.task.stratum,
        "condition": u.cond.name, "role": u.cond.role,
        "augmentation": u.cond.augmentation, "model": exp.model,
        "rep": u.rep, "experiment": exp.name, "passed": False,
        "error": msg, "ts": time.time(),
    }


def run_experiment(
    exp: Experiment,
    tasks_dir: str | Path,
    conditions_dir: str | Path,
    out_path: str | Path,
    *,
    max_workers: int = 3,
    allow_account_tasks: bool = False,
    resume: bool = True,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tasks_dir = Path(tasks_dir)

    tasks = load_tasks(tasks_dir)
    if exp.task_filter:
        tasks = [t for t in tasks if t.stratum in exp.task_filter]
    if exp.task_ids:
        tasks = [t for t in tasks if t.id in exp.task_ids]
        missing = set(exp.task_ids) - {t.id for t in tasks}
        if missing:
            print(f"[kiro-lift] warning: task_ids not found: {sorted(missing)}", file=sys.stderr)
    if not allow_account_tasks:
        skipped = [t.id for t in tasks if t.requires_account]
        tasks = [t for t in tasks if not t.requires_account]
        if skipped:
            print(f"[kiro-lift] skipping {len(skipped)} account-requiring tasks "
                  f"(use --allow-account-tasks): {skipped}", file=sys.stderr)

    conditions = load_conditions(conditions_dir, exp.conditions)
    done = _load_done_keys(out_path) if resume else set()

    # Plan units per stratum so setup/teardown hooks bracket each stratum's runs.
    strata = sorted({t.stratum for t in tasks})
    plan: dict[str, list[Unit]] = {}
    for s in strata:
        s_tasks = [t for t in tasks if t.stratum == s]
        plan[s] = [
            Unit(t, c, rep)
            for t in s_tasks
            for c in conditions
            for rep in range(exp.reps)
            if _row_key(t.id, c.name, rep) not in done
        ]
    to_run = sum(len(u) for u in plan.values())
    grand_total = len(tasks) * len(conditions) * exp.reps
    print(f"[kiro-lift] experiment '{exp.name}': {len(tasks)} tasks x "
          f"{len(conditions)} conditions x {exp.reps} reps = {grand_total} runs "
          f"({len(done)} already done, {to_run} to run); strata={strata}", file=sys.stderr)

    completed = 0
    with open(out_path, "a") as fh:
        for s in strata:
            units = plan[s]
            if not units:
                continue  # nothing to run for this stratum (e.g. fully resumed)

            ok, fixtures = _run_hook("setup", s, tasks_dir)
            if not ok:
                msg = f"stratum '{s}' setup hook failed; runs skipped"
                print(f"[kiro-lift] {msg}", file=sys.stderr)
                for u in units:
                    fh.write(json.dumps(_error_row(u, exp, msg)) + "\n")
                    fh.flush()
                    completed += 1
                _run_hook("teardown", s, tasks_dir)  # clean any partial
                continue

            try:
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    futs = {ex.submit(_execute, u, exp, fixtures): u for u in units}
                    for fut in as_completed(futs):
                        u = futs[fut]
                        try:
                            row = fut.result()
                        except Exception as e:  # noqa: BLE001 - record and continue
                            row = _error_row(u, exp, f"{type(e).__name__}: {e}")
                        fh.write(json.dumps(row) + "\n")
                        fh.flush()
                        completed += 1
                        pf = "PASS" if row.get("passed") else "fail"
                        print(f"[kiro-lift] {completed}/{to_run} "
                              f"{row['task_id']}/{row['condition']}/r{row['rep']} -> {pf} "
                              f"(corr={row.get('correctness_mean')}, "
                              f"credits={row.get('credits')}, "
                              f"tools={row.get('tool_call_count')})", file=sys.stderr)
            finally:
                _run_hook("teardown", s, tasks_dir)
    return out_path

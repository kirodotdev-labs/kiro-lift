"""Config models and loaders for tasks, conditions, and experiments."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# The knowledge strata that determine where augmentation lift can come from.
#   stable_public  - well-documented, pre-cutoff knowledge (expect ~0 lift)
#   recent         - post-cutoff services / changed limits & APIs
#   correctness    - model knows roughly but errs / hallucinates
#   architecture   - "given this usage pattern, what should I use?" advice; tests
#                    whether augmentation yields better/newer service recommendations
#   api            - deterministic, fixture-backed questions answered by calling
#                    AWS APIs (e.g. call_aws); ground truth is provisioned via a
#                    CloudFormation stack by the stratum's setup/teardown hooks
#   skills         - procedural/SDK tasks where agent skills (step-by-step
#                    runbooks) should improve correctness; tests whether skills
#                    prevent common model mistakes on multi-step AWS workflows
STRATA = ("stable_public", "recent", "correctness", "architecture", "api", "skills")


@dataclass
class Task:
    id: str
    stratum: str
    prompt: str
    answer_key: str = ""
    rubric: str = ""
    # Gate (not a fixture flag): True means the task needs a live external
    # account / credentials to run and may incur cost or side effects. It is
    # provider-agnostic — AWS, Azure, Figma, GitHub, etc. — and is the harness's
    # only knob here; all provider specifics live in the stratum's hooks. The
    # harness skips these tasks unless `--allow-account-tasks` is passed.
    #
    # Independent of fixtures (setup.sh/teardown.sh): a task can need an account
    # without any fixture (e.g. reading pre-existing account state), and a
    # fixture can provision purely local/free state without needing an account.
    requires_account: bool = False
    # name of the MCP/tool we expect a successful augmented run to use (for
    # utilization scoring); None if not applicable.
    expects_tool: str | None = None
    path: Path | None = None

    @staticmethod
    def load(path: str | Path) -> "Task":
        path = Path(path)
        data = yaml.safe_load(path.read_text())
        stratum = data["stratum"]
        if stratum not in STRATA:
            raise ValueError(f"{path}: stratum '{stratum}' not in {STRATA}")
        return Task(
            id=data["id"],
            stratum=stratum,
            prompt=data["prompt"],
            answer_key=data.get("answer_key", ""),
            rubric=data.get("rubric", ""),
            requires_account=bool(data.get("requires_account", False)),
            expects_tool=data.get("expects_tool"),
            path=path,
        )


def load_tasks(tasks_dir: str | Path) -> list[Task]:
    tasks_dir = Path(tasks_dir)
    tasks = [Task.load(p) for p in sorted(tasks_dir.glob("**/task.yaml"))]
    ids = [t.id for t in tasks]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"duplicate task ids: {dupes}")
    return tasks


@dataclass
class Condition:
    """A condition is a drop-in Kiro agent config plus experiment metadata.

    Each condition is a directory `conditions/<name>/` containing:
      * `agent.json`  — the Kiro agent config (written into the run workspace at
        `.kiro/agents/<name>.json`).
      * `workspace/`  — optional; its contents are copied verbatim into the run
        workspace before each run, so a condition can ship skills, steering
        files, or any other workspace context as part of the augmentation
        (e.g. `workspace/.kiro/skills/<name>/SKILL.md`).
    """

    name: str
    role: str  # "baseline" | "treatment" | "control"
    augmentation: str  # human label, e.g. "none", "aws-documentation-mcp"
    agent_config: dict[str, Any]
    path: Path | None = None  # the condition directory
    workspace_dir: Path | None = None  # files to copy into each run workspace

    @staticmethod
    def load(path: str | Path) -> "Condition":
        """Load a condition from its directory (`conditions/<name>/`)."""
        path = Path(path)
        cfg = json.loads((path / "agent.json").read_text())
        meta = cfg.get("_lift", {})
        ws = path / "workspace"
        return Condition(
            name=cfg.get("name", path.name),
            role=meta.get("role", "treatment"),
            augmentation=meta.get("augmentation", path.name),
            agent_config=cfg,
            path=path,
            workspace_dir=ws if ws.is_dir() else None,
        )

    def agent_json(self) -> str:
        """Agent config with the harness-only `_lift` metadata stripped."""
        cfg = dict(self.agent_config)
        cfg.pop("_lift", None)
        cfg["name"] = self.name
        return json.dumps(cfg, indent=2)


def load_conditions(conditions_dir: str | Path, names: list[str] | None = None) -> list[Condition]:
    conditions_dir = Path(conditions_dir)
    dirs = sorted(
        p for p in conditions_dir.iterdir()
        if p.is_dir() and (p / "agent.json").exists()
    )
    conds = [Condition.load(p) for p in dirs]
    if names is not None:
        by_name = {c.name: c for c in conds}
        missing = [n for n in names if n not in by_name]
        if missing:
            raise ValueError(f"conditions not found: {missing} (have {list(by_name)})")
        conds = [by_name[n] for n in names]
    return conds


@dataclass
class Experiment:
    name: str
    model: str  # model under test
    conditions: list[str]  # condition names
    judge_models: list[str]
    reps: int = 5
    pass_threshold: float = 6.0  # judge score (0-10) >= threshold counts as pass
    task_filter: list[str] = field(default_factory=list)  # stratum names; empty = all
    task_ids: list[str] = field(default_factory=list)  # specific task ids; empty = all
    timeout_s: int = 300

    @staticmethod
    def load(path: str | Path) -> "Experiment":
        data = yaml.safe_load(Path(path).read_text())
        return Experiment(
            name=data["name"],
            model=data["model"],
            conditions=data["conditions"],
            judge_models=data["judge_models"],
            reps=int(data.get("reps", 5)),
            pass_threshold=float(data.get("pass_threshold", 6.0)),
            task_filter=data.get("task_filter", []) or [],
            task_ids=data.get("task_ids", []) or [],
            timeout_s=int(data.get("timeout_s", 300)),
        )

"""Lift for Kiro — command-line entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import Experiment, load_conditions, load_tasks
from .experiment import run_experiment
from .stats import analyze, load_jsonl, render_markdown

ROOT = Path(__file__).resolve().parent.parent


def _cmd_run(args: argparse.Namespace) -> int:
    exp = Experiment.load(args.experiment)
    # Optional model override (for model sweeps): one experiment file, many models.
    if args.model:
        exp.model = args.model
    # Optional iteration overrides — probe one experiment file quickly without
    # editing it (e.g. a fast smoke at --reps 1, or a single stratum/task).
    if args.reps is not None:
        exp.reps = args.reps
    if args.task_filter:
        exp.task_filter = args.task_filter
    if args.task_ids:
        exp.task_ids = args.task_ids
    # The default output path is keyed only on name (+ model), so ad-hoc reps/
    # task overrides reuse the same results file and interact with resume (it
    # treats existing rows as done). Warn unless the user isolated the run.
    overrode_scope = args.reps is not None or bool(args.task_filter) or bool(args.task_ids)
    if overrode_scope and not args.out and not args.no_resume:
        print(
            "[kiro-lift] warning: --reps/--task-filter/--task-ids override the "
            "experiment but write to the default results file; existing rows are "
            "resumed. Pass --out <path> or --no-resume to keep this run separate.",
            file=sys.stderr,
        )
    # Default output path; when a model override is given, separate the file per
    # model so concurrent sweep runs don't collide and resume stays correct.
    if args.out:
        out = args.out
    elif args.model:
        out = ROOT / "results" / f"{exp.name}-{exp.model}.jsonl"
    else:
        out = ROOT / "results" / f"{exp.name}.jsonl"
    run_experiment(
        exp,
        tasks_dir=args.tasks or (ROOT / "tasks"),
        conditions_dir=args.conditions or (ROOT / "conditions"),
        out_path=out,
        max_workers=args.workers,
        allow_account_tasks=args.allow_account_tasks,
        resume=not args.no_resume,
    )
    print(f"Results written to {out}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    rows = load_jsonl(args.results)
    summary = analyze(rows, baseline=args.baseline)
    md = render_markdown(summary)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(summary, indent=2))
        print(f"JSON summary -> {args.json}", file=sys.stderr)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(md)
        print(f"Markdown report -> {args.out}", file=sys.stderr)
    else:
        print(md)
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    tasks = load_tasks(args.tasks or (ROOT / "tasks"))
    conds = load_conditions(args.conditions or (ROOT / "conditions"))
    print(f"{len(tasks)} tasks:")
    by_stratum: dict[str, int] = {}
    for t in tasks:
        by_stratum[t.stratum] = by_stratum.get(t.stratum, 0) + 1
        flag = " [needs account]" if t.requires_account else ""
        print(f"  - {t.id} ({t.stratum}){flag}")
    print("by stratum:", by_stratum)
    print(f"\n{len(conds)} conditions:")
    for c in conds:
        mcp = list((c.agent_config.get("mcpServers") or {}).keys())
        ws = ""
        if c.workspace_dir is not None:
            files = [str(p.relative_to(c.workspace_dir))
                     for p in sorted(c.workspace_dir.rglob("*")) if p.is_file()]
            ws = f" workspace={files}"
        print(f"  - {c.name} role={c.role} aug={c.augmentation} mcp={mcp}{ws}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kiro-lift", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run an experiment")
    pr.add_argument("experiment", help="path to experiment YAML")
    pr.add_argument("--tasks", help="tasks dir (default: ./tasks)")
    pr.add_argument("--conditions", help="conditions dir (default: ./conditions)")
    pr.add_argument("--out", help="output JSONL path")
    pr.add_argument("--model", help="override the experiment's model (for model sweeps)")
    pr.add_argument("--reps", type=int, help="override reps per (task, condition)")
    pr.add_argument("--task-filter", nargs="+", metavar="STRATUM",
                    help="override task_filter: only run these strata")
    pr.add_argument("--task-ids", nargs="+", metavar="ID",
                    help="override task_ids: only run these specific task ids")
    pr.add_argument("--workers", type=int, default=3, help="parallel runs (default 3)")
    pr.add_argument("--allow-account-tasks", action="store_true",
                    help="include tasks that need a live external account / "
                         "credentials (any provider, e.g. AWS/Azure/Figma) and "
                         "may incur cost; skipped by default")
    pr.add_argument("--no-resume", action="store_true", help="ignore existing rows")
    pr.set_defaults(func=_cmd_run)

    pp = sub.add_parser("report", help="analyze results JSONL")
    pp.add_argument("results", help="path to results JSONL")
    pp.add_argument("--baseline", default="lift-baseline", help="baseline condition name")
    pp.add_argument("--out", help="write markdown report to this path")
    pp.add_argument("--json", help="write JSON summary to this path")
    pp.set_defaults(func=_cmd_report)

    pv = sub.add_parser("validate", help="list/validate tasks and conditions")
    pv.add_argument("--tasks")
    pv.add_argument("--conditions")
    pv.set_defaults(func=_cmd_validate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

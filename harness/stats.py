"""Aggregation, paired statistics, and reporting for kiro-lift results.

Reads the JSONL produced by the orchestrator and emits a recommendation-shaped
report: per-stratum lift, capability-unlock rate, hallucination reduction, and
overhead (credits/time), with a paired McNemar test (baseline vs each treatment).
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


# The base toolset every condition shares (workspace inspection). Tool-use is
# measured as calls BEYOND these, so a bare baseline that only peeks at the
# (empty) workspace with read/glob reads 0% and the metric reflects "reached for
# an augmentation/knowledge tool" (web_search, web_fetch, an MCP tool, ...).
BASE_TOOLS = {"read", "grep", "glob"}


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for discordant counts b, c.

    b = #(baseline pass, treatment fail); c = #(baseline fail, treatment pass).
    """
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) * (0.5 ** n)
    return min(1.0, 2.0 * tail)


def _wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


@dataclass
class TaskOutcome:
    task_id: str
    stratum: str
    passed: bool          # majority of reps passed
    pass_frac: float
    mean_credits: float | None
    mean_time: float | None
    hallucination_rate: float | None
    tool_use_frac: float | None     # fraction of reps that used a NON-BASE tool
                                    # (any tool beyond the shared read/grep/glob)


def _reduce_task(rows: list[dict[str, Any]]) -> TaskOutcome:
    n = len(rows)
    passes = [bool(r.get("passed")) for r in rows]
    pass_frac = sum(passes) / n if n else 0.0
    creds = [r["credits"] for r in rows if r.get("credits") is not None]
    times = [r["cli_time_s"] for r in rows if r.get("cli_time_s") is not None]
    halls = [r["hallucination_rate"] for r in rows if r.get("hallucination_rate") is not None]
    # Tool-use = the run reached for a tool BEYOND the shared base toolset
    # (read/grep/glob) — an actual augmentation/knowledge tool such as
    # web_search, web_fetch, or an MCP tool. Base filesystem calls are excluded
    # so a bare baseline that only peeks at the empty workspace reads 0%.
    tu = [
        any(tc not in BASE_TOOLS for tc in (r.get("tool_calls") or []))
        for r in rows if r.get("tool_calls") is not None
    ]
    return TaskOutcome(
        task_id=rows[0]["task_id"],
        stratum=rows[0]["stratum"],
        passed=pass_frac >= 0.5,
        pass_frac=round(pass_frac, 3),
        mean_credits=round(mean(creds), 3) if creds else None,
        mean_time=round(mean(times), 2) if times else None,
        hallucination_rate=round(mean(halls), 3) if halls else None,
        tool_use_frac=round(sum(1 for t in tu if t) / len(tu), 3) if tu else None,
    )


def _by_task(rows: Iterable[dict[str, Any]]) -> dict[str, TaskOutcome]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r["task_id"]].append(r)
    return {tid: _reduce_task(rs) for tid, rs in grouped.items()}


def analyze(rows: list[dict[str, Any]], baseline: str) -> dict[str, Any]:
    # Drop invalid runs (transient backend errors with no usable answer) and
    # dedup by (task, condition, rep) keeping the last occurrence.
    def _invalid(r: dict[str, Any]) -> bool:
        if r.get("invalid"):
            return True
        return r.get("credits") is None and not (r.get("answer") or "").strip()

    deduped: dict[tuple, dict[str, Any]] = {}
    n_invalid = 0
    for r in rows:
        if _invalid(r):
            n_invalid += 1
            continue
        deduped[(r["task_id"], r["condition"], r["rep"])] = r
    rows = list(deduped.values())

    # Run metadata for the report header (computed from the kept rows).
    models = sorted({r.get("model") for r in rows if r.get("model")})
    experiments = sorted({r.get("experiment") for r in rows if r.get("experiment")})
    judges = sorted({
        v.get("judge_model")
        for r in rows for v in (r.get("judge_verdicts") or [])
        if v.get("judge_model")
    })
    reps = max((int(r.get("rep", 0)) for r in rows), default=-1) + 1
    meta = {
        "experiment": ", ".join(experiments),
        "models": models,
        "judge_models": judges,
        "reps": reps,
        "n_rows": len(rows),
    }

    by_cond: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_cond[r["condition"]].append(r)

    if baseline not in by_cond:
        raise ValueError(f"baseline condition '{baseline}' not in results ({list(by_cond)})")

    base_tasks = _by_task(by_cond[baseline])
    summary: dict[str, Any] = {
        "baseline": baseline,
        "meta": {**meta, "n_tasks": len(base_tasks)},
        "excluded_invalid_runs": n_invalid,
        "conditions": {},
    }

    for cond, crows in by_cond.items():
        cond_tasks = _by_task(crows)
        strata = sorted({t.stratum for t in cond_tasks.values()})
        per_stratum = {}
        for s in strata:
            ct = [t for t in cond_tasks.values() if t.stratum == s]
            bt = {tid: t for tid, t in base_tasks.items() if t.stratum == s}
            n = len(ct)
            n_pass = sum(1 for t in ct if t.passed)
            lo, hi = _wilson_ci(n_pass, n)

            # paired McNemar + capability unlock vs baseline (same tasks)
            b = c = unlock = 0
            paired_n = 0
            for t in ct:
                bt_o = bt.get(t.task_id)
                if bt_o is None:
                    continue
                paired_n += 1
                if bt_o.passed and not t.passed:
                    b += 1
                elif (not bt_o.passed) and t.passed:
                    c += 1
                    unlock += 1
            p = mcnemar_exact_p(b, c)

            creds = [t.mean_credits for t in ct if t.mean_credits is not None]
            times = [t.mean_time for t in ct if t.mean_time is not None]
            halls = [t.hallucination_rate for t in ct if t.hallucination_rate is not None]
            tus = [t.tool_use_frac for t in ct if t.tool_use_frac is not None]

            base_succ = (sum(1 for t in bt.values() if t.passed) / len(bt)) if bt else None
            succ = n_pass / n if n else 0.0
            per_stratum[s] = {
                "n_tasks": n,
                "success": round(succ, 3),
                "success_ci": [round(lo, 3), round(hi, 3)],
                "baseline_success": round(base_succ, 3) if base_succ is not None else None,
                "lift": round(succ - base_succ, 3) if base_succ is not None else None,
                "capability_unlock_rate": round(unlock / paired_n, 3) if paired_n else None,
                "mcnemar": {"b_base_only": b, "c_treat_only": c, "p": round(p, 4)},
                "mean_credits": round(mean(creds), 3) if creds else None,
                "mean_time_s": round(mean(times), 2) if times else None,
                "hallucination_rate": round(mean(halls), 3) if halls else None,
                "tool_use_frac": round(mean(tus), 3) if tus else None,
            }

        all_creds = [t.mean_credits for t in cond_tasks.values() if t.mean_credits is not None]
        summary["conditions"][cond] = {
            "role": crows[0].get("role"),
            "augmentation": crows[0].get("augmentation"),
            "n_tasks": len(cond_tasks),
            "overall_success": round(
                sum(1 for t in cond_tasks.values() if t.passed) / len(cond_tasks), 3
            ),
            "mean_credits": round(mean(all_creds), 3) if all_creds else None,
            "per_stratum": per_stratum,
        }
    return summary


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.0%}"


def _bar(frac: float | None, width: int = 10) -> str:
    """Unicode horizontal bar for a fraction in [0, 1] (renders everywhere)."""
    if frac is None:
        return ""
    f = max(0.0, min(1.0, frac))
    filled = round(f * width)
    return "█" * filled + "░" * (width - filled)


def render_markdown(summary: dict[str, Any]) -> str:
    base = summary["baseline"]
    conds = summary["conditions"]
    meta = summary.get("meta", {})
    out = ["# kiro-lift report", ""]

    # --- header block -----------------------------------------------------
    hdr: list[str] = []
    if meta.get("experiment"):
        hdr.append(f"**Experiment:** {meta['experiment']}")
    if meta.get("models"):
        hdr.append(f"**Model(s) under test:** {', '.join(meta['models'])}")
    if meta.get("reps"):
        hdr.append(f"**Reps:** {meta['reps']}")
    if meta.get("n_tasks"):
        hdr.append(f"**Tasks:** {meta['n_tasks']}")
    hdr.append(f"**Baseline:** `{base}`")
    out.append("  •  ".join(hdr))
    if meta.get("judge_models"):
        out.append("")
        out.append(f"**Judges:** {', '.join(meta['judge_models'])}")
    if summary.get("excluded_invalid_runs"):
        out.append("")
        out.append(f"_Excluded {summary['excluded_invalid_runs']} invalid run(s) (transient errors)._")
    out.append("")

    # condition order: baseline first, then the rest in insertion order
    cond_order = [base] + [c for c in conds if c != base]
    strata = sorted({s for c in conds.values() for s in c["per_stratum"]})

    out.append("## Summary by stratum")
    out.append("")
    out.append("_Conditions as rows; the baseline is the reference. Per-stratum is "
               "the decision-relevant view — the blended overall number is omitted "
               "on purpose (it mostly reflects task mix)._")
    out.append("")

    for s in strata:
        base_ps = conds[base]["per_stratum"].get(s, {})
        n = base_ps.get("n_tasks") or next(
            (conds[c]["per_stratum"][s]["n_tasks"] for c in conds if s in conds[c]["per_stratum"]), 0
        )
        base_cr = base_ps.get("mean_credits")
        base_hall = base_ps.get("hallucination_rate")

        out.append(f"### `{s}` (n={n})")
        out.append("")
        out.append("| Condition | Success | Lift | Unlock | Halluc (Δ) | Tool-use | "
                   "Cost (cr / ×base) | McNemar p (b→c) |")
        out.append("|" + "---|" * 8)

        for cond in cond_order:
            ps = conds[cond]["per_stratum"].get(s)
            if not ps:
                continue
            is_base = cond == base
            name = f"`{cond}`" + (" _(ref)_" if is_base else "")
            succ = _pct(ps.get("success"))

            if is_base:
                lift = "_ref_"
                unlock = "—"
                mcnemar = "—"
            else:
                lv = ps.get("lift")
                lift = f"{lv:+.0%} {_bar(abs(lv))}" if lv is not None else "—"
                unlock = _pct(ps.get("capability_unlock_rate"))
                m = ps.get("mcnemar") or {}
                mcnemar = f"{m.get('p')} ({m.get('b_base_only')}→{m.get('c_treat_only')})"

            hall = ps.get("hallucination_rate")
            if hall is None:
                hall_s = "—"
            elif is_base or base_hall is None:
                hall_s = f"{hall:.0%}"
            else:
                hall_s = f"{hall:.0%} ({hall - base_hall:+.0%})"

            cr = ps.get("mean_credits")
            if cr is None:
                cost = "—"
            elif is_base:
                cost = f"{cr} / 1.0×"
            elif base_cr:
                cost = f"{cr} / {cr / base_cr:.1f}×"
            else:
                cost = f"{cr}"

            out.append(
                f"| {name} | {succ} | {lift} | {unlock} | {hall_s} | "
                f"{_pct(ps.get('tool_use_frac'))} | {cost} | {mcnemar} |"
            )
        out.append("")

        # best-per-stratum recommendation + chart (only where there's real lift)
        treat = [(c, conds[c]["per_stratum"][s]) for c in cond_order
                 if c != base and s in conds[c]["per_stratum"]]
        lifted = [(c, d) for c, d in treat if (d.get("lift") or 0) > 0]
        if lifted:
            def _costx(d: dict[str, Any]) -> float:
                cr = d.get("mean_credits")
                return cr / base_cr if (cr and base_cr) else float("inf")
            bc, bd = sorted(lifted, key=lambda cd: (-(cd[1]["lift"] or 0), _costx(cd[1])))[0]
            cx = _costx(bd)
            cx_s = f"{cx:.1f}×" if cx != float("inf") else "n/a"
            out.append(f"**Best for `{s}`:** `{bc}` — {bd['lift']:+.0%} lift at {cx_s} baseline cost.")
        else:
            out.append(f"**Best for `{s}`:** none — no measurable lift "
                       "(baseline already saturates this stratum).")
        out.append("")

    out.append("---")
    out.append("_Lift = success − baseline success on the same tasks. "
               "Unlock = baseline-fail → treatment-pass rate. "
               "Halluc (Δ) = hallucination rate (Δ vs baseline). "
               "Tool-use = fraction of runs that used a tool BEYOND the shared "
               "read/grep/glob base (an augmentation/knowledge tool such as "
               "web_search, web_fetch, or an MCP tool); a bare baseline reads 0%. "
               "Cost = mean credits/run (and ×baseline). "
               "McNemar p = two-sided exact paired test with b→c discordant counts; "
               "small n (shown) is underpowered, so read p alongside lift._")
    return "\n".join(out)


# --- self-test -------------------------------------------------------------
if __name__ == "__main__":
    assert mcnemar_exact_p(0, 0) == 1.0
    # classic: b=1,c=9 -> p ~ 0.0215
    p = mcnemar_exact_p(1, 9)
    assert abs(p - 0.0215) < 0.01, p
    lo, hi = _wilson_ci(8, 10)
    assert 0.4 < lo < 0.6 and 0.9 < hi <= 1.0, (lo, hi)
    print("stats.py self-test OK; mcnemar(1,9)=", round(p, 4), "wilson(8/10)=", (round(lo,3), round(hi,3)))

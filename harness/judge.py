"""Multi-model LLM judge.

Each run's candidate answer is scored by several judge models (via kiro-cli
headless with a no-tools agent). Scores are averaged; per-judge detail is kept.
Using multiple judges -- and judges different from the model under test --
reduces single-model bias and self-preference.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .config import Task

KIRO_BIN = os.environ.get("KIRO_LIFT_BIN", "kiro-cli")

_JUDGE_AGENT = {
    "name": "lift-judge",
    "description": "Read-only grader. Emits a JSON verdict only.",
    "prompt": (
        "You are a strict, impartial grader of an AI assistant's answer to an "
        "AWS-related task. You do not use tools. You output only a single JSON object."
    ),
    "tools": [],
    "allowedTools": [],
}

_JUDGE_TEMPLATE = """\
You are grading an AI assistant's answer to an AWS task.

# Task given to the assistant
{prompt}

# Reference answer / ground truth (authoritative)
{answer_key}

# Scoring rubric
{rubric}

# Assistant's answer to grade
<<<BEGIN ANSWER>>>
{candidate}
<<<END ANSWER>>>

Grade the assistant's answer against the reference and rubric. Judge factual
and technical correctness for AWS specifically.

Hallucination rule (apply consistently): mark "hallucination": true ONLY when
the answer fabricates specifics that do not actually exist or are asserted as
fact without basis — invented APIs, parameters, service names, limits, pricing,
DATES, or product/availability status. Do NOT mark hallucination when the answer
is merely outdated/stale but was plausibly correct at an earlier time, nor when
it honestly expresses uncertainty or declines to answer — those should simply
receive a LOWER correctness score (and are not hallucinations).

Output ONLY a single JSON object, no prose, with exactly these keys:
{{"correctness": <integer 0-10>, "hallucination": <true|false>, "completeness": <integer 0-10>, "justification": "<one sentence>"}}
"""

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class JudgeVerdict:
    judge_model: str
    correctness: float | None
    completeness: float | None
    hallucination: bool | None
    justification: str
    ok: bool
    raw: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class JudgeResult:
    correctness_mean: float | None
    completeness_mean: float | None
    hallucination_rate: float | None  # fraction of judges flagging hallucination
    n_judges_ok: int
    verdicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_json(cleaned: str) -> dict[str, Any] | None:
    # find the LAST balanced-looking JSON object (judges sometimes echo schema first)
    candidates = _JSON_OBJ_RE.findall(cleaned)
    for chunk in reversed(candidates):
        # try progressively trimming to the smallest valid object
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            # try to find innermost object
            m = re.findall(r"\{[^{}]*\}", chunk)
            for inner in reversed(m):
                try:
                    return json.loads(inner)
                except json.JSONDecodeError:
                    continue
    return None


def _run_judge(prompt: str, judge_model: str, timeout_s: int) -> str:
    from .parse import strip_ansi

    workspace = Path(tempfile.mkdtemp(prefix=f"kirolift_judge_{judge_model}_"))
    try:
        agents_dir = workspace / ".kiro" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        (agents_dir / "lift-judge.json").write_text(json.dumps(_JUDGE_AGENT, indent=2))
        cmd = [
            KIRO_BIN, "chat", "--no-interactive",
            "--agent", "lift-judge",
            "--model", judge_model,
            "--trust-tools=",  # trust no tools
            prompt,
        ]
        try:
            proc = subprocess.run(
                cmd, cwd=str(workspace), capture_output=True, text=True,
                timeout=timeout_s, env=os.environ.copy(),
            )
            return strip_ansi((proc.stdout or "") + (proc.stderr or ""))
        except subprocess.TimeoutExpired:
            return "[KIRO-LIFT] JUDGE TIMEOUT"
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def judge_answer(
    task: Task, candidate: str, judge_models: list[str], *, timeout_s: int = 180
) -> JudgeResult:
    prompt = _JUDGE_TEMPLATE.format(
        prompt=task.prompt,
        answer_key=task.answer_key or "(no reference provided; grade on technical correctness)",
        rubric=task.rubric or "(no rubric; use general AWS technical correctness)",
        candidate=candidate or "(empty answer)",
    )
    verdicts: list[JudgeVerdict] = []
    for jm in judge_models:
        raw = _run_judge(prompt, jm, timeout_s)
        obj = _extract_json(raw)
        if obj is None:
            verdicts.append(JudgeVerdict(jm, None, None, None, "parse_failed", False, raw[-500:]))
            continue

        def _num(key: str) -> float | None:
            v = obj.get(key)
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        hall = obj.get("hallucination")
        if isinstance(hall, str):
            hall = hall.strip().lower() in ("true", "yes", "1")
        verdicts.append(
            JudgeVerdict(
                judge_model=jm,
                correctness=_num("correctness"),
                completeness=_num("completeness"),
                hallucination=bool(hall) if hall is not None else None,
                justification=str(obj.get("justification", ""))[:300],
                ok=True,
            )
        )

    ok = [v for v in verdicts if v.ok and v.correctness is not None]
    corr = [v.correctness for v in ok]
    comp = [v.completeness for v in ok if v.completeness is not None]
    halls = [v.hallucination for v in ok if v.hallucination is not None]

    return JudgeResult(
        correctness_mean=round(sum(corr) / len(corr), 3) if corr else None,
        completeness_mean=round(sum(comp) / len(comp), 3) if comp else None,
        hallucination_rate=round(sum(1 for h in halls if h) / len(halls), 3) if halls else None,
        n_judges_ok=len(ok),
        verdicts=[v.to_dict() for v in verdicts],
    )

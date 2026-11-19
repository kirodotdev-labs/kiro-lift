# AGENTS.md — orientation for AI agents working in kiro-lift

Read this first. It captures what the project is, how it's wired, and the
conventions to follow so you can be productive without re-deriving everything.

## What this is

Lift for Kiro measures the **lift** of knowledge augmentations for Kiro (MCP servers,
skills, steering, agents) by paired A/B testing against a baseline, using
headless `kiro-cli` runs + multi-model LLM judging + paired stats. The shipped
worked example studies the **Agent Toolkit for AWS** (the managed AWS MCP Server:
docs, `run_script`, and skills via `retrieve_skill`) vs a model with
native tools (web + AWS CLI) vs a bare model.

Key framing: the question is **where** augmentation helps and **whether it's
worth the overhead**, sliced by task stratum — not a single global score.

## How a run works (pipeline)

1. `harness/config.py` loads `Task`s (`tasks/<stratum>/<id>/task.yaml`),
   `Condition`s (`conditions/<name>/agent.json` + optional `workspace/`), and an
   `Experiment` (`experiments/*.yaml`).
2. `harness/experiment.py` orchestrates `task x condition x rep`, **one stratum
   at a time**, running optional `tasks/<stratum>/setup.sh` before and
   `teardown.sh` after (teardown always runs, even on error).
3. `harness/runner.py` writes the condition's agent JSON (and a workspace
   `cli.json` that disables inheriting the user's own steering/skills/AGENTS.md)
   into a temp workspace and invokes `kiro-cli chat --no-interactive
   --agent-engine v3 --trust-workspace --output-format stream-json --agent <name>
   --model <m> --trust-all-tools` (+ `--require-mcp-startup` when the condition
   has an MCP). Retries transient backend errors; marks still-failing ones
   `invalid`. Records `kiro_version` and `session_id` per run.
4. `harness/parse.py` reads the stream-json events (answer, credits, elapsed
   time, status, session id) and the run's session log
   (`~/.kiro/sessions/*/<sessionId>/messages.jsonl`) for exact tool names, MCP
   servers, and the ordered transcript. v3 tool ids: `read_file`,
   `list_directory`, `file_search`, `grep_search`, `code` (the `read` tag),
   `execute_bash`, `remote_web_search`, `web_fetch`; MCP calls are recorded by
   bare tool name (`aws___search_documentation`), skill loads as
   `load_resource(skill::<name>)`, and MCP tool-definition loads are not counted.
5. `harness/judge.py` scores the answer with several judge models (no-tools
   grader agent emitting a JSON verdict). The model under test is dropped from
   the judge panel automatically.
6. `harness/stats.py` aggregates the JSONL into a per-stratum report (success,
   lift vs baseline, capability-unlock, hallucination, overhead, McNemar p).

## Strata (tasks/<stratum>/)

`stable_public` (≈0 lift sanity), `recent` (post-cutoff gap), `correctness`
(plausible-but-wrong), `architecture` (usage-pattern → recommendation advice),
`api` (deterministic, fixture-backed AWS API questions), `skills` (procedural/
SDK-correctness — does a skill prevent the common model mistake?). The list of
valid strata is `STRATA` in `harness/config.py`; update it if you add one. Each
stratum has a `README.md` explaining its intent. The repo ships a deliberately
small, discriminative set — **2 tasks per stratum**.

## Conditions

Each condition is a directory `conditions/<name>/` with an `agent.json` (the
Kiro v3 agent config: `tools` selectors such as `read`/`shell`/`web`/`@server`,
optional `permissions`, `resources`, `mcpServers`; no legacy `allowedTools` /
`toolsSettings`) and an optional `workspace/` whose contents are copied verbatim
into each run workspace before the run — so a condition can ship skills
(`workspace/.kiro/skills/<n>/SKILL.md`, declared via a `skill://` resource),
steering, or any other context as part of the augmentation. All share an
identical prompt + base tools (the `read` tag); only the added retrieval/API
tools or workspace files differ.

- `lift-baseline` — bare model, no AWS access (the reference).
- `lift-baseline-native` — + Kiro's native tools: `web` (`remote_web_search`/
  `web_fetch`) **and** `shell` (`execute_bash`) with the AWS CLI installed; v3
  has no `use_aws` built-in. The realistic "native tools" bar.
- `lift-aws-mcp` — + the Agent Toolkit for AWS (managed AWS MCP Server) via
  `mcp-proxy-for-aws` (us-east-1 endpoint, operates us-west-2; SigV4 → needs
  creds). Provides `search_documentation`/`read_documentation`, `run_script`
  (AWS API access), region/availability lookups, and skill discovery
  (`retrieve_skill`). `recommend` and `call_aws` no longer exist.
- `lift-skill` — bare model + a locally-provided skill (`aws-sdk-python-usage`)
  shipped in `workspace/.kiro/skills/` and declared with
  `resources: ["skill://.kiro/skills/**/SKILL.md"]`. Demonstrates testing a
  skill you bring yourself, vs the toolkit discovering it at runtime.

`_lift` metadata in each `agent.json` (`role`, `augmentation`) is harness-only
and stripped before the agent sees it.

## Fixtures / setup-teardown contract

A stratum may declare `setup.sh` / `teardown.sh`. The harness execs them from
the repo root with env:
- `KIRO_LIFT_FIXTURE_OUT` — path where setup writes a flat JSON of outputs
- `KIRO_LIFT_STRATUM` — the stratum name

The harness stays cloud-agnostic: it injects only the generic `KIRO_LIFT_*`
contract above and inherits the rest of the environment. Any cloud-specific
config (AWS region, credentials, profiles) is the hook's own concern — the `api`
hooks read region from `AWS_REGION` / `AWS_DEFAULT_REGION` (default us-west-2).

setup writes e.g. `{"DataBucket":"...","AppSg":"..."}`; the harness substitutes
`${DataBucket}` etc. into each task's `prompt`/`answer_key`/`rubric` at runtime.
Token names match the CloudFormation Output logical IDs (which must be
alphanumeric — no underscores).
The `api` stratum's hooks deploy/delete `tasks/api/fixtures.cfn.yaml` (the
template lives with the stratum that uses it).
Keep all AWS specifics inside the bash hooks — the harness stays cloud-agnostic.

`requires_account` (a per-task gate) and fixtures (per-stratum setup/teardown)
are independent. `requires_account` means the task needs a live external account
/ credentials (provider-agnostic — AWS/Azure/Figma/…) and may cost money; such
tasks are skipped unless `--allow-account-tasks`. A fixture can provision
local/free state (no gate needed), and an account-gated task can have no fixture
(e.g. reading pre-existing state). The gate filters tasks; hooks bracket strata
that still have tasks.

## Commands

```bash
.venv/bin/python -m harness.cli validate              # list tasks + conditions
.venv/bin/python -m harness.parse                     # parser self-test
.venv/bin/python -m harness.stats                     # stats self-test
.venv/bin/python -m harness.cli run experiments/agent-toolkit-lift.yaml [--model <m>]
.venv/bin/python -m harness.cli run experiments/api-lift.yaml --allow-account-tasks
.venv/bin/python -m harness.cli report results/<e>.jsonl --baseline lift-baseline --out report/<e>.md
```

`run` overrides (probe a file without editing it): `--model`, `--reps`,
`--task-filter <stratum...>`, `--task-ids <id...>`. The default results path is
keyed on name (+model), so pair scope overrides with `--out`/`--no-resume` (the
CLI warns otherwise). `pass_threshold`/`conditions` are intentionally not
overridable — they define the experiment.

Experiments: `smoke` (baseline-only pipeline check), `agent-toolkit-lift` (the
account-free MCP worked example over 5 strata; the one swept across models
via `--model`), `skills-lift` (the bring-your-own-skill example: baseline vs
`lift-skill` vs MCP discovery, skills stratum), `api-lift` (the `api` stratum;
needs creds + `--allow-account-tasks`).
A model override writes `results/<name>-<model>.jsonl` so concurrent sweep runs
don't collide (loop over `--model` to sweep several models).

`kiro-cli` runs cost real money and hit the network/AWS; don't launch full
experiments unprompted. Prefer `validate` + the self-tests to check wiring.

## Conventions / gotchas

- Tasks are stochastic → use `reps` and majority pass; pre-screen new tasks for
  discrimination (≈30–70% baseline pass) before scaling.
- `expects_tool` accepts a `|`-separated list of accepted fragments and matches
  by substring, so `run_script|execute_bash` counts either API path and
  `retrieve_skill|search_documentation|skill::` counts MCP skill discovery or a
  local skill load (`load_resource(skill::...)`). Fragments match
  `aws___`-prefixed names too (e.g. `run_script` ⊂ `aws___run_script`).
- Architecture prompts must describe a usage pattern **only** — never name the
  target service or any specific tool.
- `api` tasks must stay read-only; resource create/delete happens only in hooks.
- Results/reports are local artifacts: everything under `results/` and `report/`
  is gitignored (except `.gitkeep`), since runs are stochastic/model-dependent.
- Run `validate` and the `parse`/`stats` self-tests after harness changes.
- The harness targets kiro-cli v3 (`--agent-engine v3`); the v2 engine prints a
  different stdout format that the parser does not read.
```

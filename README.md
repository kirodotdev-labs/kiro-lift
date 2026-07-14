# Lift for Kiro

![Lift for Kiro](assets/kiro-lift.jpg)

**Lift for Kiro measures the _lift_ of a knowledge augmentation for Kiro** — does
adding it actually make the agent better, *where*, by *how much*, and is it worth
the overhead? It answers this by paired A/B testing a treatment agent against a
baseline across a stratified task set, using headless `kiro-cli` runs,
multi-model LLM judging, and paired statistics.

The central question is deliberately **not** "does augmentation help?" but
**"where does it help, by how much, and is it worth the overhead?"** — because
much of an augmentation's content often overlaps what the model already knows.

## A reusable harness

Lift for Kiro is **augmentation-agnostic**. An "augmentation" is anything you can
express as a difference between two Kiro agent configurations:

- **MCP servers** — remote (HTTP / SigV4) or local (stdio / `uvx`)
- **Built-in tools** — e.g. web search/fetch, the AWS CLI
- **Custom agents / personas** — a different system prompt or tool policy
- **Steering files and skills** — workspace context supplied to the condition

The repository ships a complete **worked example** that exercises the **Agent 
Toolkit for AWS** — but you point the same harness at your own augmentation just 
by adding a condition (and, if useful, your own tasks). See [Test your own 
augmentation](#test-your-own-augmentation).

## How it works

1. **Conditions** (`conditions/<name>/agent.json`) are drop-in Kiro agent configs:
   one `baseline` and one or more `treatment`s that differ *only* by the
   augmentation, so any delta is attributable to it.
2. For each `task × condition × rep`, the runner writes the agent config into an
   isolated workspace and invokes `kiro-cli chat --no-interactive`.
3. Output is parsed for the answer, tool/MCP usage, credits, and latency.
4. Several **judge models** (the model under test is dropped automatically) score
   each answer against the task's answer key + rubric.
5. Results aggregate per **stratum** into a recommendation-shaped report:
   success, lift vs baseline, capability unlock, hallucination, overhead, and a
   paired McNemar test.

## Why stratify tasks

Measured lift is almost entirely a function of which tasks you pick, so Lift for
Kiro tags every task with a **stratum** — a label for *where* any lift should come
from (e.g. post-cutoff knowledge vs. things the model already knows). The report
is sliced by stratum, so the output is recommendation-shaped — "for stratum S,
augmentation A moves success X%→Y% at cost Z" — rather than one blended score
that hides where the augmentation actually helps.

Strata are **yours to define**: the set lives in `STRATA` (`harness/config.py`)
and each stratum gets a `tasks/<stratum>/README.md` describing its intent. The
included example defines six (listed under [the example](#included-example)
below); add, rename, or drop them to fit your own augmentation.

Whatever strata you use, pre-screen tasks for **discrimination** (~30–70%
baseline pass) before scaling — a stratum where the baseline already scores ~100%
has no room to show lift.

<a name="included-example"></a>
## Included example: the Agent Toolkit for AWS

Lift for Kiro ships one worked **example** — a demonstration of the harness, not a
rigorous study (the task set is deliberately tiny, so treat its numbers as
illustrative). It measures the Agent Toolkit for AWS and doubles as a template to
copy for your own augmentation. The example defines six strata:

| Stratum | What it tests | Typical baseline failure mode |
|---|---|---|
| `stable_public` | Well-documented, pre-cutoff knowledge | None — model already knows; expect ~0 lift (sanity check) |
| `recent` | Post-cutoff services / changed facts | Stale or omitted info |
| `correctness` | Knows roughly but errs / hallucinates | Plausible-but-wrong |
| `architecture` | "Given this usage pattern, what should I use?" advice | Habitual/older choices; misses better-fit options |
| `skills` | Procedural / SDK-correctness on multi-step workflows | Subtly wrong code (missing pagination, wrong patterns) |
| `api` | Deterministic facts answered by acting on live state (fixture-backed) | No access → must guess; **capability unlock** for tool-enabled conditions |

It ships a small, **discriminative** task set (2 tasks per stratum) and two
experiments.

### `agent-toolkit-lift` — the managed MCP

Three conditions — identical prompt and base tools, differing only by the added
capability — so any delta is attributable to the augmentation:

- `lift-baseline` — bare model, no tools (**baseline / reference**). Any
  account-specific or post-cutoff answer it gets right comes from training alone.
- `lift-baseline-native` — + Kiro's built-in **native tools**: web search/fetch
  **and** the AWS CLI (`use_aws`, read-only). The realistic developer who already
  has web access and the CLI. This is the comparison bar for the MCP.
- `lift-aws-mcp` — + the **Agent Toolkit for AWS** (including the AWS MCP Server)
  via `mcp-proxy-for-aws`: `documentation`, `recommend`, and `call_aws`. Connects 
  to the `us-east-1` endpoint. SigV4-authed, so it needs AWS credentials.

### `skills-lift` — bringing your own skill

Tests an **agent skill** rather than an MCP — the typical "I wrote a skill, does
it help?" workflow. The skill ships *with the condition* (copied into the run
workspace at `.kiro/skills/`):

- `lift-baseline` — no skill (reference).
- `lift-skill` — + the `aws-sdk-python-usage` skill provided locally (see
  `conditions/lift-skill/workspace/.kiro/skills/`). The augmentation is just
  files in the workspace.
- `lift-aws-mcp` — the Agent Toolkit, which can *discover* the same skill at
  runtime via `retrieve_skill` — a provided-vs-discovered contrast.

Between them the two experiments exercise both halves of the toolkit — its **MCP**
tools (`recent`, `architecture`, `api`) and its **Skills** (`skills`) — and show
the harness is augmentation-agnostic: a skill is just workspace files on a
condition.

## Test your own augmentation

Each condition is a directory `conditions/<name>/`:

```
conditions/<name>/
  agent.json     # the Kiro agent config (+ _lift metadata)
  workspace/     # optional: copied verbatim into each run workspace
    .kiro/skills/<name>/SKILL.md
    .kiro/steering/<file>.md
```

1. Add `conditions/<your-treatment>/agent.json` and keep `lift-baseline` as the
   reference. Encode the augmentation in the agent config:
   - **MCP server** → `mcpServers` block
   - **extra tools** (web, CLI, etc.) → `tools` / `allowedTools`
   - **persona / custom agent** → `prompt` and tool policy
   - **steering files / skills** → drop the files under
     `conditions/<name>/workspace/.kiro/` (they're copied into the run workspace
     before each run). The augmentation belongs to the condition, not a stratum.
   Keep prompt and base tools identical to the baseline so the only variable is
   the augmentation.
2. (Optional) Add tasks under `tasks/<stratum>/<id>/task.yaml`. If a stratum
   needs provisioned state, add `tasks/<stratum>/setup.sh` / `teardown.sh`.
3. Define `experiments/<name>.yaml` (model, conditions, judges, reps), then run
   and report.

## Layout

```
harness/        runner, parser, judge, experiment orchestrator, stats, CLI (no AWS deps)
conditions/<name>/agent.json       drop-in Kiro agent config (baseline / treatment = the augmentation)
conditions/<name>/workspace/       optional files copied into each run workspace (skills, steering, …)
tasks/<stratum>/README.md          what the stratum tests
tasks/<stratum>/<id>/task.yaml      prompt + answer key + rubric
tasks/<stratum>/setup.sh|teardown.sh   optional per-stratum fixture hooks
tasks/<stratum>/*                  any provisioning assets the hooks need live with the stratum
                                   (e.g. tasks/api/fixtures.cfn.yaml — a CloudFormation template)
experiments/    experiment definitions (model, conditions, judges, reps)
results/        JSONL output (one row per task x condition x rep); *.log gitignored
report/         generated markdown / JSON summaries
```

<a name="fixtures"></a>
### Fixtures (per-stratum setup / teardown)

A stratum may declare `setup.sh` / `teardown.sh`. The harness runs setup once
before the stratum's tasks and teardown once after (always, even on error),
passing `KIRO_LIFT_FIXTURE_OUT` (a path to write a JSON of outputs) and
`KIRO_LIFT_STRATUM`. Outputs written by setup are substituted into
`${Token}` placeholders in task prompts/answer keys at runtime. All
environment-specific logic lives in these scripts, keeping the harness generic —
including the cloud region, which the `api` hooks read from `AWS_REGION` /
`AWS_DEFAULT_REGION` (default `us-west-2`), not from the harness.
Any provisioning assets a stratum needs live with it: the `api` hooks
deploy/destroy `tasks/api/fixtures.cfn.yaml`.

#### Fixtures vs. account-gating are independent

Two concepts that are easy to conflate:

- **`requires_account`** (a per-task *gate*) — the task needs a live external
  account / credentials to run and may incur cost or side effects. It is
  provider-agnostic (AWS, Azure, Figma, GitHub, …); only our example `api`
  stratum happens to use AWS. Account-gated tasks are skipped unless you pass
  `--allow-account-tasks`.
- **Fixtures** (a per-stratum *provisioning* step) — `setup.sh`/`teardown.sh`
  prepare shared state for the stratum's tasks.

They are orthogonal — any combination is valid:

| | has fixture | no fixture |
|---|---|---|
| **requires_account** | `api`: `setup.sh` deploys real cloud resources the tasks query | a task that reads a pre-existing account, e.g. a Figma MCP task against your real Figma account — needs creds, provisions nothing |
| **no account** | a `setup.sh` that provisions local/free state (mock server, synthetic files) — runs by default, no flag | ordinary offline tasks (`stable_public`, `recent`, …) |

The gate filters *tasks*; fixtures bracket whichever *strata* still have tasks to
run. So a free local fixture runs without the flag, and an account-gated task
needs no fixture.

## Setup

**Prerequisites**
- Python 3.10+.
- [`kiro-cli`](https://kiro.dev/cli/) on your `PATH`, authenticated for headless
  use via a `KIRO_API_KEY` (Kiro Pro+). Every run shells out to
  `kiro-cli chat --no-interactive`.
- For the `api` stratum only: AWS credentials that can create/delete the fixture
  stack (see [Fixtures](#fixtures)). All other strata run offline.

**Install the harness** (creates an isolated venv the commands below use):

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

That's the whole dependency footprint — the harness itself only needs `pyyaml`;
`kiro-cli` and (optionally) the AWS CLI are external tools you provide.

## Usage

```bash
# from repo root, using the project venv
.venv/bin/python -m harness.cli validate                 # list tasks + conditions
.venv/bin/python -m harness.cli run experiments/smoke.yaml             # cheap pipeline check
.venv/bin/python -m harness.cli run experiments/agent-toolkit-lift.yaml   # the MCP worked example
.venv/bin/python -m harness.cli run experiments/skills-lift.yaml          # the skill worked example
# fixture-backed api stratum deploys/destroys cloud resources; needs creds + the flag:
.venv/bin/python -m harness.cli run experiments/api-lift.yaml --allow-account-tasks
.venv/bin/python -m harness.cli report results/agent-toolkit-lift.jsonl \
    --baseline lift-baseline --out report/agent-toolkit-lift.md
```

Account-requiring tasks (the `api` stratum) are skipped unless you pass
`--allow-account-tasks` and have credentials configured. That stratum's
`setup.sh` deploys fixtures (and `teardown.sh` deletes them) in the region from
your AWS environment (`AWS_REGION` / `AWS_DEFAULT_REGION`, default `us-west-2`).

### Model sweeps (one experiment, many models)

To compare augmentation lift across models, reuse the *same* experiment and
override the model under test with `--model`; results are written to a separate
file per model so runs don't collide:

```bash
.venv/bin/python -m harness.cli run experiments/agent-toolkit-lift.yaml --model glm-5
# -> results/agent-toolkit-lift-glm-5.jsonl
```

An experiment's `model` field is deliberately singular rather than a list. Each
experiment is one coherent statistical unit — a paired comparison of conditions
against the baseline with the model held fixed — and the single model under test
is what the harness drops from the judge panel to limit self-preference. Which
model(s) to run against is an orchestration concern, not part of the question, so
it lives in the `--model` override: that keeps the experiment file declarative
and reusable, gives one clean results file per model (cleanly resumable),
supports both parallel and sequential sweeps, and makes the cost of each run
explicit rather than silently multiplied.

To sweep several models, loop over `--model` yourself, e.g.:

```bash
for m in glm-5 claude-sonnet-4 gpt-5; do
  .venv/bin/python -m harness.cli run experiments/agent-toolkit-lift.yaml --model "$m" &
done; wait
```

### Quick iteration overrides

A few experiment fields can be overridden on the `run` command to probe a file
without editing it:

```bash
# fast smoke: one rep, a single stratum
.venv/bin/python -m harness.cli run experiments/agent-toolkit-lift.yaml \
    --reps 1 --task-filter recent --out results/scratch.jsonl

# re-run a single task
.venv/bin/python -m harness.cli run experiments/agent-toolkit-lift.yaml \
    --task-ids s3-vectors --no-resume --out results/scratch.jsonl
```

`--reps`, `--task-filter`, and `--task-ids` override the experiment's values
(alongside `--model`). The default results path is keyed only on the experiment
name (and model), so an ad-hoc override writes to the same file and resumes
existing rows — pair these with `--out` or `--no-resume` to keep the probe run
separate (the CLI warns if you don't). Scoring/definition fields
(`pass_threshold`, `conditions`) are intentionally not overridable: they define
the experiment, not the run.

## Metrics (per stratum)

- **Success / lift** — judge-pass rate, and Δ vs baseline on the same tasks (Wilson CI).
- **Capability-unlock rate** — tasks the baseline fails but the condition passes.
- **Hallucination rate** — fraction of judges flagging fabricated APIs/values.
- **Overhead** — mean credits (rate-adjusted cost proxy) and wall time.
- **Expected-tool-use** — did the treatment actually invoke the augmentation's tool.
- **McNemar exact p** — paired significance vs baseline.

`report` renders these as a markdown **per-stratum comparison** (conditions as
rows against the baseline reference, with unicode lift bars, hallucination Δ, and
cost relative to baseline) plus a computed best-per-stratum recommendation. Use
`--json` for the machine-readable summary.

## Telemetry source

Headless `kiro-cli chat --no-interactive` stdout provides, per run: the answer,
a `Credits: <n> • Time: <n>s` footer (cost + latency), and visible tool-use
lines (utilization). No raw token counts are emitted; credits is the more
decision-relevant cost metric anyway.

## Method notes

- Runs are stochastic → repeat (`reps`), reduce to per-task pass by majority.
- Pre-screen for *discriminative* tasks (30–70% baseline pass) before scaling.
- Augmentation adds a real cost floor; always weigh lift against overhead.
- Use judge models different from the model under test to limit self-preference
  (the harness drops the model under test from the judge panel automatically).

## Disclaimer

The sample code; software libraries; command line tools; proofs of concept; templates; or other related technology (including any of the foregoing that are provided by our personnel) is provided to you as AWS Content under the AWS Customer Agreement, or the relevant written agreement between you and AWS (whichever applies). **You should not use this AWS Content in your production accounts, or on production or other critical data.** You are responsible for testing, securing, and optimizing the AWS Content, such as sample code, as appropriate for production grade use based on your specific quality control practices and standards. Deploying AWS Content may incur AWS charges for creating or using AWS chargeable resources, such as running Amazon EC2 instances or using Amazon S3 storage.

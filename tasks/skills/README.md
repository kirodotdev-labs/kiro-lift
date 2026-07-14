# Stratum: `skills`

Procedural and SDK-correctness tasks where **agent skills** (curated step-by-step
instructions from the Agent Toolkit for AWS) should improve correctness. Skills
bridge the gap between what the model knows from training and the exact
sequences, patterns, and gotchas needed to complete multi-step AWS workflows
correctly.

- **Tests:** tasks the model gets subtly wrong without procedural guidance —
  SDK pagination, DynamoDB marshalling, CDK patterns, IAM policy construction,
  multi-step deployment workflows.
- **Expected baseline failure mode:** plausible-but-incorrect code — missing
  pagination, wrong marshalling format, deprecated patterns, skipped steps.
- **Expected lift from augmentation:** the skill provides the correct procedure
  and prevents the common mistake. The MCP condition can discover skills at
  runtime via `retrieve_skill`; a local-skills condition has them pre-installed.

No fixtures; no account required. Tasks test the *code/procedure* the agent
produces, not live AWS state.

## Conditions for this stratum

The `skills-lift.yaml` experiment compares:
- `lift-baseline` — no skill access; relies on training data alone (the reference).
- `lift-skill` — the skill is **provided locally**, shipped in the condition's
  `workspace/.kiro/skills/` and copied into the run workspace. This is the
  typical "I wrote a skill — does it help?" case.
- `lift-aws-mcp` — the Agent Toolkit for AWS **discovers** the skill at runtime
  via `search_documentation` (topic: `agent_skills`) + `retrieve_skill` — a
  provided-vs-discovered contrast.

## Authoring guidance

- Prompts should ask the agent to produce code or a step-by-step procedure.
- Answer keys should include the correct code/procedure AND the specific
  common mistake the skill corrects (so the judge knows what to penalize).
- `expects_tool: retrieve_skill|search_documentation` — credit any skill
  discovery (the `lift-skill` condition reads files instead, so it won't match;
  that's expected — the gain shows up in correctness, not tool-use).

#!/usr/bin/env bash
# kiro-lift: API-stratum fixture SETUP.
set -uo pipefail

# Region: the harness no longer injects this (it stays cloud-agnostic). Use the
# standard AWS env vars, falling back to a sensible default.
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
STACK="${KIRO_LIFT_STACK_NAME:-kiro-lift-fixtures}"
# Template lives alongside this hook (resolve relative to the script, not cwd).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$SCRIPT_DIR/fixtures.cfn.yaml"

echo "[api/setup] deploying stack '$STACK' in $REGION" >&2
if ! aws cloudformation deploy \
  --stack-name "$STACK" \
  --template-file "$TEMPLATE" \
  --capabilities CAPABILITY_NAMED_IAM \
  --region "$REGION" >&2; then
  echo "[api/setup] deploy FAILED — stack events:" >&2
  aws cloudformation describe-stack-events \
    --stack-name "$STACK" --region "$REGION" \
    --query "StackEvents[?ResourceStatus=='CREATE_FAILED'].[LogicalResourceId,ResourceStatusReason]" \
    --output text >&2 || true
  exit 1
fi

echo "[api/setup] reading stack outputs" >&2
OUTPUTS_JSON="$(aws cloudformation describe-stacks \
  --stack-name "$STACK" --region "$REGION" \
  --query 'Stacks[0].Outputs' --output json)"

# Convert [{OutputKey,OutputValue},...] -> {KEY: VALUE} for the harness.
OUTPUTS_JSON="$OUTPUTS_JSON" python3 - "$KIRO_LIFT_FIXTURE_OUT" <<'PY'
import json, os, sys
outs = json.loads(os.environ["OUTPUTS_JSON"]) or []
mapping = {o["OutputKey"]: o["OutputValue"] for o in outs}
with open(sys.argv[1], "w") as fh:
    json.dump(mapping, fh)
print("[api/setup] outputs: " + ", ".join(f"{k}={v}" for k, v in mapping.items()),
      file=sys.stderr)
PY

echo "[api/setup] done" >&2

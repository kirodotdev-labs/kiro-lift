#!/usr/bin/env bash
# kiro-lift: API-stratum fixture TEARDOWN.
set -euo pipefail

# Region: the harness no longer injects this (it stays cloud-agnostic). Use the
# standard AWS env vars, falling back to a sensible default.
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-west-2}}"
STACK="${KIRO_LIFT_STACK_NAME:-kiro-lift-fixtures}"

STATUS=$(aws cloudformation describe-stacks --stack-name "$STACK" --region "$REGION" \
  --query "Stacks[0].StackStatus" --output text 2>/dev/null || echo "DOES_NOT_EXIST")

if [[ "$STATUS" == "DOES_NOT_EXIST" ]]; then
  echo "[api/teardown] stack does not exist; nothing to do" >&2
  exit 0
fi

if [[ "$STATUS" == "ROLLBACK_COMPLETE" || "$STATUS" == "CREATE_FAILED" ]]; then
  echo "[api/teardown] stack in $STATUS — leaving for inspection; delete manually with:" >&2
  echo "  aws cloudformation delete-stack --stack-name $STACK --region $REGION" >&2
  exit 0
fi

echo "[api/teardown] deleting stack '$STACK' in $REGION" >&2
aws cloudformation delete-stack --stack-name "$STACK" --region "$REGION" >&2
if aws cloudformation wait stack-delete-complete \
     --stack-name "$STACK" --region "$REGION" >&2; then
  echo "[api/teardown] done" >&2
else
  echo "[api/teardown] WARNING: delete did not confirm; check the stack manually" >&2
fi

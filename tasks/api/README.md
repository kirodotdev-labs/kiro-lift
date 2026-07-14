# Stratum: `api`

Deterministic, fixture-backed questions that require calling AWS APIs to answer
(e.g. `call_aws`). Unlike account-counting questions, every answer here is a
hard-coded yes/no fact because the resources are provisioned by a known
CloudFormation template.

- **Tests:** can the agent inspect real resources and answer specific,
  non-yes/no questions — e.g. "list exactly which S3 actions role X has on
  bucket Y" (IAM), "which TCP/UDP ports can an instance in SG X reach on SG Y"
  and "enumerate all inbound rules on SG Y" (security groups). Specific
  enumeration answers can't be guessed the way a yes/no can.
- **Expected baseline failure mode:** the bare `lift-baseline` model has no
  account access, so it must guess and should fail. `lift-baseline-native` can
  query the account with the AWS CLI (`use_aws`).
- **Expected lift from augmentation:** capability unlock for the bare model, and
  a reliability/cost comparison for the rest — the Agent Toolkit's `call_aws`
  (`lift-aws-mcp`) versus the native AWS CLI both have account access, so any
  delta here is about correctness and overhead rather than raw capability.

## Fixtures (setup / teardown)

This stratum has hook scripts the harness runs around the stratum's tasks:

- `setup.sh` — deploys `tasks/api/fixtures.cfn.yaml` (IAM read-only
  role + S3 bucket; a VPC with `app`/`db` security groups where `db` allows TCP
  8080 from `app` and nothing on 22). It writes the stack Outputs as JSON to
  `$KIRO_LIFT_FIXTURE_OUT`; the harness substitutes them into `${PLACEHOLDER}`
  tokens in the task prompts/answer keys (e.g. `${DataBucket}`, `${AppSg}`).
  Token names match the stack's Output logical IDs (alphanumeric, no underscores).
- `teardown.sh` — deletes the stack. The harness always runs it after the
  stratum, even on error/interrupt.

## Requirements

- Run with `--allow-account-tasks` (tasks are `requires_account: true`).
- Valid AWS credentials on the default profile (`aws sts get-caller-identity`).
- Region comes from your AWS environment (`AWS_REGION` / `AWS_DEFAULT_REGION`,
  default `us-west-2`) — the hook scripts read it, not the harness.
- The Agent Toolkit's `call_aws` and the native AWS CLI both operate read-only
  (the toolkit's API surface and the `use_aws` read-only policy); tasks are
  strictly read-only (describe / simulate). Resource creation/deletion happens
  only in the hook scripts, never through an MCP or the CLI.

## Fixture permissions

The `setup.sh` / `teardown.sh` hooks provision and delete real infrastructure,
which needs broad permissions: CloudFormation on the `kiro-lift-fixtures*`
stack, IAM to create/delete the fixture role, EC2 to create/delete a VPC and
security groups, and S3 to create/delete the fixture data bucket.

The `api` stratum runs locally with your own AWS credentials, which must be able
to create and delete these resources. A policy matching what the hooks need:

- `cloudformation:*` on `arn:aws:cloudformation:*:<account>:stack/kiro-lift-fixtures*`
- `iam:CreateRole` / `DeleteRole` / `PutRolePolicy` / `DeleteRolePolicy` /
  `GetRole` / `GetRolePolicy` / `ListRolePolicies` / `ListAttachedRolePolicies` /
  `PassRole` / `SimulatePrincipalPolicy` / `TagRole` / `UntagRole` on
  `arn:aws:iam::<account>:role/kiro-lift-*`
- `s3:CreateBucket` / `DeleteBucket` / `PutBucketPublicAccessBlock` /
  `GetBucketLocation` on `arn:aws:s3:::kiro-lift-data-<account>`
- `ec2:CreateVpc` / `DeleteVpc` / `CreateSecurityGroup` / `DeleteSecurityGroup` /
  `AuthorizeSecurityGroupIngress` / `RevokeSecurityGroupIngress` /
  `DescribeVpcs` / `DescribeSecurityGroups` / `DescribeSecurityGroupRules` /
  `CreateTags` / `DeleteTags`

Scope these down to your environment as appropriate.

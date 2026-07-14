# Stratum: `correctness`

Topics the model knows roughly but tends to get subtly wrong or hallucinate.

- **Tests:** plausible-but-wrong failure modes (e.g. invalid IAM policy
  structure, query-vs-scan tradeoffs).
- **Expected baseline failure mode:** confident, plausible, incorrect answers.
- **Expected lift from augmentation:** moderate — authoritative sources correct
  the details and reduce hallucination.

No fixtures; no account required; no setup/teardown hooks.

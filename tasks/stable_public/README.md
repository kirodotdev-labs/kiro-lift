# Stratum: `stable_public`

Well-documented, pre-cutoff AWS knowledge the model should already know.

- **Tests:** stable services, basics, long-standing limits and APIs.
- **Expected baseline failure mode:** none — the bare model usually answers correctly.
- **Expected lift from augmentation:** ~0. This stratum is the consistency check
  that augmentation isn't changing answers it shouldn't.

No fixtures; no account required; no setup/teardown hooks.

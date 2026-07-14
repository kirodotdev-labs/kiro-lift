# Stratum: `architecture`

Architecture-advice tasks. Each prompt describes a **usage pattern only** and
asks "what should I use?" — it must never name or hint the target service.

- **Tests:** whether augmentation yields better, more current service
  recommendations (e.g. surfacing S3 Vectors, Aurora DSQL, S3 Express One Zone
  when they fit) instead of older/heavier defaults.
- **Expected baseline failure mode:** recommends habitual or pre-cutoff services;
  misses newer best-fit options.
- **Expected lift from augmentation:** comes from `recommend` /
  `search_documentation` surfacing current options.
- **Guardrail:** at least one task (`static-site-not-overbuilt`) rewards the
  *simple* answer, so a condition can't win just by always upselling the newest
  service.

Authoring rule: keep prompts neutral and pattern-shaped; never mention a
candidate service or the recommend tool. No fixtures; no account required.

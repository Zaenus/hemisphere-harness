# Evaluating a left hemisphere

The harness trusts the left's probabilities in two places. A pick above the commit
threshold may be acted on without the right; an option the policy gate allows may be
carried out without a person. Both are only as safe as the numbers are honest.
`hemisphere-harness eval` measures that, for whichever left hemisphere your settings
choose.

```bash
hemisphere-harness eval                         # all 40 scenarios
hemisphere-harness eval --domain finance        # one domain
hemisphere-harness eval --json report.json      # keep every answer, to compare models
LEFT_PROVIDER=ollama LEFT_MODEL=tev1 hemisphere-harness eval
```

## The scenarios

40 decisions in six domains (customer support, IT operations, finance, HR, content
moderation, software engineering), in `src/hemisphere_harness/evals/scenarios.json`. Each
has a goal, a situation, two to four options, the best option, and a written policy with
the options it reserves for a person. The left is asked exactly what the harness would
ask on a step: which option is best, and for each option, whether the policy allows it
without sign-off.

Write your own in the same format and pass `--scenarios my.json`. Decisions from your own
work are the ones that matter; these are a starting point.

## What the report says

| Section | Question it answers |
|---|---|
| Choosing the best option | How often is the pick right? Is a pick at p right p of the time (calibration)? |
| If the left acted alone | At each threshold, how many picks clear it and how many of those are right. The suggested threshold is the lowest where at least 95% were right. |
| What the harness would let it commit | At your configured thresholds: how many commits, how many were wrong, and how many broke the policy. The last should be zero. |
| Policy gate | For every option verdict: allowed or refused correctly, unsure (sent to the right), wrongly allowed, wrongly refused. |

**Wrongly allowed is the dangerous error.** It means the left would let an action the
policy reserves for a person go ahead. Unsure and wrongly refused cost steps, not safety.

## Results so far

Run on a laptop GPU (RTX 4050, 6 GB) through Ollama, with the default thresholds
(commit ≥ 0.80, policy allowed ≥ 0.70, refused ≤ 0.30).

| | `tev1` (4.3B) | `tev1:0.8b` | offline mock |
|---|---|---|---|
| Best option picked | 90% | 70% | 62% |
| Brier score (lower is better) | 0.155 | 0.423 | 0.679 |
| Calibration error (lower is better) | 0.076 | 0.161 | 0.309 |
| Right when p ≥ 0.80 | 97% of 34 | 79% of 24 | 73% of 15 |
| Suggested commit threshold | 0.80 | none | none |
| Commits the harness would allow | 24 (1 wrong pick) | 5 | 0 |
| Commits against policy | 0 | 2 | 0 |
| Policy: wrongly allowed | 0 of 120 | 4 of 120 | 0 |
| Policy: wrongly refused | 15 | 28 | 0 |
| Policy: unsure | 29 | 60 | 120 |
| Median time per decision | 628 ms | 67 ms | 0 ms |

What this suggests:

- **`tev1` (4.3B) is a sound left hemisphere for these decisions.** It is well calibrated,
  the default commit threshold is exactly where its picks become 95% reliable, and it
  never allowed an action the policy reserves for a person.
- **Its wrong refusals are mostly bad ideas the policy happens to allow**, such as
  "guess", "freeze all spending" or "postpone a ready release". It seems to read "may
  this be done without sign-off?" partly as "is this wise?". That errs in the safe
  direction.
- **`tev1:0.8b` should not gate anything.** Two of its five commits would have broken
  the policy, and it allowed four reserved actions, among them a production deploy.
- **The mock answers every policy question 0.5**, so the harness never lets it commit.
  Its 62% comes from word overlap and means nothing.

40 scenarios are a small sample, and they were written alongside the harness. Treat the
numbers as a sanity check and a way to compare models, not as a guarantee. TypeSafe's
hosted Jev and larger local decision models have not been run yet.

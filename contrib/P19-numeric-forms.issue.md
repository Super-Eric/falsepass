## What

A per-example probe, **P19 `numeric_forms`**: when the reference answer is a plain number, re-render it as a value-identical spelling and require full reward — `36` -> `36.0`, `0.5` -> `0.50`, `12000` -> `12,000`, `36` -> `+36`, `-5` -> `−5` (U+2212). Registered in `PER_EXAMPLE`, both README tables, `draft.py`, and `NEEDS_JUDGEMENT` next to P2: a prompt that demands one spelling may reject some forms on purpose, so this is a lead, not a verdict.

## Why

On 300 held-out GSM8K items a 0.6B RLVR run read **50.7%** under exact string matching and **63.0%** under numeric-equivalent matching. The runs also differ in prompt style, so treat the 12.3-point gap as an upper bound rather than a single-variable estimate; the direction is the same false-fail class that P2's default forms (whitespace, sentence-after, CRLF) cannot reach.

Audit against a first-pass strict grader (`1.0 if c.strip() == a else 0.0`) on two numeric items:

```
| P19 numeric_forms | trailing_zero       | full | 2 | 2 | 0 / 0 / 0 | expected full, got 0.0 |
| P19 numeric_forms | thousands_separator | full | 1 | 1 | 0 / 0 / 0 | expected full, got 0.0 |
| P19 numeric_forms | plus_sign           | full | 2 | 2 | 0 / 0 / 0 | expected full, got 0.0 |
```

`rewardlint` on the same grader independently reports a **95.7%** false-negative rate.

## Checks

- `uv run pytest -q` passes; `uvx ruff check .` clean.
- New test: a string-matching grader is flagged on at least two variants, a numeric-value grader clears every row; the strict-grader parametrisation now allows `{"P2", "P19"}`.
- Applied on a clean clone of `main` (97583c4), then `pytest -k p19` passes there.

## Patch

https://raw.githubusercontent.com/lindicaphxag-tech/falsepass/contrib/p19/contrib/p19-numeric-forms.unidiff.patch

It is a zero-context, five-file diff (+60/-2): `git apply --unidiff-zero p19-numeric-forms.unidiff.patch`. The file was downloaded back from that branch, byte-compared against the local original, and applied to a fresh clone before being linked here. (`contrib/p19-numeric-forms.patch` in the same folder is a superseded first upload that lost its context lines; ignore it.)

## Not touched

- Abstention is still priced like a wrong answer. Two runs of the same configuration and prompt style, differing only in seed, landed at **63.0%** and **46.7%** pass@1 with 115 of 300 items flipping, so a single-cell reading of the effect is not available. Treating "unfinished" as a third class did cut abstention to **11%** in that cell, and a paired recheck of the two cells gives p = 0.033, which the seed swing above undercuts. Happy to write this up as a spec for P20 instead of code, since it adds a judgement category rather than a false pass/fail.
- Fractions and percentages (`1/2` vs `0.5`, `25%` vs `0.25`) are out of scope: they need the prompt to say which form is allowed.

The paired counts behind the two numbers above are released under an exploratory label, with the exact test recomputed from discordant counts: `experiments/mathematics/verifier-reliability-pilot` in the anarres candidate branch.

Happy to redo this as a PR if you prefer that to an issue, or to narrow the scope (fewer forms, adapter-level instead of probe-level).

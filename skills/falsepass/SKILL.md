---
name: falsepass
description: Audit the reward code of an RL environment or eval task (verifiers / Prime Intellect Environments Hub, Inspect AI, HUD; published or local) for false passes and false fails with the falsepass CLI, confirm each lead, and write it up fairly. Use when asked to check, audit or harden an environment's grader, verifier or reward function.
---

# Audit an RL environment's grader with falsepass

falsepass scores completions whose correctness is known in advance with the environment's
own reward code and reports every disagreement. Its output is a list of leads. Your job is
to turn leads into findings a maintainer can act on, or to drop them.

## 1. Run it

Requires `uv` on the PATH. If `falsepass` is not installed, prefix every command with
`uvx` (`uvx falsepass init ...`), or install it once with `uv tool install falsepass`.

```bash
falsepass scan path/to/source --json      # optional first look, nothing installed
falsepass init owner/environment          # Hub environment; or ./folder, an Inspect task file,
                                          # inspect_evals/<task>, or a HUD environment folder
falsepass audit environment               # 25 tasks; add --search 120 for mutation search
falsepass results environment --json      # headline numbers and failing probes
```

Work in a scratch directory: `init` creates `audits/<name>/` in the current directory.
If `audit` stops at the control ("inconclusive", "needs a hand-written adapter"), the
environment is multi-turn, tool-use, sandboxed or judge-only in a way automatic mode
cannot script. Say so; `falsepass init --manual` writes an adapter skeleton to fill in.

## 2. Read the results

- `false_pass_ceiling` near the maximum: some known-bad completion was paid. Look at
  `failing` for which probe and variant.
- `lazy_policy_reward` above zero: a fixed strategy that never uses the answer earns
  that much on average. On a closed answer set (yes/no, A–J) subtract the share one fixed
  label earns under any correct grader before calling it a problem.
- `false_fail_rate`: correct answers refused. Read the variant names: rejecting
  `\boxed{6.}` or lower-case letters can be deliberate strictness.
- `static_hints` with `verdict: confirmed` point at the line that likely causes a failure.
- HUD tasks without `--answers`: only answer-free probes ran (P3, P4, A1, P10, P11). A1
  paying means a reply that lists everything earns reward; P1/P5/P9 need reference answers.

## 3. Confirm before you claim

For each lead:

1. `falsepass repro environment --probe P5` writes `repro_auto.py`. Run it twice from a
   clean virtualenv (the command is in its docstring). Identical output, or it does not count.
2. Open the reward code and find the line that causes it. Explain the mechanism in one
   or two sentences.
3. Decide defect or design. Partial credit in a similarity grader, a deliberately
   exploitable grader in an environment built to study reward hacking, and format
   strictness the prompt asks for are design choices: report them as observations.
4. Measure reach on the environment's own data: on how many tasks does it happen?
5. If you have a fix, check it keeps every correct answer that is paid today and run the
   repro again; with a patched copy, `falsepass verify-fix environment --source <folder>`
   shows which lines change.

## 4. Write it up

For the maintainer: environment and version, the exact completion, the score it got
and should get, the line of code and why, how many tasks are affected, a minimal repro,
and a suggested fix with what it was tested on. Say what the grader gets right.

Do not call an environment "broken", do not inflate how likely a model is to find the
hole, and do not file issues or pull requests on someone else's behalf without their
explicit go-ahead. A rating scale is in `docs/SEVERITY.md` of the falsepass repository.

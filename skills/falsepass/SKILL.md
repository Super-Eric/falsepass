---
name: falsepass
description: Audit the reward code of an RL environment or eval task (verifiers / Prime Intellect Environments Hub, OpenEnv / Hugging Face Spaces, Inspect AI, HUD, or plain reward functions written for TRL's GRPOTrainer; published or local) for false passes and false fails with the falsepass CLI, confirm each lead, and write it up fairly. Use when asked to check, audit or harden an environment's grader, verifier or reward function.
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
falsepass init owner/environment          # Hub environment; or rewards.py --dataset d.jsonl (TRL-style reward
                                          # functions), hf:owner/space (an OpenEnv Space), ./folder,
                                          # an Inspect task file, inspect_evals/<task>, or a HUD environment folder
falsepass audit environment               # 25 tasks; add --search 120 for mutation search
falsepass results environment --json      # headline numbers and failing probes
```

Work in a scratch directory: `init` creates `audits/<name>/` in the current directory.
If `audit` stops at the control ("inconclusive", "needs a hand-written adapter"), the
environment is multi-turn, tool-use, sandboxed or judge-only in a way automatic mode
cannot script. Say so, then write policies: `falsepass init <target> --policies --force`
writes a skeleton (a solver, an honest loser, and exploit policies that never use the
answer); see "Games, simulations and tool tasks" on the agents page of the docs. For a
Q&A environment whose answer format can't be inferred, `--manual` writes a simpler one.

## 2. Read the results

- `false_pass_ceiling` near the maximum: some known-bad completion was paid. Look at
  `failing` for which probe and variant.
- `lazy_policy_reward` above zero: a fixed strategy that never uses the answer earns
  that much on average. On a closed answer set (yes/no, A–J) subtract the share one fixed
  label earns under any correct grader before calling it a problem.
- `false_fail_rate`: correct answers refused. Read the variant names: rejecting
  `\boxed{6.}` or lower-case letters can be deliberate strictness.
- `static_hints` with `verdict: confirmed` point at the line that likely causes a failure.
- OpenEnv: `load_notes` say how tasks were picked and which action fields were filled with a
  constant; `audits/<env>/tasks.json` (reset keyword sets, each may carry an `answer`) pins
  the tasks. O1 rows are a fixed strategy, read their mean (the lazy-policy reward), not
  single rows; an O2 row that fails means a self-reported field moves the reward.
- HUD and OpenEnv tasks without `--answers`: only answer-free probes ran (P3, P4, A1, P10, P11). A1
  paying means a reply that lists everything earns reward; P1/P5/P9 need reference answers.
- Each failing entry shows the start and the end of one completion (the answer is
  usually at the end). `falsepass results environment --completion <completion_id>`
  prints it in full.
- Through MCP, an audit runs in the background: if `falsepass_audit` returns
  `"status": "running"`, call `falsepass_audit_status` until it finishes.

## 3. Confirm before you claim

For each lead:

1. `falsepass repro environment --probe P5` writes `repro_auto.py`. Run it twice from a
   clean virtualenv (the command is in its docstring), or add `--run` to have falsepass do
   that and compare. Identical output, or it does not count.
2. Open the reward code and find the line that causes it. Explain the mechanism in one
   or two sentences.
3. Decide defect or design. Partial credit in a similarity grader, a deliberately
   exploitable grader in an environment built to study reward hacking, and format
   strictness the prompt asks for are design choices: report them as observations.
4. Measure reach on the environment's own data: on how many tasks does it happen?
   `falsepass reach environment --probe P15/denied` counts one probe row on every task
   (`--tasks N` for a sample), with the reference as a control. To count your own
   completion, `--template '\boxed{{answer} \text{ is wrong}}'` fills `{answer}`, `{wrong}`,
   `{answer_in_format}`, `{wrong_in_format}` per task; no counting script needed.
5. If you have a fix, check it keeps every correct answer that is paid today and run the
   repro again; with a patched copy (of the audit's `src/` or the package),
   `falsepass verify-fix environment --source <folder> --known-good` shows which lines
   change and which correct answers, on every task, the patch stops paying. It reruns the
   repro only; `falsepass audit environment --source <folder>` runs every probe on the patch.

## 4. Write it up

For the maintainer: environment and version, the exact completion, the score it got
and should get, the line of code and why, how many tasks are affected, a minimal repro,
and a suggested fix with what it was tested on. Say what the grader gets right.

Do not call an environment "broken", do not inflate how likely a model is to find the
hole, and do not file issues or pull requests on someone else's behalf without their
explicit go-ahead. A rating scale is in `docs/SEVERITY.md` of the falsepass repository.

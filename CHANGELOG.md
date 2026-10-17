# Changelog

## Unreleased

- `falsepass reach <env> --template TEXT` (MCP `falsepass_reach` with `templates`): count a
  completion of your own on every task or a sample, without writing a script. `{answer}`,
  `{wrong}`, `{answer_in_format}` and `{wrong_in_format}` are filled per task; other braces
  stay, so LaTeX can be typed as is. Each template is a row `U1/t1`, `t2`, ... with the text
  it became on the first task. `--expect full` counts where a template is not paid in full.
  `--probe` is no longer required when a template is given.
- P14 (the answer wrapper named in prose) now also runs when the dataset stores the whole
  block, wrapper included, as the reference (for example `<answer>…</answer>` with a bare
  answer format). Before, such environments got no P14 rows.
- Environments pinned to verifiers 0.1.8 or earlier (no `vf.Client`) can be audited
  multi-turn: the scripted model is an OpenAI-style client there, and the rubric gets the
  semaphore those releases ask for. Generated repro scripts do the same. Before, the run
  stopped with `cannot import name 'Response' from 'verifiers.types'`.
- `falsepass init` reads the package's `Requires-Python` and writes `python = "3.13"` (or
  whichever version fits) to env.toml when 3.12 is ruled out; audit, repro and verify-fix
  use it. Before, such packages failed to resolve.
- `falsepass init` honours a verifiers ceiling: `>=0.1.14,<0.2` installs the newest release
  under 0.2 (from PyPI's list, or the floor offline) instead of the default 0.3.1, which
  could not resolve.
- `falsepass init` picks the module named after the package when a wheel ships several
  top-level modules (`cortifybench_env` for `cortifybench-env`), as verifiers does. Before,
  it took the first in alphabetical order.

## 0.7.0 — 2026-10-17

- `falsepass reach <env> --probe P15/denied [--tasks N]` (MCP `falsepass_reach`): after an
  audit, count how many tasks one failing probe row reaches, on every task or a sample.
  Only the rows asked for are scored, plus the reference (P1) as a control; the counts,
  failing task keys and three failing completions go to `audits/<env>/reach/`, and the
  stored audit is not touched. Per-example probes only. Asking for a row that does not
  exist lists the rows that do. The MCP tool runs in the background like an audit.
- `falsepass repro <env> --run` (MCP `falsepass_repro` with `run=true`): write the script,
  run it twice, each in a fresh `uv run --isolated` virtualenv, compare stdout and store the
  first run as `repro_auto.output.txt`, headed by the command and whether the second run
  was identical (the file `verify-fix` compares against). Reports how many cases still fail.
  `--script repro.py` runs a hand-written script the same way. Runs that differ exit 1.
- A known-bad row that said exactly what a known-good row said on the same task (a policy's
  "unsorted echo" on a row that is already sorted, a wrong answer that happens to equal the
  reference) is no longer counted as a false pass: it is marked `same_as_good`, passed, and
  left out of the ceiling and the lazy-policy reward; the summary says how many there were.
- The results digest says why a failing row has no `completion_id` (P10's 2 MB reply is kept
  as a preview only). The `--policies` skeleton says what an `Example` holds.
- `verify-fix --known-good [TASKS]`: also score every correct-answer row (P1, P2, P14) on
  every task (or a sample) with the release and with the patch, and list the tasks whose
  correct answer the patch stops paying. `reach --probe known-good` is the same count for
  one source; `reach --source` takes a patched copy of `src/` the way `verify-fix` does.
- Installed Inspect tasks get a `src/` too: the audit copies the task's own folder and the
  modules of the scorers it uses, so the static scan and a reader see the grading code.
  Two new static rules, both documented inspect_ai behaviour that a report should treat
  as a design choice unless the task asks for more: `inspect_match_end` (`match()` reads
  only the end of the output, or with `numeric=True` its last number, so
  `100 (this is wrong)` matches 100) and `inspect_choice_lenient` (`choice()` counts
  `ANSWER: A (this is wrong)` as A). Both are confirmed by P15.
- Progress lines in `job.log` are capped at about 100 per stage.
- From a newcomer trial (an agent using only the docs):
  - The lazy-policy reward again averages over every task, including those where a
    fixed strategy happens to be right. A known-bad row whose last reply equals a
    known-good one (but said other things first) is labelled `final_as_good`; when the
    ceiling rests on one, the headline says so and gives the ceiling without them.
  - `reach` counts every task with a hand-written adapter too (its `load` returns at most
    the rows there are; the `--manual` skeleton now does), and reports `all_tasks` when
    everything was counted. Rows come back in the order asked for.
  - P15 adds the reversed orders (`false_equation_reversed`, `either_or_reversed`,
    `either_or_latex_reversed`): a grader that keeps one side pays only one order.
  - `init` on a target that is already set up returns the existing audit instead of an
    error (a different target under the same name still needs `--force`).
  - MCP `falsepass_verify_fix`. Repro scripts print rewards with six decimals (a reward of
    0.00002 printed as 0.000). `repro-run.json` paths are absolute. Errors from reach and
    repro jobs no longer end with the audit's list of common causes.

## 0.6.0 — 2026-10-16

- `falsepass.policies`: ready-made known-bad policies for games, simulations and tool
  tasks (`after_the_end`, `keeps_talking`, `never_acts`) and `special_numbers()`, which
  puts NaN, Infinity and negative values into an accepted action. The `--policies`
  skeleton uses them, so an agent fills in a few messages instead of writing policies.
- New probe T8 for multi-turn environments: answer wrong, then repeat what the
  environment gives away after the attempt ("The word was …"). It never uses the
  reference, so any reward is a false pass; the row also says when the episode ran on
  after the environment announced its end. The run is kept as an episode, so `repro`
  replays it.
- Faster audits: single-turn rewards are scored on up to 8 tasks at once (`--jobs N`,
  `--jobs 1` for one at a time), and so are the slower probes and the rewardlint corpus.
  Results, their order and `completions.json` are the same as before. It helps most where
  each call waits on a subprocess, e.g. a v1 task whose reward runs a uv script (225 s →
  61 s on one such audit). Only scorers that build their own state per call run
  concurrently; multi-turn rollouts, tool-mode policies and Inspect / HUD tasks stay one
  at a time.
- `verify-fix` works in one step from what `repro` writes: it uses `repro.py`, else
  `repro_auto.py`; with no stored output it runs the unpatched script once and stores it
  as the baseline; `--source` may be a patched copy of the audit's `src/` (no
  `pyproject.toml`), laid over the released package via `PYTHONPATH`. Missing files give
  one-line errors. The agents page has the fix-checking step.
- `repro` prints the exact command to run the script (absolute path).
- The MCP server reports its version in `serverInfo`.

## 0.5.0 — 2026-10-16

From a second run as a new agent, this time on a word game, a customer-service
simulation, a tool task and a long-conversation benchmark.

- Policies: `falsepass init <target> --policies` (MCP: `policies=true`) writes an adapter
  skeleton for games, simulations and tool tasks: a solver, an honest loser and exploit
  policies, run through the environment's own rollout loop. The docs have a new section,
  "Games, simulations and tool tasks", on the agents page.
- A policy's episode is kept: what it said (replayable) and the whole conversation.
  `results --completion` / `falsepass_completion` show it, the digest previews it, and
  `falsepass repro` replays the policy's turns as a fixed script, so a lead found by a
  policy reproduces without falsepass.
- Inconclusive runs (automatic mode could not tell a correct reply from an empty one) no
  longer print a headline or a failing list that mean nothing: the digest says
  `"status": "inconclusive"`, lists `facts_that_hold` (rows built without the answer) and
  says what to do next. Other digests list at most 25 failing variants.
- MCP: command output is no longer cut at 20,000 characters, which broke the JSON of
  large digests (`JSONDecodeError` from `falsepass_results`). `falsepass_init` returns an
  MCP-style next step.
- When the reward code raises, the summary table shows the exception, and digest rows
  carry `errors`.
- `init --force` keeps a hand-written adapter.py; `audit --help` lists the probe options
  (`-n`, `--search`, `--fail-on`, ...); repro scripts say to compare stdout only.

## 0.4.1 — 2026-10-16

- Listed in the official MCP Registry as `io.github.Super-Eric/falsepass` (`server.json`;
  each release publishes it from GitHub Actions). `uvx falsepass mcp` now works without
  the `mcp` extra: the server restarts itself through `uvx --from "falsepass[mcp]"`.
- New static rule `math_verify_whole_slot`: the answer slot goes to math_verify's
  `parse()` whole, so a denial or a false equation inside it is paid (confirmed by P15).
  On the sources of the environments audited so far it adds two hints, both real.
- The results digest is smaller (about a fifth): `meta` and `static_hints` keep what an
  agent needs, and `failing` lists false passes first, most consistent on top.

## 0.4.0 — 2026-10-16

Fixes from running falsepass the way a new coding agent would, from the docs alone,
through the CLI and the MCP server, on a Hub environment, an Inspect task and a HUD
environment.

- MCP: a results digest that held a lone surrogate (from the P10 probe) could not be
  sent, so `falsepass_audit` and `falsepass_results` failed with no reason on some
  environments. Text is now escaped before it is sent.
- MCP: failures are tool errors (`isError`) whose text gives the reason and the next
  step, instead of a successful reply with `exit_code: 1` inside.
- MCP: `falsepass_init` takes every option the CLI has (`version`, `framework`, `task`,
  `answers`, `extra`, `manual`, `force`) and returns the audit name as a field
  (`init --json` on the CLI). New `falsepass_completion` returns one completion in full.
  Tool descriptions no longer carry source indentation.
- Digest and draft: the example completion shows its start and its end, where the answer
  usually is, plus a `completion_id`; `falsepass results <env> --completion <id>` prints
  it in full. Drafts break the observed scores down by variant.
- Audits print progress per example, which the MCP status shows while an audit runs.
- Names are forgiving (`arc_easy` finds `arc-easy`), and a missing or unfinished audit
  gets a message that lists the audits that exist, instead of a traceback or advice that
  can't work. `init --force` rewrites an existing audit's env.toml (e.g. to add HUD answers).
- A1 (answer-free sprays) runs in every mode, plus an `all_labels` variant on closed
  answer sets, so the lazy-policy number no longer drops when HUD answers are given. The
  dataset check no longer reports empty answers when none were given.
- The HUD answers file format is documented in the guide and on the agents page.

- MCP: audits run in the background. `falsepass_audit` waits up to `wait` seconds (default
  45) and returns the results or `{"status": "running", ...}`; the new
  `falsepass_audit_status` waits for it again. A client's tool-call timeout no longer cuts
  an audit off, a second call doesn't start a duplicate, and a failed audit returns its
  log tail with a hint. State in `audits/<env>/job.json`, output in `job.log`.

- Docs, website, skill and MCP instructions install from PyPI (`uvx falsepass`,
  `uvx --from "falsepass[mcp]" falsepass mcp`); a PyPI version badge.

## 0.3.1 — 2026-10-16

- First release on PyPI: `pip install falsepass`, `uvx falsepass`. README links are
  absolute so they work on the PyPI page.

- Website at https://super-eric.github.io/falsepass/ (English and Chinese), built from
  this repository by `site/build.py` and deployed by `.github/workflows/pages.yml`. The
  docs pages are rendered from the README, `docs/` and the skill; every page is also
  served as Markdown, indexed by `llms.txt` and `llms-full.txt`. New page for agents:
  MCP setup for several clients, the tool list and the results digest.
- First write-up: "Your reward function is an assertion" (`site/content/blog/`).

## 0.3.0 — 2026-10-14

- **Inspect AI** tasks: `falsepass init path/to/task.py`, a project folder that depends on
  inspect-ai, or a registry name (`falsepass init inspect_evals/gsm8k`). Each reply runs
  through the task's own solver chain with a scripted model (so templates and
  `multiple_choice()` parsing are the task's code), then through its scorers.
- **HUD** environments: `falsepass init path/to/env-folder`. Each reply is graded by the
  task template, driven the way HUD's server drives it. HUD tasks carry no reference
  answer, so by default only answer-free probes run (new probe A1: replies that list every
  small integer, every letter, yes/no words or the prompt's words); `--answers file.json`
  supplies references and enables every probe.
- Static rules for framework substring graders (Inspect `includes()`, HUD `contains*()`)
  and for `x in answer` after `answer = yield` in a HUD template.
- `falsepass repro` writes stand-alone scripts for Inspect and HUD audits too.
- MCP: works with the `mcp` package 1.x and 2.x (both tested in CI).
- Examples: `examples/inspect-toy/`, `examples/hud-toy/` (each with a planted defect).
- README: badges, a recording of `falsepass demo`, a real example, and the demo's
  output (`examples/toy-env/summary.md`). Chinese README.

## 0.2.0 — 2026-10-14

- Local environments: `falsepass init ./folder` audits an environment from its
  `pyproject.toml` before it is published; the folder is reinstalled on every run.
- Runs when installed from git or a wheel (`uvx --from git+…`), not only from a checkout.
- `falsepass demo`: a bundled toy environment with one planted defect.
- `falsepass results [--json]` and `falsepass scan --json` for scripts and agents;
  output formats in `docs/OUTPUT.md`.
- `falsepass mcp`: the commands as MCP tools (`pip install 'falsepass[mcp]'`).
- GitHub Action (`uses: Super-Eric/falsepass@main`) for environment repositories.
- Agent skill in `skills/falsepass/`.
- verifiers pin: a declared floor above 0.3.1 (e.g. `>=0.4.0`) is now used instead of
  0.3.1, and `verify-fix` raises the pin when the patched source needs a newer verifiers.
- `--version`.

## 0.1.0 — 2026-10-14

First public release.

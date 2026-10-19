# Changelog

## Unreleased

- **v1 tool and multi-turn tasksets are played as whole episodes.** A `verifiers.v1` taskset
  with tool servers, its own `Env` episode loop or a scripted user is now run through its
  own loop, as `vf-eval` runs it (harness, tool servers, scripted user, stop conditions,
  rewards), against a local endpoint on 127.0.0.1 that answers with the policy
  (`falsepass.v1_episodes.V1EpisodeScorer`). Before, automatic mode scored a single reply
  against such a taskset, or stopped. Automatic mode now plays T5 and T6 this way, after one
  trial episode that stops with a one-line reason when the taskset cannot run here;
  `init --policies` on a v1 taskset writes a v1 skeleton (solver as the control,
  `refusing_is_right`, exploits) instead of refusing; `repro` replays each recorded episode,
  tool calls included, with verifiers alone (loader kind `v1-episode`). env.toml takes
  `runtime` (`subprocess` by default, `docker`) and `vf_args`. `examples/v1-tool-toy/` has a
  planted defect.
- T5/T6 rows on a task where refusing is the right answer are marked `no_control` and left
  out of the ceiling: declared by an adapter (`refusal_tasks`), or inferred when "Done." with
  no tool call earns the maximum on that task but not on every task. The summary and the
  headline count them. When doing nothing earns the maximum everywhere, it stays a false pass.
- New T6 policy `claims_success_without_any_tool_call`.
- A local environment folder is installed editable in the audit's virtualenv: uv cached the
  build of a folder and did not notice edits to its code, so a run could test an older
  version of the folder than the one on disk.
- Tool-mode audits no longer report the empty-reference hygiene row (tool tasks carry no
  reference), and v1 episode audits no longer report identical prompts (the task is its
  world, not its prompt). A known-bad tool episode is no longer compared with a known-good
  one by its last words alone.

## 0.10.0 — 2026-10-19

- **Reward functions without an environment**: `falsepass init rewards.py --dataset data.jsonl`
  (or a module name such as `trl.rewards` with `--framework reward`) audits functions written
  for TRL's `GRPOTrainer`. Each is called the way the trainer calls it (every dataset column as
  a keyword argument, `prompts`, `completions`, `completion_ids`, `trainer_state`,
  `log_extra`, `log_metric`; one completion per call; message lists when the prompts are
  conversations or a function reads `completion[0]["content"]`), and the reward is their
  weighted sum (`--func`, `--weight`), `None` and NaN counting as nothing. Datasets: a
  JSONL/JSON/CSV/Parquet file, `hf:owner/name` (streamed), or `call:function` in the file,
  with `--map` for a row mapper. The reference column is the one a function names, else the
  usual names (`--answer-column`); a worked solution gives the probes its final answer. The
  file's third-party imports are installed for the audit. Repro scripts inline the rows they
  need; `verify-fix --source` takes a patched copy of the file. MCP `falsepass_init` gains
  `dataset`, `functions`, `weights`, `split`, `answer_column`, `prompt_column`.
  `examples/reward-toy/` has a planted defect.
- From the seventh dogfood round (reward functions: TRL's built-ins, open-r1, a tutorial-style
  GSM8K file): the answer format is inferred from the markup the reward functions themselves
  look for, not only the prompt, so `max_reward` and the lazy-policy reward are what a
  well-formatted reply earns (before, three of three targets reported a maximum below the true
  one, and format-only payouts were missed); `<reasoning>` and `<thinking>` work as reasoning
  tags like `<think>`, and the answer tag on lines of its own is a candidate format; `reach`
  counts in the audit's own format and against its maximum, reports each reward term's mean
  (`mean_terms`) and the rows every function skipped (`skipped`); the audit and reach processes
  exit once their results are written (a streamed Hub dataset kept one alive); a column of
  worked solutions without a marked final answer gives way to a short answer column for the
  probes, with a note; a file with relative imports is caught at `init`, which names the
  module form, and `--path DIR` (MCP `path`) imports a package from a folder instead of
  installing it; module targets get distinct names (`trl-rewards`, `open-r1-rewards`); the
  versions of the graders' own libraries are recorded in the results; hints inside reward
  functions that were not selected are dropped; a wrong answer keeps the kind of the reference
  (a number, not "Yes"); MCP `falsepass_init` gains `dataset_config`, `map` and `path`; the
  agents page has a twenty-line MCP client.
- Reward functions summed with a format reward: when a wrong answer in the right format earns
  the same amount on every task tried (the format terms), that amount is the floor other
  known-bad rows are judged above; P6 and the lazy-policy reward still show it. Before, every
  formatted wrong reply failed and buried the rows paid for more (a denial inside the box).
  When the reference earns different amounts on different tasks, `max_reward` is the most it
  earns, not the average. The answer tag with a box inside (`<answer>\\boxed{...}</answer>`)
  is a candidate format. A failed import of a `--path` package lists every missing import of
  the package at once, with the versions its own setup.py or pyproject.toml pins (extras
  dropped).
- `init --policies` on a v1 taskset says that the policy skeleton is for v0 environments and
  names the routes that work (`--manual`, or verifiers' `vf-eval` with a local stand-in model
  serving scripted policies, documented on the agents page), instead of writing a skeleton
  that fails under verifiers 0.4.
- New probe P17 (JSON answers): one short string field (a label, a date, an ID) followed by
  "(this is wrong)" everywhere it occurs, one field at a time; it must earn less than the
  reference (`expected: below_reference`). Found a grader that reads only the first ten
  characters of a date field on 10/10 tasks, which no other probe showed.
- `init --manual` lists the package's task-data fields (`verdad: list[dict]`, ...) and `--json`
  returns them as `task_data_fields`: where the expected values are, for `render()`.
- The draft shows the shortest failing completion of a candidate (a 2 KB preview hid the
  defect), and splits P11 by variant in "Checks that passed"; a hand-written adapter no longer
  leaves `__pycache__/` in the audit folder.
- The static scan treats a function that takes `completions` as a reward term, and knows
  `solution`, `ground_truth`, `target`, `label` as reference names.
- When the reference earns clearly different amounts on different tasks (a clamp at 0.99 on
  one, the first of two steps on another), correct-answer variants (P2, P14, T7) are judged
  against the reference's own reward on their task instead of the average, and the note says
  so; P1 still shows the tasks where a full answer earns less. Before, a two-task OpenEnv
  audit reported a false-fail rate of 0.5 that was only this averaging.
- Reward functions that return None for the reference itself (TRL's `accuracy_reward` on a gold
  it cannot parse) mark tasks the trainer skips: those are left out of the run, with a note.
- rewardlint's reference swap is skipped for HUD and OpenEnv audits, where the expected answer
  lives inside the environment (its numbers there were meaningless).

## 0.9.0 — 2026-10-18

- **OpenEnv** (Meta / Hugging Face) environments can be audited: `falsepass init hf:owner/space`
  clones the Space next to the audit, `init ./folder` takes a local one (an `openenv.yaml`).
  The audit installs the Space's own requirements plus `openenv` and drives the environment
  class in-process, as a session on its server does (`reset()` to the task, `step()` with the
  reply as the action): no Docker, no port. Tasks come from `audits/<name>/tasks.json` (reset
  keyword sets, each may carry an `answer`), the Task API when implemented, or repeated
  `reset()` calls; an environment that draws a task at random is reset until the recorded one
  returns. A plain reply goes into the action's free-text field, other required fields are
  filled with constants, and a reply that is a JSON object with the action's field names is
  the action. Two probes for structured actions: O1 plays every value of a decision field (a
  fixed strategy, `expected: lazy`, read through the lazy-policy reward) and O2 moves a
  self-reported field (`confidence`, `score`) between its minimum and maximum. Generated repro
  scripts, `verify-fix --source` and `reach` work for these audits; `examples/openenv-toy/` has
  a planted defect. Not covered: MCP tool environments and hand-written servers without an
  `Environment` class (they stop with a message).
- From the sixth dogfood round (four Spaces): the environment class is read from the served
  app itself (what `create_app` was given), so a `_build_app()` wrapper no longer passes for
  the environment; `init --env-class` / `--action-class` (MCP `env_class`, `action_class`) name
  them by hand; `init --without PKG` (MCP `without`) drops one of the environment's own
  requirements, and pins of openenv's own dependencies (`fastapi==0.115.2`) are relaxed at
  `init` so the folder resolves next to the openenv release; a decision spelled as text
  (`action_type: str`) is no longer taken for the free-text field; references an environment
  reveals only after a step (`correct_answer`) are read at load time; a run in which an empty
  reply and a sentence score the same on every task says so in its note instead of posing as
  a result; generated repro scripts carry the audit's reset retries; `repro --run` counts
  cases that raised in both runs and does not call them reproduced; the draft says "no
  control was possible" rather than "the control failed" for an answer-free run.
- `falsepass discover --hub hf` (MCP `falsepass_discover(hub="hf")`) lists OpenEnv Spaces on
  the Hugging Face Hub updated within `--days`, most liked first, with the `hf:owner/space`
  target for `init`; the public listing only, nothing is cloned.
- `expected` gains the value `lazy`: a fixed strategy that is right on some tasks by chance;
  such rows never fail, only their mean (the lazy-policy reward) is read.
- Static rule `reference_substring_match` also recognises `expected in action.answer`.
- `falsepass init` keeps a Hub package's data files (JSON, JSONL, CSV, TSV, YAML, text; up to
  8 MB each and 32 MB in all) in `audits/<env>/src/` next to the source, so a reviewer can read
  the task bank the grader compares against and a patched copy of `src/` carries its own data.
- For a v1 taskset, `init` pins the newest verifiers release the package allows (`>=0.3` gives
  0.4.0 today) instead of the 0.3.1 default, which a package built against 0.4 may not import
  under; v0 environments keep the default. Local folders are classified too, so `init --json`
  reports `automatic_likely` for them as well.
- Two static rules: `empty_component_scores_zero` (a per-item average that returns 0.0 when
  there are no items, so a component emptied on purpose counts as a failure) and
  `unreferenced_prompt_constant` (a module-level `*_PROMPT` string nothing in the package reads).
- MCP: `falsepass_draft` (the report draft) and `falsepass_upstream` (the source repository
  and a file comparison), which were CLI-only.
- `falsepass --version` names the commit (`0.8.0+abc1234`, `.dirty` with local changes) when
  run from a git checkout, so a log tells a release from a development build.
- Docs: `verify-fix` reruns the repro script only; `audit --source` runs the whole probe
  suite on a patch.

## 0.8.0 — 2026-10-17

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
- New probe P16 (JSON answers): the reference with every free-text value of two words or
  more replaced by the prompt text; labels, IDs, numbers and values quoted verbatim from
  the prompt are kept. A grader that pays word overlap with phrases the prompt already
  contains pays this like the reference. Expected: (close to) nothing.
- A v1 package whose Taskset sits in a submodule (`pkg/taskset.py`) and is not exported
  at the top level is found and audited, with a note that verifiers' own loader cannot
  load it by id (worth telling the maintainer).
- `falsepass init` no longer says "no adapter needed" for every Hub package: when the
  source shows no reference answer, or a sandbox, tools, judge or API key, it says the
  automatic run may stop and names the flags to use. `init --json` gains
  `automatic_likely`.
- `falsepass init --manual` writes a v1 skeleton (`taskset_class`, `V1TaskScorer`) for a
  v1 taskset; it wrote the v0 one (`vf.load_environment`) for every package.
- `falsepass audit` prints its own stop messages (no reference answers, a failed control,
  a container task) as one line on stderr, without a Python traceback.
- Hand-written adapters: the `--manual` skeletons declare a `loader`, so `falsepass repro`
  works for them; the v1 skeleton loads its taskset with `falsepass.auto.open_taskset`, which
  also finds a Taskset in a submodule (and so does the generated v1 repro script). An
  adapter that does not override `equivalent_forms` now gets three P2 forms (whitespace, a
  sentence after, CRLF) instead of none. The agents page gains "When the reference is not
  a reply" (what `Example.answer` and `raw` are, which one the scorer reads, `loader`).
- An inconclusive run whose `answer` field is a JSON object says it may be a grading record
  and points to `--manual`; the digest's `next` names both `--manual` (Q&A) and `--policies`
  (games, simulations, tool tasks) instead of policies only.
- MCP: two jobs started on one environment at the same moment (a client that starts a
  server per call) no longer overwrite each other's job.json: starts are serialised by a
  file lock and job.json is replaced atomically.
- `verify-fix` with a patched copy of `src/` (an overlay) fills in the package's data files
  from the release wheel; `init` copies only source text, so a JSON task bank was missing
  and the patched run failed. A stored command that names its script by a relative path now
  runs from any directory (an absolute audit path failed with "can't open file").
- `reach` rows gain `not_applicable` (tasks where the probe writes no such completion), and
  `same_as_good` is documented. `init --json` gains `automatic_unlikely_because`, and the MCP
  `next` then says to use `manual` or `policies`. `repro` without a loader prints one line,
  not a traceback; job log tails drop finished progress bars.
- "no reference answers found" now names the command to run next (`init <id> --force
  --manual`, or `--policies` for games, simulations and tool tasks).

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

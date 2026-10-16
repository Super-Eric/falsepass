# Changelog

## Unreleased

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
  `--jobs 1` for one at a time). Results, their order and `completions.json` are the
  same as before. Only scorers that build their own state per call run concurrently;
  multi-turn rollouts, tool-mode policies and Inspect / HUD tasks stay one at a time.

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

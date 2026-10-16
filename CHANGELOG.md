# Changelog

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

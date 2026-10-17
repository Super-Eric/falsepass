---
title: Using falsepass from an agent
description: MCP server, agent skill, JSON output and exit codes: everything a coding agent needs to audit a grader with falsepass.
---

# Using falsepass from an agent

falsepass is built to be driven by a coding agent as much as by a person. Every command
is non-interactive, writes its results to files, and has a machine-readable form. There
are three ways in, from most to least structured.

## 1. MCP server

`falsepass mcp` serves the commands as tools over stdio. Any MCP client can start it
without installing anything first:

```bash
uvx --from "falsepass[mcp]" falsepass mcp
```

For Claude Code:

```bash
claude mcp add falsepass -- uvx --from "falsepass[mcp]" falsepass mcp
```

For clients configured with JSON (Cursor, Claude Desktop, and others):

```json
{
  "mcpServers": {
    "falsepass": {
      "command": "uvx",
      "args": ["--from", "falsepass[mcp]", "falsepass", "mcp"]
    }
  }
}
```

| Tool | Arguments | Returns |
|---|---|---|
| `falsepass_scan` | `path` | static hints for a source folder or file; nothing is installed |
| `falsepass_init` | `target` (Hub id, local folder, Inspect task, HUD folder); optional `name`, `version`, `framework`, `task`, `answers`, `extra`, `manual`, `force` | `{"name": ..., "folder": ..., "framework": ..., "env": {...}, "next": ...}`: `name` is what the other tools take |
| `falsepass_audit` | `env`, `n` (tasks, default 25), `search` (mutation budget), `wait` (seconds, default 45) | the results digest below with `"status": "finished"`, or `"status": "running"` if the audit needs longer |
| `falsepass_audit_status` | `env`, `wait` | the same, for an audit already started: results, or `"running"` with the latest progress lines |
| `falsepass_results` | `env` | the digest of the last run (progress instead while one is running) |
| `falsepass_completion` | `env`, `completion_id` | the full text of one completion; the digest shows only its start and end |
| `falsepass_reach` | `env`, `probe` (e.g. `["P15/denied"]`), `tasks` (0 = every task), `wait` | per probe row and for the reference (P1) as a control: tasks counted, how many failed, full-reward and error counts, the failing task keys, up to three failing completions; runs in the background like an audit |
| `falsepass_repro` | `env`, `probe` (e.g. `["P5"]`); `run` (true: also run it twice), `script` (run an existing script instead), `wait` | where the stand-alone repro script was written; with `run`, whether the two runs were identical, where the first is stored, and how many cases still fail (in the background like an audit) |
| `falsepass_discover` | `days`, `limit` | recently maintained Hub environments and what each needs |

Every tool takes `workdir`; audits are created in `workdir/audits/<name>/`.

An audit installs the environment before it probes it, so it can take several minutes,
longer than many clients allow for one tool call. It therefore runs in the background:
`falsepass_audit` waits up to `wait` seconds and either returns the results or
`{"status": "running", "elapsed_s": ..., "log_tail": [...]}`. Call
`falsepass_audit_status` with the same `env` until it finishes. Calling
`falsepass_audit` again while one is running doesn't start a second audit. The job is
recorded in `audits/<env>/job.json`, its output in `audits/<env>/job.log`.

When something goes wrong (no such audit, a failed install, a dataset that needs a
login), the tool returns an MCP error whose text says what happened and what to try
next, so a client sees `isError` and the reason without reading server logs. Names are
forgiving: `arc_easy` finds the audit `init` named `arc-easy`.

For a HUD environment, pass reference answers with `answers`: a JSON file holding either
an object from task slug to answer (`{"add-2-3": "5"}`) or a list in task order. Without
it only the answer-free probes run. Adding answers to an existing audit takes
`force: true`. `uv` must be
on the PATH: each audit installs the environment in its own throw-away virtualenv.

## 2. Agent skill

[`skills/falsepass/SKILL.md`]({ORIGIN}{BASE}/docs/agents/skill/) is the whole audit procedure in
the Agent Skills format: run, read the numbers, confirm a lead before claiming it, decide
defect or design, and write it up for the maintainer. Copy the folder into the agent's
skills directory (for Claude Code, `~/.claude/skills/`) and ask the agent to audit an
environment.

## 3. Plain CLI

```bash
falsepass init owner/environment          # or ./folder, task.py, inspect_evals/<task>, a HUD folder
falsepass audit environment --fail-on 'ceiling>0.1'   # exit 1 when the condition is met
falsepass audit environment --jobs 1      # one task at a time (default: up to 8 at once where safe)
falsepass results environment --json      # compact digest, small enough for a context window
falsepass scan path/to/source --json      # static pre-scan only
```

The digest from `results --json` and from the MCP audit tools (abridged):

```json
{
  "environment": "owner/env",
  "headline": {"false_pass_ceiling": 1.0, "false_pass_via": "P5 shotgun/all_in_one_slot",
               "lazy_policy_reward": 0.0, "false_fail_rate": 0.108, "scoring_errors": 0},
  "failing": [{"probe": "P5", "variant": "all_in_one_slot", "runs": 10, "failed": 10,
               "completion": "start of the completion … [316 chars] … its end, where the answer is",
               "completion_id": "da79bcea66a8"}],
  "static_hints": ["..."],
  "note": "Leads, not findings: ..."
}
```

All fields are described in [Output files]({ORIGIN}{BASE}/docs/output/).

## Games, simulations and tool tasks

Automatic mode needs a reference answer it can write into a reply. A game, a
customer-service simulation or a tool task has none: the reward depends on what the model
does over several turns. There, an audit ends `"status": "inconclusive"` (the reference
and an empty reply earned about the same) or, for tool tasks, runs only the generic tool
policies. The digest then lists only `facts_that_hold`: rows built without the answer,
such as "an empty reply earned 1.0 on 20 of 20 tasks", which are true either way.

The next step is to write policies, which takes a capable agent a few minutes per
environment:

```bash
falsepass init owner/environment --policies --force   # adds adapter.py: a policy skeleton
```

A policy is a function from the conversation so far (a list of `{"role", "content"}`
dicts) to the next assistant message, either a string or
`{"content": ..., "tool_calls": [{"name": ..., "arguments": {...}}]}`. With
`Policy(name, act, wants_tools=True)` it also gets the tool schemas. Each policy runs
through the environment's own rollout loop and rubric. Fill in:

- `solver(example)`: a policy that does the task properly (it may read the row's answer).
  It should earn full reward; set `max_reward` to what it earns.
- `gives_up(example)`: plays honestly without solving. It should earn nothing.
- `exploits(example)`: policies that never use the answer. The skeleton starts from the
  ready-made ones in `falsepass.policies`, each of which found a real hole:
  `after_the_end(wrong, say)` (answer wrong, then repeat what the environment gives away),
  `keeps_talking(first)` (carry on after "game over"), `never_acts()`. Adapt their
  messages to the environment's format.
- `special_number_scripts(example)`: one message sequence the environment accepts;
  `policies.special_numbers()` turns it into variants with `nan`, `inf`, `-1`, `1e999` and
  `-0` in place of its numbers (ids such as `#1234` are left alone). A limit written as
  `amount > LIMIT` is false for NaN.

Then run `falsepass audit <env>` as usual. Each policy's episode (what it said, and the
whole conversation) is kept: `falsepass_completion` shows it, and `falsepass repro`
replays the policy's turns as a fixed script, so a lead found by a policy is reproduced
without falsepass.

## Reading the output

- **`false_pass_ceiling` near the maximum**: some known-bad completion was paid. `failing`
  says which probe and variant.
- **`lazy_policy_reward` above zero**: one fixed strategy that never uses the answer
  earns that much on average. On a closed answer set (yes/no, A–J), subtract what one
  fixed label earns under any correct grader.
- **`false_fail_rate`**: correct answers refused. Some of it is deliberate strictness.
- **`static_hints` with `verdict: confirmed`**: the line that most likely causes a failure.

## Rules worth giving your agent

The output is a list of leads. Before an agent reports anything:

1. Read the whole completion if the preview isn't enough: `falsepass_completion` (or
   `falsepass results <env> --completion <id>`).
2. Reproduce it: `falsepass repro <env> --probe P5` writes a script that depends only on
   the environment's own packages. Run it twice from a clean virtualenv, or add `--run`
   (MCP: `run=true`): falsepass runs it twice, each in a fresh virtualenv, compares the
   output and stores the first run headed by its command and the comparison.
3. Find the line in the reward code that causes it.
4. Decide whether it is a defect or a design choice. A grader that is exploitable on
   purpose, or strictness the prompt asks for, is not a bug.
5. Measure how many of the environment's own tasks it affects. The audit samples 25
   tasks by default; `falsepass reach <env> --probe P15/denied` (MCP `falsepass_reach`)
   scores that one row, and the reference as a control, on every task (`--tasks N` for a
   sample) and writes the counts and failing task keys to `audits/<env>/reach/`. It covers
   the per-example probes (P1–P9, P13–P15, A1, X1). Confirm the count with your own script
   before you quote it to a maintainer.
6. If you have a fix, check it: copy the audit's `src/` (or the package), patch the copy,
   and run `falsepass verify-fix <env> --source <copy>` (CLI). It reruns the repro script
   (`repro.py`, else `repro_auto.py`) unpatched once if no output is stored yet, then with
   the patch, and prints the lines that changed. A folder without `pyproject.toml` is laid
   over the released package, so its dependencies stay as released. It only replays the
   stored cases: to see whether the fix still pays correct answers, also `falsepass init`
   the patched copy as a local environment and audit it (the false-fail rate should not
   rise).

Don't let an agent call an environment broken on the strength of a probe table, and
don't let it file issues on other people's repositories without a person agreeing.

## Text versions

Every page on this site is also published as Markdown (link at the bottom of each page),
and [`llms.txt`]({ORIGIN}{BASE}/llms.txt) / [`llms-full.txt`]({ORIGIN}{BASE}/llms-full.txt)
index all of them.

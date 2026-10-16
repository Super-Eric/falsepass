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
| `falsepass_init` | `target` (Hub id, local folder, Inspect task, HUD folder), `name` | the command's output; creates `audits/<name>/env.toml` |
| `falsepass_audit` | `env`, `n` (tasks, default 25), `search` (mutation budget), `wait` (seconds, default 45) | the results digest below with `"status": "finished"`, or `"status": "running"` if the audit needs longer |
| `falsepass_audit_status` | `env`, `wait` | the same, for an audit already started: results, `"failed"` with the log tail, or `"running"` |
| `falsepass_results` | `env` | the digest of the last run |
| `falsepass_repro` | `env`, `probe` (e.g. `["P5"]`) | the command's output; writes `audits/<env>/repro_auto.py` |
| `falsepass_discover` | `days`, `limit` | recently maintained Hub environments and what each needs |

Every tool takes `workdir`; audits are created in `workdir/audits/<name>/`.

An audit installs the environment before it probes it, so it can take several minutes,
longer than many clients allow for one tool call. It therefore runs in the background:
`falsepass_audit` waits up to `wait` seconds and either returns the results or
`{"status": "running", "elapsed_s": ..., "log_tail": [...]}`. Call
`falsepass_audit_status` with the same `env` until it finishes. Calling
`falsepass_audit` again while one is running doesn't start a second audit. The job is
recorded in `audits/<env>/job.json`, its output in `audits/<env>/job.log`. `uv` must be
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
               "completion": "first failing completion, truncated"}],
  "static_hints": ["..."],
  "note": "Leads, not findings: ..."
}
```

All fields are described in [Output files]({ORIGIN}{BASE}/docs/output/).

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

1. Reproduce it: `falsepass repro <env> --probe P5` writes a script that depends only on
   the environment's own packages. Run it twice from a clean virtualenv.
2. Find the line in the reward code that causes it.
3. Decide whether it is a defect or a design choice. A grader that is exploitable on
   purpose, or strictness the prompt asks for, is not a bug.
4. Measure how many of the environment's own tasks it affects.

Don't let an agent call an environment broken on the strength of a probe table, and
don't let it file issues on other people's repositories without a person agreeing.

## Text versions

Every page on this site is also published as Markdown (link at the bottom of each page),
and [`llms.txt`]({ORIGIN}{BASE}/llms.txt) / [`llms-full.txt`]({ORIGIN}{BASE}/llms-full.txt)
index all of them.

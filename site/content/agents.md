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

Without an MCP client at hand (a script, a sandbox), twenty lines of Python drive it; this
works with the `mcp` SDK 1.x and 2.x:

```python
import asyncio, json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def call(tool: str, **arguments):
    server = StdioServerParameters(command="uvx", args=["--from", "falsepass[mcp]", "falsepass", "mcp"])
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            text = result.content[0].text
            if getattr(result, "isError", None) or getattr(result, "is_error", False):
                raise RuntimeError(text)  # the reason and the next step, in one message
            return json.loads(text)

init = asyncio.run(call("falsepass_init", target="rewards.py", dataset="data.jsonl"))
print(asyncio.run(call("falsepass_audit", env=init["name"], wait=120))["headline"])
```

Each call above starts a fresh server; that is fine, because state lives in `audits/` and a
running audit is found again by `falsepass_audit_status`.

| Tool | Arguments | Returns |
|---|---|---|
| `falsepass_scan` | `path` | static hints for a source folder or file; nothing is installed |
| `falsepass_init` | `target` (Prime Hub id, OpenEnv Space `hf:owner/space` or folder, a file of reward functions with `dataset`, local folder, Inspect task, HUD folder); optional `name`, `version`, `framework`, `task`, `answers`, `extra`, `without`, `dataset`, `functions`, `weights`, `split`, `dataset_config`, `answer_column`, `prompt_column`, `map`, `path`, `env_class`, `action_class`, `manual`, `policies`, `force` | `{"name": ..., "folder": ..., "framework": ..., "env": {...}, "next": ...}`: `name` is what the other tools take |
| `falsepass_audit` | `env`, `n` (tasks, default 25), `search` (mutation budget), `wait` (seconds, default 45) | the results digest below with `"status": "finished"`, or `"status": "running"` if the audit needs longer |
| `falsepass_audit_status` | `env`, `wait` | the same, for an audit already started: results, or `"running"` with the latest progress lines |
| `falsepass_results` | `env` | the digest of the last run (progress instead while one is running) |
| `falsepass_completion` | `env`, `completion_id` | the full text of one completion; the digest shows only its start and end |
| `falsepass_reach` | `env`, `probe` (e.g. `["P15/denied"]`) and/or `templates` (completions of your own, e.g. `["\\boxed{{answer} \\text{ is wrong}}"]`), `expect` (`zero` or `full`, for templates), `tasks` (0 = every task), `wait` | per probe row, per template and for the reference (P1) as a control: tasks counted, how many failed, full-reward and error counts, the failing task keys, up to three failing completions, and for a template the text it became on the first task; runs in the background like an audit |
| `falsepass_repro` | `env`, `probe` (e.g. `["P5"]`); `run` (true: also run it twice), `script` (run an existing script instead), `wait` | where the stand-alone repro script was written; with `run`, whether the two runs were identical, where the first is stored, and how many cases still fail (in the background like an audit) |
| `falsepass_verify_fix` | `env`, `source` (a patched package, or a patched copy of the audit's `src/`), `known_good` (-1 off, 0 every task, N a sample), `wait` | `report`: the repro lines the patch changes and, with `known_good`, the tasks whose correct answer it stops paying; in the background like an audit |
| `falsepass_draft` | `env` | writes `REPORT.draft.md` (each failing probe group as a candidate finding, confirmed static hints attached, judgement calls marked) and returns its text |
| `falsepass_upstream` | `env`, optional `repo` (owner/name), `limit` | the environment's source repository on GitHub, a file-by-file comparison with the audited version, default branch, licence, last push, open PRs (needs the `gh` CLI) |
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

For a HUD or OpenEnv environment, pass reference answers with `answers`: a JSON file holding either
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
falsepass init owner/environment          # or hf:owner/space (OpenEnv), ./folder, task.py, inspect_evals/<task>, a HUD folder
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

**v1 tool tasksets** (`verifiers.v1`, tools served over MCP, a scripted user): the policy
skeleton above is for environments built with `vf.load_environment`, and `init --policies`
says so instead of writing it. Until falsepass plays policies against v1 tasksets itself,
the route that works is verifiers' own evaluation loop with a stand-in model: a small local
OpenAI-compatible server (streaming chat completions) that answers with your scripted
policy, chosen by model name, then

```bash
vf-eval owner-environment -m <policy-name> --client.base-url http://127.0.0.1:PORT/v1 \
    --env.agent.runtime.type subprocess --no-push
```

`--env.agent.runtime.type subprocess` keeps the harness local (the default is a hosted
sandbox that needs a Prime login), and `--no-push` keeps the results off the Hub. Each
episode then runs through the taskset's own tools, scripted user and reward. Numbers a
policy puts in tool arguments as JSON `NaN` or `Infinity` arrive as `null`; send them as
strings (`"nan"`, `"1e999"`) to test the tools' parsing.

## When the reference is not a reply

Some Q&A environments store something other than a reply in their `answer` field: a
grading record (a JSON object with the truth phrases and the source text), a list of
accepted answers, or a placeholder. Automatic mode then writes that field into the reply,
the reference scores below the maximum, and the run is inconclusive. Write an adapter whose
reference is a complete reply:

```bash
falsepass init owner/environment --manual --force   # adds adapter.py (a v1 skeleton for v1 tasksets)
```

What the skeleton's parts mean:

- `load(n, seed)` returns the scorer and `n` examples. Each is
  `Example(key, prompt, answer, raw)`: `answer` is the correct reply's content, which the
  probes render, vary and deny; `raw` is the environment's own row or task object, which
  the scorer uses (the scorer never reads `answer`). Build `answer` from `raw` here, e.g.
  the JSON object the prompt asks for, filled from the grading record.
- `render(answer, example)` writes an answer the way the prompt asks for it, and
  `wrong_answer(example)` returns one that is certainly wrong.
- `equivalent_forms(example)` gives P2 its correct variants. The default is three
  (whitespace, a sentence after, CRLF); override it to add forms the prompt allows.
- `loader` tells `falsepass repro` how to load the same rows without the adapter: keep the
  skeleton's line, and keep example keys that end in the row's index (`row-17`, `task-17`).
- `max_reward` is what the reference earns when it is below 1.0 by design.

## Reward functions without an environment

When the grader is a set of functions written for TRL's `GRPOTrainer` (`def
accuracy_reward(completions, solution, **kwargs)`), point `falsepass_init` at the file (or
at a module name such as `trl.rewards` with `framework="reward"`) and give the rows it is
trained on as `dataset`: a `.jsonl`/`.json`/`.csv`/`.parquet` file, `hf:owner/name` (add
`split`, and `dataset_config` on the CLI), or `call:function` for a loader defined in the
same file. `functions` picks and orders the functions, `weights` gives the trainer's
`reward_weights`. Each function is called as the trainer calls it (every column as a keyword,
one completion per call) and the weighted sum is the reward; `metrics` in each row holds every
function's own value, so the term that pays is visible.

Read the digest's `note` first. It names the functions and weights used, the dataset's
columns, the prompt and reference columns chosen, whether completions were passed as
message lists, and, when the reference column is a worked solution, that the probes use its
final answer. If the reference column is wrong, re-init with `answer_column`; if a function
refuses one of the trainer's keywords (no `**kwargs`), it would raise `TypeError` under
`GRPOTrainer` itself, which is worth telling its author. The file's third-party imports are
installed for the audit; `extra` adds one, `without` drops one. A file that is part of a
package (`from .utils import ...`) cannot be imported alone: give the module name and the
folder that holds the package, `target="open_r1.rewards", framework="reward",
path=["./open-r1/src"]`, with the package's third-party imports in `extra`; `init` says so
when it sees relative imports.

The answer format is inferred from the prompt and from the markup the functions themselves
look for (`<think>`, `<reasoning>`, `<answer>`, `\boxed`), so the reference is written the
way the format rewards pay it and `max_reward` is what a correct, well-formatted reply
earns. `metrics` in each row (and `mean_terms` in a `falsepass_reach` row) holds every
function's own value: a row paid by a format term alone shows there. A function that
returns `None` for the reference itself marks a task the trainer skips: such tasks are left
out of the audit (the note counts them) and counted as `skipped` by `falsepass_reach`. When
the reference column holds worked solutions without a marked final answer and a short
answer column exists, the probes take their value from that column and the note says so;
the functions still receive the original column, so P1 failing then means the functions
read a different value out of it than the answer column holds.

## OpenEnv environments

`falsepass init hf:owner/space` clones the Space into `audits/<name>/space` and records it as
the source; a local folder with an `openenv.yaml` works the same. The audit installs the
Space's own requirements plus `openenv`, imports its app module, and drives the environment
class directly: `reset()` to a task, `step()` with the reply as the action. There is no
Docker and no server; the HTTP `/reset` and `/step` routes of an openenv server build a fresh
environment per request, which is why they are not used.

What to check in the results and how to steer the run:

- **Tasks.** Few Spaces implement the Task API; most pick a task through extra `reset()`
  keywords (`task_id`, `task_name`, `difficulty`, a seed). The `load_notes` in the digest
  name those keywords. Write `audits/<name>/tasks.json`, a JSON list of keyword sets, each
  optionally with an `answer` (`[{"task_id": 1, "answer": "approve"}, ...]`, or an object from
  task key to such a set), and rerun the audit. Without it, falsepass calls `reset()`
  repeatedly and keeps one task per distinct prompt (`reset-N` keys). An environment that
  draws a task at random on every reset is reset again until the recorded prompt comes back;
  when it never does, the rows carry an error saying so instead of a grade for another task.
- **References.** Give them with `--answers` (an object by task key or a list in task order)
  or in `tasks.json`; a Task API record's `answer`/`target`/`expected`/`label` field is used
  too. An environment that reveals the answer only after a step (`correct_answer` in the
  next observation, `ground_truth` in its state) is stepped once per task at load time and
  those values are taken, with a note saying so; the P1 control then shows whether that is
  what `step()` compares against. Without any, only the answer-free probes run and the
  digest says so. An answer may be a JSON object naming the action's fields
  (`{"decision": "approve"}`), in `tasks.json` and in `--answers` alike.
- **Structured actions.** When the action is a decision (`category: Literal[...]`,
  `approve: bool`) plus optional text, a plain reply goes into the text field and the
  decision is filled with a constant (`load_notes` says which). O1 then plays every value of
  the decision field with nothing else said: its per-value mean is what a policy earns
  without reading the task, shown as the lazy-policy reward. O2 sets a self-reported number
  (`confidence`, `score`, `quality`) to its maximum and to its minimum: a grader that trusts
  it pays the two differently. To grade a real decision, give answers that are JSON objects
  naming the action's fields (`{"category": "billing"}`), or write an adapter whose
  `render()` returns such an object as a string: a reply that parses as a JSON object with
  the action's field names is the action.
- **Reading a flat run.** When every reply scores the same (an empty one and a sentence
  alike), the digest's `note` says so: the reply never reached the graded part of the
  action (a decision or id field kept its default). The note also says where a plain reply
  goes and what the other fields get. The rows of such a run show that constant, not a
  grader property. `falsepass_reach` templates may be whole JSON actions
  (`'{"claim_id": "C-7", "decision": "deny"}'`), which is the quick way to score specific
  actions on every task.
- **What is not covered.** MCP tool environments (`CallToolAction`) and hand-written servers
  without an `Environment` subclass stop with a message; use `--policies` or an adapter.
  The environment class is read from the served app (what `create_app` was given); when
  detection still picks the wrong one, `falsepass init ... --env-class module:Class
  --action-class module:Class` (MCP `env_class`, `action_class`) names them, and `env.toml`
  takes `app = "module:attr"` as well. `--with` adds a requirement to the environment's own,
  `--without name` drops one (a pin that does not resolve); pins of openenv's own
  dependencies (`fastapi==...`) are relaxed at `init`, and the env.toml comment says which.

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
   the per-example probes (P1–P9, P13–P17, A1, X1). A count is for the completion the
   probe wrote, including the wrong value falsepass picked and where it put it: P15 has
   both orders (`false_equation` and `false_equation_reversed`), because a grader that keeps
   one side pays only one. To count a completion of your own, pass it as a template:
   `falsepass reach <env> --template '\boxed{{wrong} \lor {answer}}'` (MCP `templates`)
   fills `{answer}` and `{wrong}` (the reference and a wrong value), or `{answer_in_format}`
   and `{wrong_in_format}` (the same written in the audit's answer format), on each task;
   other braces stay as typed. Rows `U1/t1`, `t2`, ... count where it is paid (`--expect
   full`: where it is not paid in full), and `first_completion` shows what it became on the
   first task, so a mistyped placeholder shows. Look at a few failing completions before
   you quote a count to a maintainer.
6. If you have a fix, check it: copy the audit's `src/` (or the package), patch the copy,
   and run `falsepass verify-fix <env> --source <copy>` (MCP `falsepass_verify_fix`). It reruns the repro script
   (`repro.py`, else `repro_auto.py`) unpatched once if no output is stored yet, then with
   the patch, and prints the lines that changed (the repro only: `falsepass audit <env>
   --source <copy>` runs the whole probe suite on the patch). A folder without `pyproject.toml` is laid
   over the released package, so its dependencies stay as released. Add `--known-good`
   to also score every correct-answer row (P1, P2, P14) on every task with and without the
   patch (`--known-good 500` for a sample): it lists the tasks whose correct answer the
   patch stops paying. A fix that refuses correct answers is not ready.

Don't let an agent call an environment broken on the strength of a probe table, and
don't let it file issues on other people's repositories without a person agreeing.

## Text versions

Every page on this site is also published as Markdown (link at the bottom of each page),
and [`llms.txt`]({ORIGIN}{BASE}/llms.txt) / [`llms-full.txt`]({ORIGIN}{BASE}/llms-full.txt)
index all of them.

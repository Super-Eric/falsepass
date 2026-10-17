# Output files

Everything an audit writes lives in `audits/<env>/` (or the folder given with `--out`).

| File | For | Written by |
|---|---|---|
| `env.toml` | how the environment is installed: `id`, `framework` (`verifiers`, `inspect`, `hud`; default verifiers), `task` (Inspect or HUD task spec), `package`, `version`, `module`, `index`, `verifiers` / `inspect_ai` / `hud` (version pins), optional `source` (a local folder), `answers` (HUD reference file), `extra`, `python`, `load_args` | `falsepass init` |
| `src/` | the Hub wheel's source, for the static scan (local environments are scanned in place); for an installed Inspect task, its own folder and its scorers' modules, rewritten on every audit | `falsepass init`; `falsepass audit` for Inspect |
| `probe-results.json` | every probe run, machine-readable (below) | `falsepass audit` |
| `summary.md` | the same for people: headline numbers, one row per probe variant, reward terms, static hints | `falsepass audit` |
| `completions.json` | the full text of every completion a result refers to, keyed by `completion_id` | `falsepass audit` |
| `job.json`, `job.log` | an audit or reach job started from the MCP server: its `command`, state (`running`, `finished`, `failed`), pid, start time, and its console output | `falsepass mcp` |
| `reach/<probe>.json`, `reach/latest.json` | per-row counts over many tasks (below) | `falsepass reach` |
| `repro_auto.py` | stand-alone script that re-scores the failing completions | `falsepass repro` |
| `repro_auto.output.txt`, `repro-run.json` | the first of two runs of a repro script, headed by its command and whether the second was identical; the comparison as JSON (below) | `falsepass repro --run` |
| `REPORT.draft.md` | candidate findings to review | `falsepass draft` |

## probe-results.json

```json
{
  "meta": {"environment": "owner/env", "date": "…", "examples": 25, "seed": 0, "python": "3.12.x",
           "versions": {"verifiers": "…", "…": "…"}, "falsepass": "…", "max_reward": 1.0,
           "bad_tolerance": 0.0, "weights": {"term": 1.0}, "adapter": {"…": "how the format was inferred"} },
  "results": [ { "probe": "P5", "name": "shotgun", "variant": "all_in_one_slot", "example": "row-98",
                 "expected": "zero", "score": 1.0, "passed": false, "detail": "expected zero, got 1.0",
                 "completion": "preview", "metrics": {"term": 1.0}, "errors": [],
                 "answer_free": false, "completion_id": "…", "same_as_good": false } ],
  "static": [ { "rule": "reference_substring_match", "file": "env.py", "line": 26, "where": "load_environment",
                "evidence": "source line", "meaning": "…", "confirm_with": ["P9", "P5", "S3"],
                "verdict": "confirmed: P9 failed 10/20, P5 failed 10/30" } ],
  "rewardlint": null
}
```

- `expected`: `full` (a correct completion, must get the maximum), `zero` (a known-bad
  completion, must get nothing), `stable` (the same completion scored repeatedly, must get
  the same score), `at_most_reference` (must not beat the concise correct answer).
- `passed`: whether the grader did what `expected` asks.
- `answer_free`: the completion was built without the reference answer, so a policy that
  knows nothing about the task could produce it. These feed the lazy-policy number.
- `metrics`: the value of each reward term, so a leak can be traced to the term that pays.
- `errors`: exceptions or timeouts raised by the reward code on this completion.
- `completion_id`: key into `completions.json`; empty for completions over 60,000
  characters (generated payloads such as P10's 2 MB reply), which are kept as a preview.
- `same_as_good`: a known-bad completion that is identical to a known-good one on the same
  task (for a policy: the same replies). It is marked `passed` and left out of the ceiling;
  the lazy-policy reward still counts it, since a fixed strategy earns it on every task.
- `final_as_good`: a known-bad completion whose last reply equals a known-good one's but
  which said other things first: right by chance, or wrong tries before the right one (a
  real exploit if the grader pays it). It is counted; the headline adds a note when the
  ceiling rests on such a row, with the ceiling without them.

## falsepass results --json

A digest of the same run, small enough to hand to a model (numbers from `falsepass demo`):

```json
{
  "environment": "owner/env",
  "meta": {"date": "…", "falsepass": "…", "versions": {"…": "…"}, "examples": 25, "max_reward": 1.0,
           "adapter": {"format": "<answer>", "answers": "open", "turns": "single-turn", "validity": "ok"}},
  "headline": {"false_pass_ceiling": 1.0, "false_pass_via": "P5 shotgun/all_in_one_slot",
               "lazy_policy_reward": 0.0, "lazy_policy_via": "P3 empty/empty",
               "false_fail_rate": 0.108, "known_good": 93, "known_bad": 233, "scoring_errors": 0,
               "same_as_good": 0},
  "…": "when the ceiling rests on a row whose last reply equals a known-good one, the headline
        also has false_pass_note, false_pass_ceiling_other and false_pass_other_via",
  "failing": [ {"probe": "P5", "name": "shotgun", "variant": "all_in_one_slot", "expected": "zero",
                "runs": 10, "failed": 10, "scores": [1.0, 1.0, 1.0, 1.0, 1.0], "detail": "…", "example": "row-98",
                "completion": "start … [316 chars] … end", "completion_id": "da79bcea66a8",
                "answer_free": false} ],
  "static_hints": [ "as in probe-results.json" ],
  "note": "Leads, not findings: …"
}
```

`failing` lists false passes first (a known-bad completion was paid), the most
consistent ones on top, then false fails. `meta` and `static_hints` are trimmed to what an
agent needs; everything else is in `probe-results.json`.

The headline numbers:

- **false-pass ceiling**: the highest reward any known-bad completion received.
- **lazy-policy reward**: the best average reward of one fixed strategy that never uses
  the reference answer. On a closed answer set, one fixed label earns its share of the
  dataset under any correct grader; subtract that base rate before reading it.
- **false-fail rate**: the share of known-good completions denied full reward. Some of
  these are deliberate strictness about format; read the variant names.

`completion` shows the start and the end of the first failing completion (the answer is
usually at the end); `falsepass results <env> --completion <completion_id>` prints all of it.
A row without a `completion_id` carries a `completion_note` saying why.

## repro-run.json

Written by `falsepass repro <env> --run` (and returned by MCP `falsepass_repro` with `run=true`):

```json
{"script": "repro_auto.py", "command": "uv run -q --isolated …", "exit_codes": [0, 0],
 "identical": true, "output": "/abs/path/audits/env/repro_auto.output.txt", "lines": 12,
 "differences": [], "cases": 12, "reproduced": 12}
```

`cases` and `reproduced` appear for scripts `falsepass repro` wrote: `reproduced` counts the
cases that still miss what a correct verifier gives. A first run that fails gives `error`
and `stderr` instead, and nothing is stored.

## reach/<probe>.json

```json
{"meta": {"environment": "owner/env", "date": "…", "falsepass": "…", "asked": ["P15/denied"],
          "tasks_counted": 500, "tasks_in_dataset": 500, "all_tasks": true, "seed": 0, "jobs": 8,
          "max_reward": 1.0, "bad_tolerance": 0.0, "format": "reasoning+boxed",
          "audit_format": "reasoning+boxed", "note": "", "versions": {"verifiers": "…"}},
 "rows": [{"probe": "P1", "name": "ground_truth", "variant": "reference", "control": true,
           "expected": "full", "tasks": 500, "not_applicable": 0, "failed": 0, "full_reward": 500,
           "errors": 0, "same_as_good": 0, "mean_reward": 1.0, "failed_tasks": [], "examples": []},
          {"probe": "P15", "name": "hedged_in_slot", "variant": "denied", "control": false,
           "expected": "zero", "tasks": 500, "not_applicable": 0, "failed": 7, "full_reward": 7,
           "errors": 0, "same_as_good": 0, "mean_reward": 0.014, "failed_tasks": ["test-17", "…"],
           "examples": [{"task": "test-17", "score": 1.0, "completion_id": "…", "completion": "…"}]}]}
```

`failed` counts rows that missed their expectation: for `expected` zero, paid more than
`bad_tolerance`; for `expected` full, paid less than `max_reward`. `errors` are calls that
returned no score. `same_as_good` counts tasks where the known-bad completion came out
identical to the reference reply (a wrong value that happens to equal the answer, a
template that reduces to it): paid, but not counted in `failed`, so `full_reward` can
exceed `failed`. `not_applicable` counts tasks where the probe writes no such completion
(P16 on a task with no free-text field, for example), so `tasks` can be smaller than
`tasks_counted`. The answer format is inferred again for this run; `note` says so when
it differs from the audit's. `latest.json` is the same document plus `file`, its path.

With `--template`, `meta` also has `templates` and `template_expect`, and each template
is a row with `probe` "U1", `variant` "t1", "t2", ... in the order given, plus `template`
(the text as given) and `first_completion` (what it became on the first task). The file is
named `<probes>+template-<hash>.json`.

## falsepass init --json

```json
{"name": "gsm8k", "folder": "/abs/path/audits/gsm8k", "framework": "verifiers",
 "env": {"id": "primeintellect/gsm8k", "package": "gsm8k", "version": "…"}, "manual": false,
 "automatic_likely": true, "others": [], "next": "falsepass audit gsm8k"}
```

`name` is what every other command takes. Names are forgiving: `arc_easy` finds `arc-easy`.
`automatic_likely` is read from the package source (single- or multi-turn, a reference
answer in the data, no sandbox, tools, judge or API key): `false` means the automatic run
will probably stop and an adapter (`--manual` or `--policies`) is needed; `null` when there
is no wheel to read (local folders, Inspect registry names). With `false`,
`automatic_unlikely_because` says why (e.g. "sandbox, no reference answer found in the
source").

## Exit codes

`falsepass audit` exits 1 when a `--fail-on` condition is met and with the probe run's
own code when it fails; `falsepass reach` exits 2 when the rows asked for do not exist (the
message lists those that do); `falsepass repro --run` exits 1 when the two runs differ; every other command exits 0 on success and 1 with a message on
stderr when its input is missing.

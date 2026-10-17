# Output files

Everything an audit writes lives in `audits/<env>/` (or the folder given with `--out`).

| File | For | Written by |
|---|---|---|
| `env.toml` | how the environment is installed: `id`, `framework` (`verifiers`, `inspect`, `hud`; default verifiers), `task` (Inspect or HUD task spec), `package`, `version`, `module`, `index`, `verifiers` / `inspect_ai` / `hud` (version pins), optional `source` (a local folder), `answers` (HUD reference file), `extra`, `python`, `load_args` | `falsepass init` |
| `src/` | the Hub wheel's source, for the static scan (local environments are scanned in place) | `falsepass init` |
| `probe-results.json` | every probe run, machine-readable (below) | `falsepass audit` |
| `summary.md` | the same for people: headline numbers, one row per probe variant, reward terms, static hints | `falsepass audit` |
| `completions.json` | the full text of every completion a result refers to, keyed by `completion_id` | `falsepass audit` |
| `job.json`, `job.log` | an audit or reach job started from the MCP server: its `command`, state (`running`, `finished`, `failed`), pid, start time, and its console output | `falsepass mcp` |
| `reach/<probe>.json`, `reach/latest.json` | per-row counts over many tasks (below) | `falsepass reach` |
| `repro_auto.py` | stand-alone script that re-scores the failing completions | `falsepass repro` |
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
                 "answer_free": false, "completion_id": "…" } ],
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

## falsepass results --json

A digest of the same run, small enough to hand to a model (numbers from `falsepass demo`):

```json
{
  "environment": "owner/env",
  "meta": {"date": "…", "falsepass": "…", "versions": {"…": "…"}, "examples": 25, "max_reward": 1.0,
           "adapter": {"format": "<answer>", "answers": "open", "turns": "single-turn", "validity": "ok"}},
  "headline": {"false_pass_ceiling": 1.0, "false_pass_via": "P5 shotgun/all_in_one_slot",
               "lazy_policy_reward": 0.0, "lazy_policy_via": "P3 empty/empty",
               "false_fail_rate": 0.108, "known_good": 93, "known_bad": 233, "scoring_errors": 0},
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

## reach/<probe>.json

```json
{"meta": {"environment": "owner/env", "date": "…", "falsepass": "…", "asked": ["P15/denied"],
          "tasks_counted": 500, "tasks_in_dataset": 500, "all_tasks": true, "seed": 0, "jobs": 8,
          "max_reward": 1.0, "bad_tolerance": 0.0, "format": "reasoning+boxed",
          "audit_format": "reasoning+boxed", "note": "", "versions": {"verifiers": "…"}},
 "rows": [{"probe": "P1", "name": "ground_truth", "variant": "reference", "control": true,
           "expected": "full", "tasks": 500, "failed": 0, "full_reward": 500, "errors": 0,
           "mean_reward": 1.0, "failed_tasks": [], "examples": []},
          {"probe": "P15", "name": "hedged_in_slot", "variant": "denied", "control": false,
           "expected": "zero", "tasks": 500, "failed": 7, "full_reward": 7, "errors": 0,
           "mean_reward": 0.014, "failed_tasks": ["test-17", "…"],
           "examples": [{"task": "test-17", "score": 1.0, "completion_id": "…", "completion": "…"}]}]}
```

`failed` counts rows that missed their expectation: for `expected` zero, paid more than
`bad_tolerance`; for `expected` full, paid less than `max_reward`. `errors` are calls that
returned no score. The answer format is inferred again for this run; `note` says so when
it differs from the audit's. `latest.json` is the same document plus `file`, its path.

## falsepass init --json

```json
{"name": "gsm8k", "folder": "/abs/path/audits/gsm8k", "framework": "verifiers",
 "env": {"id": "primeintellect/gsm8k", "package": "gsm8k", "version": "…"}, "manual": false,
 "others": [], "next": "falsepass audit gsm8k"}
```

`name` is what every other command takes. Names are forgiving: `arc_easy` finds `arc-easy`.

## Exit codes

`falsepass audit` exits 1 when a `--fail-on` condition is met and with the probe run's
own code when it fails; `falsepass reach` exits 2 when the rows asked for do not exist (the
message lists those that do); every other command exits 0 on success and 1 with a message on
stderr when its input is missing.

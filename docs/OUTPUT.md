# Output files

Everything an audit writes lives in `audits/<env>/` (or the folder given with `--out`).

| File | For | Written by |
|---|---|---|
| `env.toml` | how the environment is installed: `id`, `framework` (`verifiers`, `inspect`, `hud`; default verifiers), `task` (Inspect or HUD task spec), `package`, `version`, `module`, `index`, `verifiers` / `inspect_ai` / `hud` (version pins), optional `source` (a local folder), `answers` (HUD reference file), `extra`, `python`, `load_args` | `falsepass init` |
| `src/` | the Hub wheel's source, for the static scan (local environments are scanned in place) | `falsepass init` |
| `probe-results.json` | every probe run, machine-readable (below) | `falsepass audit` |
| `summary.md` | the same for people: headline numbers, one row per probe variant, reward terms, static hints | `falsepass audit` |
| `completions.json` | the full text of every completion a result refers to, keyed by `completion_id` | `falsepass audit` |
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
  "meta": { "…": "as above" },
  "headline": {"false_pass_ceiling": 1.0, "false_pass_via": "P5 shotgun/all_in_one_slot",
               "lazy_policy_reward": 0.0, "lazy_policy_via": "P3 empty/empty",
               "false_fail_rate": 0.108, "known_good": 93, "known_bad": 233, "scoring_errors": 0},
  "failing": [ {"probe": "P5", "name": "shotgun", "variant": "all_in_one_slot", "expected": "zero",
                "runs": 10, "failed": 10, "scores": [1.0, 1.0, 1.0, 1.0, 1.0], "detail": "…", "example": "row-98",
                "completion": "first failing completion, truncated", "answer_free": false} ],
  "static_hints": [ "as in probe-results.json" ],
  "note": "Leads, not findings: …"
}
```

The headline numbers:

- **false-pass ceiling**: the highest reward any known-bad completion received.
- **lazy-policy reward**: the best average reward of one fixed strategy that never uses
  the reference answer. On a closed answer set, one fixed label earns its share of the
  dataset under any correct grader; subtract that base rate before reading it.
- **false-fail rate**: the share of known-good completions denied full reward. Some of
  these are deliberate strictness about format; read the variant names.

## Exit codes

`falsepass audit` exits 1 when a `--fail-on` condition is met and with the probe run's
own code when it fails; every other command exits 0 on success and 1 with a message on
stderr when its input is missing.

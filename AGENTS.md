# Notes for coding agents working on this repository

To *use* falsepass on an environment, read `skills/falsepass/SKILL.md` instead.

- Run the tests with `uv run pytest -q` and the linter with `uvx ruff check .`; both run in CI.
  Do not reformat files you did not change (the code is not `ruff format`-clean on purpose).
- The package has no runtime dependencies. verifiers is imported lazily inside the
  throw-away virtualenv an audit runs in; keep it that way, because Hub environments need
  mutually incompatible verifiers versions.
- A new probe goes in `falsepass/probes.py` with an ID, a line in the README's check
  table, a `draft.py` description, and a test that shows it catching a toy grader and
  passing a correct one (`tests/test_probes.py` has the pattern).
- A new static rule goes in `falsepass/static.py` with the probe that confirms it.
  Rules must be narrow: a hint that fires on correct code costs every user time.
- `examples/toy-env/` is bundled into the wheel as `falsepass/_demo` and checked by CI
  (`.github/workflows/ci.yml`): its planted defect must keep failing the gate.
- Probe output is a lead, not a verdict. Keep that wording in anything user-facing.
- `README.md` and `README.zh-CN.md` cover the same sections; change both together.

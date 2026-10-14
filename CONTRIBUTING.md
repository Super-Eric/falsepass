# Contributing

Bug reports, new probes and fixes are welcome.

- **A wrong lead** (falsepass flags a grader that behaves correctly, or misses a defect
  you found by hand): open an issue with the environment, its version, the probe and
  variant, and the completion. These reports are the most useful kind.
- **Code**: `uv run pytest -q` and `uvx ruff check .` must pass. Add a test for new
  behaviour. Keep changes small and say in the pull request what you ran.
- **Defects in someone else's environment**: report them to that environment's
  maintainer, not here. Give a minimal repro and a suggested fix; see
  `skills/falsepass/SKILL.md`, section 4.

By contributing you agree that your contribution is licensed under Apache-2.0.

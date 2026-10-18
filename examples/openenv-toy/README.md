# openenv-toy

A minimal [OpenEnv](https://github.com/huggingface/OpenEnv) environment with one planted
grading defect, laid out the way `openenv init` lays one out: `models.py` (action and
observation), `server/arith_environment.py` (`reset()`, `step()`, the Task API),
`server/app.py` (`create_app`), `client.py`, `openenv.yaml`.

Twelve arithmetic questions in two splits. `step()` pays 1.0 when the expected number
appears **anywhere** in the answer text (`self._expected in action.answer`), so an answer
that lists several numbers, or names the right one and then rejects it, is paid in full.

```bash
falsepass init examples/openenv-toy --answers examples/openenv-toy/answers.json
falsepass audit arith-env
```

The Task API lists the questions but, as usual, not the answers, so `answers.json` gives
them (by task key, `test-1`); without it only the answer-free probes run. The static scan
points at the line (`reference_substring_match`); the probes that list several answers
(P5) or deny the answer (P9, P15) are paid, and the control (P1, the expected number
alone) scores 1.0.

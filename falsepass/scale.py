"""Reference-relative scores, for rewards with no fixed scale.

Some rewards measure an outcome instead of checking an answer: a hedging error or a cost
(maximum 0, no floor), a game score, a distance. "Full" and "nothing" then have no fixed
values, and a fixed strategy really achieves some of the outcome. An adapter that defines
`scale_points(example)` returns two policies or completions for a task: the floor (doing
nothing, or the naive strategy a model could fall back on) and the reference (the textbook
solution, using only what the prompt shows). Both are scored on every task first, and every
score on that task is then reported as

    (raw - floor) / (reference - floor)

so 0 is what the floor earns and 1 what the reference earns; a strategy that beats the
reference scores above 1. The raw reward stays in the row's metrics as `raw_reward`. Tasks
where the reference does not beat the floor cannot be scaled and are left out (listed in
the results' `meta.scale.excluded`).
"""

from __future__ import annotations

import asyncio
from typing import Any

from falsepass.model import Example, Score

EPS = 1e-9
RAW = "raw_reward"


class RelativeScorer:
    """Wraps a scorer: same calls, rewards on each task's reference-relative scale."""

    def __init__(self, inner: Any, points: dict[str, tuple[float, float]]) -> None:
        self.inner, self.points = inner, points

    def __getattr__(self, name: str) -> Any:  # the wrapped scorer's flags (multi_turn, weights, ...)
        return getattr(self.inner, name)

    async def score(self, example: Example, completion: Any) -> Score:
        score = await self.inner.score(example, completion)
        if score.reward is None or example.key not in self.points:
            return score
        floor, reference = self.points[example.key]
        scaled = (score.reward - floor) / (reference - floor)
        return Score(scaled, {**score.metrics, RAW: score.reward}, score.errors, score.info)

    async def close(self) -> None:
        await self.inner.close()


async def measure(adapter: Any, scorer: Any, examples: list[Example], jobs: int = 1) -> tuple[dict, dict]:
    """Floor and reference rewards per task; the tasks that cannot be scaled, with why."""
    gate = asyncio.Semaphore(max(1, jobs))

    async def one(example: Example) -> tuple[str, Any]:
        async with gate:
            floor_completion, reference_completion = adapter.scale_points(example)
            floor = await scorer.score(example, floor_completion)
            reference = await scorer.score(example, reference_completion)
        if floor.reward is None or reference.reward is None:
            errors = (floor.errors or []) + (reference.errors or [])
            return example.key, f"not scored ({errors[0] if errors else 'no reward'})"
        if reference.reward - floor.reward <= EPS:
            return example.key, f"the reference ({reference.reward:.4g}) does not beat the floor ({floor.reward:.4g})"
        return example.key, (floor.reward, reference.reward)

    points, excluded = {}, {}
    for key, value in await asyncio.gather(*(one(ex) for ex in examples)):
        (points if isinstance(value, tuple) else excluded)[key] = value
    return points, excluded


def apply(adapter: Any, scorer: Any, examples: list[Example], loop: asyncio.AbstractEventLoop,
          jobs: int = 1) -> tuple[Any, list[Example], dict[str, Any] | None]:  # fmt: skip
    """The scorer and examples to probe with, and what goes into the results' `meta.scale`.
    Unchanged when the adapter defines no scale points."""
    if not examples or getattr(adapter, "scale_points", None) is None or adapter.scale_points(examples[0]) is None:
        return scorer, examples, None
    points, excluded = loop.run_until_complete(measure(adapter, scorer, examples, jobs))
    if not points:
        first = next(iter(excluded.values()), "no tasks")
        raise RuntimeError(f"no task could be put on the reference-relative scale: {first}. Check scale_points(): "
                           "the reference must earn more than the floor")  # fmt: skip
    # Every task now runs from 0 (the floor) to 1 (the reference).
    adapter.max_reward, adapter.task_max, adapter.format_floor = 1.0, {}, 0.0
    kept = [ex for ex in examples if ex.key in points]
    meta = {
        "kind": "reference-relative",
        "floor": getattr(adapter, "scale_floor", "") or "the adapter's floor strategy",
        "reference": getattr(adapter, "scale_reference", "") or "the adapter's reference strategy",
        "tasks": len(kept),
        "points": {key: list(value) for key, value in points.items()},
        "excluded": excluded,
    }
    return RelativeScorer(scorer, points), kept, meta

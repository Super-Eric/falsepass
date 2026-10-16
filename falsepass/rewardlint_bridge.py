"""Optional: run rewardlint's adversarial corpus through an environment's reward code.

rewardlint (github.com/junglezke/rewardlint, Apache-2.0) ships a fixed corpus of
adversarial completions for single reward functions. falsepass exposes the env's
scorer to it as a plain `f(completion, reference) -> float`.

Signature adaptation: rewardlint calls a synchronous function with a completion
and a reference of its own choosing. We substitute that reference into one of the
env's real examples via `Adapter.with_reference` and score through the env's full
rubric. Envs whose references cannot be swapped (multiple choice, per-row tests)
return None from `with_reference` and are skipped.
"""

from __future__ import annotations

import asyncio
from typing import Any

from falsepass.adapter import Adapter
from falsepass.model import Example
from falsepass.scorers import Scorer


def run(
    adapter: Adapter, scorer: Scorer, example: Example, loop: asyncio.AbstractEventLoop, jobs: int = 1
) -> dict[str, Any] | None:
    """With `jobs` > 1 (only for a scorer that is safe to call concurrently) the corpus is
    scored ahead, `jobs` calls at a time; rewardlint then reads the stored scores, so the
    results are the same as one call at a time."""
    try:
        import rewardlint
    except ImportError:
        return None
    if adapter.with_reference(example, "42") is None:
        return None
    ahead = _score_ahead(adapter, scorer, example, loop, jobs) if jobs > 1 else {}

    def reward(completion: str, reference: str) -> float:
        swapped = adapter.with_reference(example, reference)
        score = ahead.get((completion, reference)) or loop.run_until_complete(scorer.score(swapped, completion))
        if score.reward is None:
            raise RuntimeError(score.errors[0] if score.errors else "scoring failed")
        return score.reward / adapter.max_reward

    audit = rewardlint.audit(reward, name=adapter.name)
    return {
        "rewardlint_version": rewardlint.__version__ if hasattr(rewardlint, "__version__") else "unknown",
        "headline": audit.headline,
        "profile": audit.profile,
        "false_positive_rate": audit.false_positive_rate,
        "false_negative_rate": audit.false_negative_rate,
        "false_positives": [
            {"case": o.case.id, "attack": o.case.attack, "reference": o.case.reference,
             "completion": o.case.completion, "score": o.score}
            for o in audit.false_positives
        ],  # fmt: skip
        "false_negatives": [
            {"case": o.case.id, "reference": o.case.reference, "completion": o.case.completion, "score": o.score}
            for o in audit.false_negatives
        ],
        "errors": [{"case": o.case.id, "error": o.error} for o in audit.errors],
    }


def _score_ahead(adapter: Adapter, scorer: Scorer, example: Example, loop: asyncio.AbstractEventLoop, jobs: int) -> dict:
    try:
        from rewardlint import corpus

        cases = [*corpus.load(include_contested=False), *corpus.contested()]
        pairs = list(dict.fromkeys((c.completion, c.reference) for c in cases))
    except Exception:  # another rewardlint layout: score one at a time instead
        return {}
    out: dict = {}
    gate = asyncio.Semaphore(jobs)

    async def one(completion: str, reference: str) -> None:
        async with gate:
            try:
                out[(completion, reference)] = await scorer.score(adapter.with_reference(example, reference), completion)
            except Exception:  # left to the call rewardlint makes, which records the error
                pass

    async def everything() -> None:
        await asyncio.gather(*(one(*pair) for pair in pairs if isinstance(pair[0], str) and isinstance(pair[1], str)))

    loop.run_until_complete(everything())
    return out

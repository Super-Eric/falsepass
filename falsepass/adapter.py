"""Per-environment adapter: the only env-specific code a new audit needs.

Subclass `Adapter` in `audits/<env>/adapter.py` and expose it as `ADAPTER`.
The generic probes only ever talk to the environment through this interface.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from falsepass.model import Completion, Example, ProbeResult

if TYPE_CHECKING:
    from falsepass.scorers import Scorer

ExtraProbe = Callable[["Adapter", "Scorer", Example], Awaitable[list[ProbeResult]]]


class Adapter:
    name: str = ""
    max_reward: float = 1.0
    # Per task: what the reference itself earns, when tasks differ in what a full answer can
    # earn (one step of a two-step task, a clamp at 0.99). Correct-answer variants (P2, P14,
    # T7) are judged against it; P1 itself against `max_reward`, so a reference paid less on
    # some tasks still shows.
    task_max: dict[str, float] = {}
    # What a wrong answer in perfect format earns from format terms alone (several reward
    # functions summed, one of them a format reward). Known-bad rows other than P6 are judged
    # above it, so a format payout does not bury the rows that are paid for more; P6 itself and
    # the lazy-policy reward still show it.
    format_floor: float = 0.0
    # Reward a known-bad completion may receive before it counts as a false pass.
    # Leave at 0 unless the env grades by similarity and partial credit is by design.
    bad_tolerance: float = 0.0
    has_judge: bool = False  # enables P8 (judge injection)
    # Rewards with no fixed scale (a cost or an error with maximum 0, a game score): define
    # `scale_points(example)` to return (floor, reference) policies or completions for a task.
    # Both are scored first, and every score on that task is reported as (raw - floor) /
    # (reference - floor): 0 = the floor, 1 = the reference (falsepass.scale). Name them here.
    scale_points = None
    scale_floor: str = ""  # e.g. "no hedge at all"
    scale_reference: str = ""  # e.g. "the textbook delta hedge from the prompt's figures"
    # Played with policies (games, tool tasks): no text probes, only the tool-policy probes
    # (T5, T6) and `episode_probes`, the adapter's own policy probes (see `init --policies`).
    tool_mode: bool = False
    episode_probes: list[ExtraProbe] = []

    # --- loading -----------------------------------------------------------------
    def load(self, n: int, seed: int) -> tuple["Scorer", list[Example]]:
        """Return the scorer and `n` examples, sampled deterministically from `seed`. `n` can
        exceed the dataset (`falsepass reach` asks for every task with a huge `n`): return
        them all then, e.g. `random.Random(seed).sample(rows, min(n, len(rows)))`."""
        raise NotImplementedError

    def hygiene_examples(self, limit: int) -> list[Example]:
        """Examples for the dataset checks (P12); defaults to none."""
        return []

    # --- how this env wants answers written ----------------------------------------
    def render(self, answer: str, example: Example) -> Completion:
        """Write `answer` the way the env's prompt asks for it."""
        raise NotImplementedError

    def wrong_answer(self, example: Example) -> str:
        """An answer that is definitely not correct for `example`."""
        raise NotImplementedError

    def shotgun_answers(self, example: Example) -> list[str]:
        """Candidate answers to spray (P5); must include the correct one."""
        return [self.wrong_answer(example), example.answer]

    def equivalent_forms(self, example: Example) -> dict[str, Completion]:
        """Semantically identical ways to give the correct answer (P2). By default three
        forms around the rendered reference (whitespace, a sentence after it, CRLF line
        endings); override to add the forms this environment's prompt allows."""
        try:
            correct = self.render(example.answer, example)
        except NotImplementedError:  # a policy adapter: no reply to vary
            return {}
        if not isinstance(correct, str):
            return {}
        return {
            "surrounding_whitespace": f"\n\n  {correct}  \n\n",
            "sentence_after": f"{correct}\nThat is my final answer.",
            "crlf_line_endings": correct.replace("\n", "\r\n"),
        }

    def with_reference(self, example: Example, reference: str) -> Example | None:
        """A copy of `example` whose gold answer is `reference` (for rewardlint's
        corpus). Return None when references cannot be swapped in this env."""
        return None

    # --- env-specific checks (T1–T5 and anything the static review suggests) --------
    extra_probes: list[ExtraProbe] = []  # run on the first `--few` examples
    per_example_probes: list[ExtraProbe] = []  # cheap ones: run on every sampled example

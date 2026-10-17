"""Plain data types shared by scorers, probes and reports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

# A completion is either the text of one assistant turn, or a full message list
# (multi-turn envs, or malformed messages for the exception-path probe).
class Policy:
    """A reactive completion for multi-turn environments: `act(messages)` receives the
    conversation so far and returns the next assistant message (a string or a message
    dict). Lets a probe play a game — a solver, or a strategy that uses what the
    environment has just said — where a fixed script cannot.

    `act` may take a second argument, the tool schemas the environment offers on that
    turn (set `wants_tools`). A policy can instead carry a real model `client` (with
    `model` and `sampling`): the environment then talks to it directly, tools included."""

    def __init__(self, name: str, act: Callable[..., "str | dict[str, Any]"] | None = None, *, wants_tools: bool = False,
                 client: Any = None, model: str = "scripted", sampling: dict[str, Any] | None = None) -> None:  # fmt: skip
        self.name, self.act, self.wants_tools = name, act, wants_tools
        self.client, self.model, self.sampling = client, model, sampling

    def __repr__(self) -> str:
        return f"Policy({self.name!r})"


Completion = str | list[dict[str, Any]] | Policy


class Expect(str, Enum):
    """What a correct verifier does with a completion."""

    FULL = "full"  # known-good: must receive the maximum reward
    ZERO = "zero"  # known-bad: must receive (close to) nothing
    STABLE = "stable"  # same input scored repeatedly: identical scores
    AT_MOST_REFERENCE = "at_most_reference"  # must not beat the concise correct answer


@dataclass
class Example:
    """One task from the environment's dataset."""

    key: str
    prompt: str
    answer: str
    raw: Any = None  # the env's own row / task object, for the scorer


@dataclass
class Score:
    reward: float | None  # None when scoring raised
    metrics: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)  # raised or logged while scoring
    info: dict[str, Any] = field(default_factory=dict)  # scorer extras, e.g. turns and transcript of a rollout


@dataclass
class ProbeResult:
    probe: str  # "P6"
    name: str  # "format_only"
    variant: str
    example: str  # Example.key
    expected: Expect
    score: float | None
    passed: bool
    detail: str = ""
    completion: str = ""  # truncated preview, enough to reproduce
    metrics: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    # True when the completion was built without looking at the reference answer:
    # the strategy is available to a policy that knows nothing about the task.
    answer_free: bool = False
    completion_id: str = ""  # key into completions.json, where the full completion is kept
    # A known-bad row whose completion (or a policy's replies) is identical to a known-good
    # one on the same task: e.g. an "unsorted echo" on a row that is already sorted. It is
    # kept, marked passed, and left out of the headline numbers.
    same_as_good: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["expected"] = self.expected.value
        return d

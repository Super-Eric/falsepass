"""falsepass — probe RL environment verifiers for false passes and false fails.

The verifier is the system under test. falsepass feeds it completions whose
correctness is known in advance (the reference answer, an empty string, a
well-formatted wrong answer, ...) and reports every case where the reward
disagrees with what a correct grader would return. No model rollouts.
"""

from falsepass.adapter import Adapter
from falsepass.model import Completion, Example, Expect, ProbeResult, Score
from falsepass import hygiene, probes, report  # noqa: E402,F401

__all__ = ["Adapter", "Completion", "Example", "Expect", "ProbeResult", "Score"]
__version__ = "0.8.0"

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
__version__ = "0.10.0"


def build_version() -> str:
    """`0.8.0`, or `0.8.0+<commit>` when falsepass runs from a git checkout rather than a
    release (`+<commit>.dirty` with uncommitted changes), so a log says which build ran."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        return __version__
    try:
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return __version__
    if head.returncode != 0 or not head.stdout.strip():
        return __version__
    return f"{__version__}+{head.stdout.strip()}{'.dirty' if dirty.stdout.strip() else ''}"

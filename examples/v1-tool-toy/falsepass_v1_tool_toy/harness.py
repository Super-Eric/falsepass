"""The harness for this taskset: a plain tool-calling chat loop over the desk tools (no shell,
no file tools). Exported, so `vf-eval` and falsepass use it by default."""

from verifiers.v1.harnesses.null.harness import NullHarness


class DeskHarness(NullHarness):
    pass

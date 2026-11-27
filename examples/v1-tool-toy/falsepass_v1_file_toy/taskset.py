"""Reverse a word and write it to `answer.txt`; verifiers' default harness (bash) runs the agent.

The planted defect: `correct` grades the final reply, never the file the prompt asks for. An
agent that does as told and writes `answer.txt` scores 0 unless it also repeats the word as its
reply. `reads_the_file` is the fix, plugged in as
`--env.taskset.task.rewards '{"correct": {"fn": "falsepass_v1_file_toy:reads_the_file"}}'`.
"""

from collections.abc import Iterator

import verifiers.v1 as vf

WORDS = ["falsepass", "grader", "reward", "verifier", "episode"]
ARTIFACT = "answer.txt"


class ReverseTaskData(vf.TaskData):
    answer: str


class ReverseTask(vf.Task[ReverseTaskData, vf.State, vf.TaskConfig]):
    @vf.stop
    async def turn_limit(self, trace: vf.Trace) -> bool:
        return trace.num_turns >= 6

    @vf.reward
    async def correct(self, trace: vf.Trace) -> float:
        return float((trace.last_reply or "").strip() == self.data.answer)  # planted: the file is never read


async def reads_the_file(task, trace, runtime) -> float:
    """The fix: grade `answer.txt` when the agent wrote one, else the final reply."""
    data = getattr(task, "data", task)
    try:
        text = (await runtime.read(ARTIFACT, max_bytes=1 << 16)).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - no file written: grade the reply
        text = trace.last_reply or ""
    return float(text.strip() == data.answer)


class ReverseTaskset(vf.Taskset[ReverseTask, vf.TasksetConfig]):
    def load(self) -> Iterator[ReverseTask]:
        for i, word in enumerate(WORDS):
            yield ReverseTask(
                ReverseTaskData(id=f"reverse-{i}", prompt=f"Reverse the word `{word}`. Write the result to `{ARTIFACT}`.",
                                answer=word[::-1]),
                self.config.task,
            )

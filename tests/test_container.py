"""`falsepass.container.score_submission` drives a task's own setup, finalize and score in
the right order. Uses a real verifiers v1 task and a fake box, so no Docker is needed."""
import asyncio

import verifiers.v1 as vf

from falsepass.container import Submission, score_submission


class Data(vf.TaskData):
    expected: int


class EchoTask(vf.Task[Data, vf.State, vf.TaskConfig]):
    NEEDS_CONTAINER = True

    async def setup(self, trace, runtime):
        await runtime.write("/work/input.txt", b"41")

    async def finalize(self, trace, runtime):
        try:
            trace.info["answer"] = (await runtime.read("/work/answer.txt")).decode()
        except FileNotFoundError:
            trace.info["missing"] = True

    @vf.reward(weight=1.0)
    async def right(self, trace) -> float:
        return float(trace.info.get("answer") == str(self.data.expected))


class Done:
    exit_code, stdout, stderr = 0, "", ""


class FakeBox:
    """Stores files in a dict; 'runs' the submission script by executing it against that dict."""

    def __init__(self):
        self.files, self.log = {}, []

    async def write(self, path, data):
        self.log.append(("write", path))
        self.files[path] = data

    async def read(self, path, max_bytes=None):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def run(self, argv, env):
        self.log.append(("run", argv[0]))
        if argv[0] == "python3":
            exec(self.files[argv[1]].decode(), {"files": self.files})
        elif argv[0] == "rm":
            self.files.pop(argv[-1], None)
        return Done()


def task():
    return EchoTask(Data(idx=0, prompt="add one to /work/input.txt", workdir="/work", expected=42), vf.TaskConfig())


def test_nothing_written_scores_zero():
    box = FakeBox()
    score = asyncio.run(score_submission(task(), Submission("nothing"), box))
    assert score.reward == 0.0 and score.info == {"missing": True}


def test_files_are_written_after_setup_and_before_finalize():
    box = FakeBox()
    score = asyncio.run(score_submission(task(), Submission("file", files={"answer.txt": "42"}), box))
    assert score.reward == 1.0 and score.metrics["right"] == 1.0
    assert box.log[0] == ("write", "/work/input.txt")


def test_script_runs_in_the_box_and_is_removed():
    box = FakeBox()
    script = 'files["/work/answer.txt"] = str(int(files["/work/input.txt"]) + 1).encode()'
    score = asyncio.run(score_submission(task(), Submission("script", script=script), box))
    assert score.reward == 1.0 and score.script_exit == 0
    assert "/work/.falsepass_submission.py" not in box.files

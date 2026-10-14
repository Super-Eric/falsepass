"""Score `verifiers.v1` tasks that are graded on what is left in a container.

Such a task has no text answer: `setup` writes its input files into a box, an agent works
there, `finalize` reads the artifact back and the rewards compare it with a key held in
the host process. A probe is therefore a submission, not a completion: files to write
into the box, a script to run there, or both.

    with_box(task)            one Docker container from the task's own image and workdir
    score_submission(...)     setup -> write files -> run script -> finalize -> score

The caller decides what a submission does; nothing here limits it. The audits in this
repository keep to a convention: a submission reads only the input files the task's own
`setup` wrote and writes only the output the prompt names. Needs a running Docker daemon.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Submission:
    """What a probe leaves in the box. `script` is Python source run with `python3` in the
    task's workdir after `files` are written."""

    name: str
    files: dict[str, str] = field(default_factory=dict)
    script: str | None = None


@dataclass
class BoxScore:
    reward: float | None
    metrics: dict[str, float]
    info: dict[str, Any]
    script_exit: int | None = None
    script_stderr: str = ""


def workdir_of(task: Any) -> str:
    """The task's workdir. A task that leaves `data.workdir` empty may fall back to a
    constant of its own inside `setup`; this cannot see that and uses the runtime default,
    so pass such tasks with `data.workdir` set."""
    return task.data.workdir or "/app"


@contextlib.asynccontextmanager
async def with_box(task: Any):
    from verifiers.v1.runtimes import provision_runtime
    from verifiers.v1.runtimes.docker import DockerConfig

    config = DockerConfig(workdir=workdir_of(task))
    if task.data.image:
        config.image = task.data.image
    async with provision_runtime(config) as runtime:
        yield runtime


def empty_trace(task: Any) -> Any:
    import verifiers.v1 as vf
    from verifiers.v1.graph import MessageNode
    from verifiers.v1.trace import AgentInfo, TraceTask

    trace = vf.Trace(
        task=TraceTask(type=type(task).__name__, data=task.data, key=task.key, hash=task.hash),
        agent=AgentInfo(config=vf.AgentConfig()),
    )
    trace.nodes.append(MessageNode(message=vf.UserMessage(content=task.data.prompt_text)))
    return trace


async def score_submission(task: Any, submission: Submission, runtime: Any = None) -> BoxScore:
    """One task, one submission, one fresh box unless `runtime` is passed in."""
    if runtime is None:
        async with with_box(task) as box:
            return await score_submission(task, submission, box)
    trace = empty_trace(task)
    workdir = workdir_of(task)
    await task.setup(trace, runtime)
    for name, body in submission.files.items():
        await runtime.write(f"{workdir}/{name}", body.encode())
    exit_code, stderr = None, ""
    if submission.script is not None:
        path = f"{workdir}/.falsepass_submission.py"
        await runtime.write(path, submission.script.encode())
        done = await runtime.run(["python3", path], {})
        exit_code, stderr = done.exit_code, (done.stderr or "")[-2000:]
        await runtime.run(["rm", "-f", path], {})
    await task.finalize(trace, runtime)
    await task.score(trace, runtime)
    # Reward entries are the weighted values (a 0.5-weight reward that fired reads 0.5);
    # metrics are added under their own names and win on a name clash.
    metrics = {k: v.value for k, v in trace.rewards.items() if v is not None}
    metrics.update({k: v for k, v in trace.metrics.items() if v is not None})
    reward = float(trace.reward) if trace.reward is not None else None
    return BoxScore(reward, metrics, dict(trace.info), exit_code, stderr)

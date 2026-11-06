"""Where verifiers' API moved between the releases falsepass supports.

verifiers 0.4.1 (2026-11-03) moved evaluations to prime-rl: `vf-eval` and its `EvalConfig`
became `vf-run` and `RunnerConfig`, the `--no-push` flag went with the uploads, and the
runtimes were split into a backend (started and stopped) and the runtime it binds. Every
call site goes through here, so 0.4.0 and 0.4.1 both work.
"""

from __future__ import annotations

from typing import Any


def run_config_type() -> type:
    """The run config verifiers' own CLI builds: `EvalConfig` (0.4.0) or `RunnerConfig` (0.4.1)."""
    try:
        from verifiers.v1.configs.cli.eval import EvalConfig

        return EvalConfig
    except ImportError:
        from verifiers.v1.configs.cli.run import RunnerConfig

        return RunnerConfig


def no_push_args(config_type: type) -> list[str]:
    """`--no-push` where the run config has a push option (0.4.0 uploaded runs by default);
    nothing where uploads are gone (0.4.1)."""
    return ["--no-push"] if "push" in getattr(config_type, "model_fields", {}) else []


async def start_subprocess_runtime() -> tuple[Any, Any]:
    """A started local subprocess runtime and the coroutine function that stops it."""
    from verifiers.v1 import runtimes

    if hasattr(runtimes, "make_backend"):  # 0.4.1: a backend is started, then bound
        backend = runtimes.make_backend(runtimes.SubprocessConfig())
        await backend.start()
        return backend.bind(), backend.stop
    runtime = runtimes.SubprocessRuntime(runtimes.SubprocessConfig())
    await runtime.start()
    return runtime, runtime.stop

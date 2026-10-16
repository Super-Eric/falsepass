"""Run every applicable probe against one environment and write the results."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import platform
import sys
from datetime import date
from importlib import metadata
from pathlib import Path
from typing import Any

import falsepass
from falsepass import hygiene, probes, report, rewardlint_bridge
from falsepass.adapter import Adapter
from falsepass.model import ProbeResult

DEFAULT_JOBS = min(8, os.cpu_count() or 1)


def concurrent_ok(adapter: Adapter, scorer: Any) -> bool:
    """Examples may be scored concurrently only for single-turn scoring through a scorer
    that says it is safe (each call builds its own state). Multi-turn rollouts, tool-mode
    policies and frameworks that run a whole task loop stay one at a time: their
    environments may keep state between episodes, and that is what some probes look for."""
    return (
        bool(getattr(scorer, "concurrent_safe", False))
        and not getattr(scorer, "multi_turn", False)
        and not getattr(adapter, "multi_turn", False)
        and not getattr(adapter, "tool_mode", False)
    )


def load_adapter(audit_dir: Path) -> Adapter:
    if not (audit_dir / "adapter.py").is_file():  # no hand-written adapter: infer one
        import tomllib

        from falsepass.auto import AutoAdapter

        return AutoAdapter(tomllib.loads((audit_dir / "env.toml").read_text())["env"])
    spec = importlib.util.spec_from_file_location(f"falsepass_adapter_{audit_dir.name}", audit_dir / "adapter.py")
    assert spec and spec.loader, f"no adapter.py in {audit_dir}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ADAPTER


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not installed"


def run(
    audit_dir: Path, n: int = 25, seed: int = 0, few: int = 3, packages: list[str] | None = None,
    search_calls: int = 0, search_examples: int = 3, out: Path | None = None, jobs: int = DEFAULT_JOBS,
) -> dict[str, Any]:  # fmt: skip
    """Probe one environment. Results go to `out` (default: the audit folder itself; pass
    another folder for a trial run that must not replace the stored results). `jobs` is
    how many examples are scored at once where that is safe (see `concurrent_ok`); the
    results are the same as with `jobs=1`, in the same order."""
    out = out or audit_dir
    out.mkdir(parents=True, exist_ok=True)
    adapter = load_adapter(audit_dir)
    probes.STORE.clear()
    scorer, examples = adapter.load(n, seed)
    loop = asyncio.new_event_loop()
    results: list[ProbeResult] = []

    parallel = jobs > 1 and concurrent_ok(adapter, scorer)

    def run_probes(probe_list, subset, label: str = "probes", together: bool = False) -> None:
        """Each example's probes run in order. With `together`, up to `jobs` examples run
        at once; results are still collected in example order."""
        done = 0

        async def one(example) -> list[ProbeResult]:
            nonlocal done
            out_: list[ProbeResult] = []
            for probe in probe_list:
                out_.extend(await probe(adapter, scorer, example))
            done += 1
            print(f"falsepass: {label}: example {done} of {len(subset)} done", file=sys.stderr, flush=True)  # progress for job.log
            return out_

        if not (together and parallel):
            for example in subset:
                results.extend(loop.run_until_complete(one(example)))
            return
        gate = asyncio.Semaphore(jobs)

        async def gated(example) -> list[ProbeResult]:
            async with gate:
                return await one(example)

        async def everything() -> list[list[ProbeResult]]:
            return await asyncio.gather(*(gated(example) for example in subset))

        for part in loop.run_until_complete(everything()):
            results.extend(part)

    try:
        if hasattr(adapter, "prepare"):
            loop.run_until_complete(adapter.prepare(scorer, examples))
        if getattr(adapter, "tool_mode", False):
            run_probes(probes.TOOL_MODE, examples)
        elif getattr(adapter, "answer_free", False):
            run_probes(probes.ANSWER_FREE, examples, together=True)
            run_probes(probes.ANSWER_FREE_FEW, examples[:few], "slower probes")
        else:
            run_probes(probes.PER_EXAMPLE + list(adapter.per_example_probes), examples, together=True)
            run_probes(probes.FEW_EXAMPLES + list(adapter.extra_probes), examples[:few], "slower probes")
        if search_calls and not getattr(adapter, "tool_mode", False) and not getattr(adapter, "answer_free", False):
            from falsepass import search

            run_probes([search.make_probe(search_calls, seed)], examples[:search_examples], "mutation search")
        hygiene_results = hygiene.check_dataset(adapter.hygiene_examples(100)) if adapter.hygiene_examples(1) else []
        if getattr(adapter, "answer_free", False):  # no references were given, so none is "empty"
            hygiene_results = [r for r in hygiene_results if r.variant != "empty_answer"]
        results.extend(hygiene_results)
        no_reference = getattr(adapter, "tool_mode", False) or getattr(adapter, "answer_free", False)
        lint = None if no_reference else rewardlint_bridge.run(adapter, scorer, examples[0], loop)
    finally:
        loop.run_until_complete(scorer.close())
        loop.close()

    from falsepass import static

    source = audit_dir / "src"
    if not source.is_dir():  # a local environment (`falsepass init <folder>`): scan the folder itself
        import tomllib

        env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
        task_file = env.get("task", "").partition("@")[0]
        if env.get("framework") == "hud" and task_file:  # the environment and its tasks share a folder
            task_file = str(Path(task_file).parent if task_file.endswith(".py") else task_file)
        source = Path(env.get("source") or (task_file if task_file.endswith(".py") or env.get("framework") == "hud" else "") or source)
    hints = [h.to_dict() for h in static.scan(source)] if source.exists() else []
    payload = {
        "static": static.cross_check(hints, [r.to_dict() for r in results]),
        "meta": {
            "environment": adapter.name,
            "date": date.today().isoformat(),
            "examples": len(examples),
            "seed": seed,
            "python": platform.python_version(),
            "versions": {p: v for p in ["verifiers", "inspect-ai", "hud", "rewardlint", *(packages or [])]
                         if (v := _version(p)) != "not installed" or p not in ("verifiers", "inspect-ai", "hud")},
            "falsepass": falsepass.__version__,
            "max_reward": adapter.max_reward,
            "bad_tolerance": adapter.bad_tolerance,
            "weights": getattr(scorer, "weights", {}),
            "adapter": getattr(adapter, "inferred", None) or "hand-written",
            "loader": getattr(adapter, "loader", None),
        },
        "results": [r.to_dict() for r in results],
        "rewardlint": lint,
    }
    used = dict.fromkeys(r.completion_id for r in results if r.completion_id)  # in result order, whatever ran first
    (out / "completions.json").write_text(
        json.dumps({k: probes.STORE[k] for k in used if k in probes.STORE}, ensure_ascii=True)
    )
    (out / "probe-results.json").write_text(json.dumps(payload, indent=1, ensure_ascii=True))
    (out / "summary.md").write_text(report.summary(payload))
    return payload


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m falsepass.runner")
    parser.add_argument("audit_dir", type=Path)
    parser.add_argument("-n", type=int, default=25, help="examples to sample")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--few", type=int, default=3, help="examples for slow probes (P10, P11, extras)")
    parser.add_argument("--package", action="append", default=[], help="record this package's version")
    parser.add_argument("--search", type=int, default=0, metavar="CALLS",
                        help="mutation search: scorer calls per direction and example (0 = off; try 120)")
    parser.add_argument("--search-examples", type=int, default=3, help="examples to search on")
    parser.add_argument("--out", type=Path, help="write results here instead of the audit folder")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                        help=f"examples scored at once where safe (single-turn scoring); 1 = one at a time (default {DEFAULT_JOBS})")
    parser.add_argument(
        "--fail-on", action="append", default=[], metavar="NAME>NUMBER",
        help="exit 1 when met; NAME is ceiling, lazy, false_fail or errors (repeatable)",
    )  # fmt: skip
    args = parser.parse_args(argv)
    # Absolute paths: a HUD environment is loaded from its own folder (its hooks use relative paths).
    args.audit_dir = args.audit_dir.resolve()
    args.out = args.out.resolve() if args.out else None
    payload = run(args.audit_dir, args.n, args.seed, args.few, args.package, args.search, args.search_examples, args.out,
                  max(1, args.jobs))
    print(report.summary(payload))
    met = report.gate(payload["results"], args.fail_on)
    for condition in met:
        print(f"falsepass: gate failed: {condition}", file=sys.stderr)
    return 1 if met else 0


if __name__ == "__main__":
    sys.exit(main())

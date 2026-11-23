"""Run every applicable probe against one environment and write the results."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from datetime import date
from importlib import metadata
from pathlib import Path
from typing import Any

import falsepass
from falsepass import hygiene, probes, report, rewardlint_bridge, scale
from falsepass.adapter import Adapter
from falsepass.model import Expect, ProbeResult

DEFAULT_JOBS = min(8, os.cpu_count() or 1)
INSPECT_MARK = ".installed-inspect-task"  # in a src/ the runner wrote; such a src/ is rewritten on every run
LAUNCHED_AT = "FALSEPASS_LAUNCHED_AT"  # set by the CLI before the virtualenv is built: the set-up time is measured from it


class Stopwatch:
    """Seconds spent in each phase of a run, for `meta.timings` (where the time went: the
    virtualenv, loading, the probes, rewardlint) so a reader knows what a rerun costs."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.phases: dict[str, float] = {}
        self.hub_checked = False  # the dataset library was loaded and nothing told it to stay offline
        launched = os.environ.get(LAUNCHED_AT)
        self.set_up = round(max(0.0, time.time() - float(launched)), 1) if launched else None

    @contextlib.contextmanager
    def phase(self, name: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.phases[name] = round(self.phases.get(name, 0.0) + time.perf_counter() - started, 1)

    def summary(self, jobs: int) -> dict[str, Any]:
        return {
            "total_s": round(time.perf_counter() - self.started, 1),  # this process; the set-up came before it
            **({"set_up_s": self.set_up} if self.set_up is not None else {}),
            "phases": dict(self.phases),
            "jobs": jobs,
            "hub_checked": self.hub_checked,
        }


def concurrent_ok(adapter: Adapter, scorer: Any) -> bool:
    """Examples may be scored concurrently only for single-turn scoring through a scorer
    that says it is safe (each call builds its own state). Multi-turn rollouts, tool-mode
    policies and frameworks that run a whole task loop stay one at a time: their
    environments may keep state between episodes, and that is what some probes look for."""
    if getattr(scorer, "episodes_independent", False):  # each episode builds its own state (v1 episodes)
        return True
    return (
        bool(getattr(scorer, "concurrent_safe", False))
        and not getattr(scorer, "multi_turn", False)
        and not getattr(adapter, "multi_turn", False)
        and not getattr(adapter, "tool_mode", False)
    )


def score_examples(adapter: Adapter, scorer: Any, loop: asyncio.AbstractEventLoop, probe_list: list, subset: list,
                   jobs: int = 1, label: str = "probes") -> list[ProbeResult]:
    """Run `probe_list` on every example of `subset`; each example's probes in order. With
    `jobs` > 1 (only where `concurrent_ok`), up to `jobs` examples run at once; results are
    still returned in example order."""
    done = 0

    async def one(example) -> list[ProbeResult]:
        nonlocal done
        out_: list[ProbeResult] = []
        for probe in probe_list:
            out_.extend(await probe(adapter, scorer, example))
        done += 1
        if len(subset) <= 200 or done % (len(subset) // 100) == 0 or done == len(subset):  # about 100 lines at most
            print(f"falsepass: {label}: example {done} of {len(subset)} done", file=sys.stderr, flush=True)  # progress for job.log
        return out_

    if jobs <= 1:
        return [r for example in subset for r in loop.run_until_complete(one(example))]
    gate = asyncio.Semaphore(jobs)

    async def gated(example) -> list[ProbeResult]:
        async with gate:
            return await one(example)

    async def everything() -> list[list[ProbeResult]]:
        return await asyncio.gather(*(gated(example) for example in subset))

    return [r for part in loop.run_until_complete(everything()) for r in part]


def write_inspect_sources(audit_dir: Path, adapter: Adapter, scorer: Any) -> None:
    """An installed Inspect task has no wheel to unpack at `init`: copy its own module folder
    and the modules of the scorers it uses into src/, for the static scan and for reading."""
    import shutil

    spec = getattr(adapter, "task_spec", "") or ""
    framework = getattr(adapter, "framework", "")
    if framework not in ("inspect", "reward") or spec.partition("@")[0].endswith(".py"):
        return  # another framework, or a local file, which is scanned where it is
    src = audit_dir / "src"
    if src.exists() and not (src / INSPECT_MARK).exists():
        return  # a src/ someone else put there
    if framework == "reward":  # a module name (`trl.rewards`): the files that define the functions used
        from falsepass import reward_support

        files = reward_support.source_files(getattr(scorer, "funcs", {}))
    else:
        from falsepass import inspect_support

        files = inspect_support.source_files(spec, getattr(scorer, "scorers", []))
    if not files:
        return
    shutil.rmtree(src, ignore_errors=True)
    for relative, file in files.items():
        (src / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, src / relative)
    (src / INSPECT_MARK).write_text("Copied by falsepass from the installed task or module and the grading code it uses; rewritten on every audit.\n")


def load_adapter(audit_dir: Path) -> Adapter:
    if not (audit_dir / "adapter.py").is_file():  # no hand-written adapter: infer one
        import tomllib

        from falsepass.auto import AutoAdapter

        return AutoAdapter({**tomllib.loads((audit_dir / "env.toml").read_text())["env"], "_audit_dir": str(audit_dir)})
    spec = importlib.util.spec_from_file_location(f"falsepass_adapter_{audit_dir.name}", audit_dir / "adapter.py")
    assert spec and spec.loader, f"no adapter.py in {audit_dir}"
    module = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True  # no __pycache__ left in the audit folder
    spec.loader.exec_module(module)
    return module.ADAPTER


def vcs_versions() -> dict[str, str]:
    """Packages installed from a git URL (`tool @ git+https://...`), with the commit that was
    resolved: an unpinned URL is whatever the branch held that day, and it may be the grader."""
    found: dict[str, str] = {}
    for dist in metadata.distributions():
        try:
            direct = json.loads(dist.read_text("direct_url.json") or "{}")
        except (OSError, ValueError):
            continue
        commit = (direct.get("vcs_info") or {}).get("commit_id")
        name = dist.metadata["Name"] if dist.metadata else None
        if commit and name:
            found[name] = f"{dist.version} ({direct.get('url', '')}@{commit[:12]})"
    return dict(sorted(found.items()))


def _version(package: str) -> str:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not installed"


# --- P11 across processes ----------------------------------------------------------------

HASH_SEED_LOAD_LIMIT = 60.0  # seconds of loading: the second process loads the environment again
HASH_SEED_VARIANTS = ("reference", "wrong_in_format")


def _p11_completions(adapter: Adapter, ex: Any) -> dict[str, Any]:
    return {"reference": adapter.render(ex.answer, ex), "wrong_in_format": adapter.render(adapter.wrong_answer(ex), ex)}


def _fingerprint(completion: Any) -> str:
    import hashlib

    return hashlib.sha1(repr(completion).encode("utf-8", "surrogatepass")).hexdigest()


def rescore(audit_dir: Path, n: int, seed: int, keys: set[str], out: Path) -> None:
    """The second process of the hash-seed check: load the same sample and score the P11
    completions of the given examples once each, with this process's own string hashing."""
    adapter = load_adapter(audit_dir)
    scorer, examples = adapter.load(n, seed)
    loop = asyncio.new_event_loop()
    found: dict[str, dict[str, Any]] = {}
    try:
        if hasattr(adapter, "prepare"):
            loop.run_until_complete(adapter.prepare(scorer, examples))
        for ex in examples:
            if ex.key in keys:
                found[ex.key] = {
                    variant: {"reward": loop.run_until_complete(scorer.score(ex, done)).reward, "completion": _fingerprint(done)}
                    for variant, done in _p11_completions(adapter, ex).items()
                }
    finally:
        loop.run_until_complete(scorer.close())
        loop.close()
    out.write_text(json.dumps(found))


def hash_seed_rows(audit_dir: Path, n: int, seed: int, adapter: Adapter, examples: list, results: list[ProbeResult],
                   load_s: float) -> tuple[list[ProbeResult], str]:  # fmt: skip
    """P11 scores each completion repeatedly in one process, where str hashes, and with them
    the order of a set of strings, never change. A reward that depends on that order (the
    first five items of `list(set(...))`) is stable there and different in the next run. So
    the reference and the wrong reply P11 scored are scored once more in a second process
    with another PYTHONHASHSEED. Returns the rows and, when it did not run, why."""
    first = {(r.example, r.variant): r for r in results if r.probe == "P11" and r.variant in HASH_SEED_VARIANTS}
    if not first:
        return [], ""
    if load_s > HASH_SEED_LOAD_LIMIT:
        return [], f"not run: loading took {load_s:.0f} s, and the check loads the environment again"
    other = "1" if os.environ.get("PYTHONHASHSEED") == "0" else "0"
    keys = sorted({key for key, _ in first})
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "scores.json"
        command = [sys.executable, "-m", "falsepass.runner", str(audit_dir), "-n", str(n), "--seed", str(seed),
                   "--rescore", ",".join(keys), "--rescore-out", str(out)]  # fmt: skip
        try:
            done = subprocess.run(command, env={**os.environ, "PYTHONHASHSEED": other}, capture_output=True, text=True,
                                  timeout=max(120.0, 4 * load_s + 60))  # fmt: skip
        except subprocess.TimeoutExpired:
            return [], "not run: the second process timed out"
        if done.returncode or not out.is_file():
            last = (done.stderr.strip().splitlines() or ["no output"])[-1][:200]
            return [], f"not run: the second process failed ({last})"
        there = json.loads(out.read_text())
    by_key = {ex.key: ex for ex in examples}
    rows: list[ProbeResult] = []
    for (key, variant), row in first.items():
        other_run = (there.get(key) or {}).get(variant)
        if other_run is None or key not in by_key:
            continue
        done_here = _p11_completions(adapter, by_key[key])[variant]
        if other_run["completion"] != _fingerprint(done_here):  # the completion itself was built differently
            continue
        same = repr(other_run["reward"]) == repr(row.score)
        rows.append(ProbeResult(
            "P11", "determinism", f"{variant}_other_hash_seed", key, Expect.STABLE, other_run["reward"], same,
            f"scored again in a second process with PYTHONHASHSEED={other}: "
            + ("identical" if same else f"{other_run['reward']!r} there, {row.score!r} here"),
            row.completion,
        ))  # fmt: skip
    return rows, ""


def run(
    audit_dir: Path, n: int = 25, seed: int = 0, few: int = 3, packages: list[str] | None = None,
    search_calls: int = 0, search_examples: int = 3, out: Path | None = None, jobs: int = DEFAULT_JOBS,
    hash_seed: bool = True,
) -> dict[str, Any]:  # fmt: skip
    """Probe one environment. Results go to `out` (default: the audit folder itself; pass
    another folder for a trial run that must not replace the stored results). `jobs` is
    how many examples are scored at once where that is safe (see `concurrent_ok`); the
    results are the same as with `jobs=1`, in the same order."""
    out = out or audit_dir
    out.mkdir(parents=True, exist_ok=True)
    watch = Stopwatch()
    adapter = load_adapter(audit_dir)
    probes.STORE.clear()
    with watch.phase("load"):
        scorer, examples = adapter.load(n, seed)
        write_inspect_sources(audit_dir, adapter, scorer)
    watch.hub_checked = "datasets" in sys.modules and os.environ.get("HF_HUB_OFFLINE", "0").lower() not in ("1", "true", "yes", "on")
    loop = asyncio.new_event_loop()
    results: list[ProbeResult] = []

    parallel = jobs > 1 and concurrent_ok(adapter, scorer)

    def run_probes(probe_list, subset, label: str = "probes", together: bool = False) -> None:
        with watch.phase(label.replace(" ", "_")):
            results.extend(score_examples(adapter, scorer, loop, probe_list, subset, jobs if together and parallel else 1, label))

    try:
        with watch.phase("prepare"):
            if hasattr(adapter, "prepare"):
                loop.run_until_complete(adapter.prepare(scorer, examples))
            scorer, examples, scaled = scale.apply(adapter, scorer, examples, loop, jobs if parallel else 1)
        if getattr(adapter, "tool_mode", False):  # episodes run at once where the scorer builds each afresh (v1 episodes)
            run_probes(probes.TOOL_MODE + list(getattr(adapter, "episode_probes", [])), examples, together=True)
        elif getattr(adapter, "answer_free", False):
            run_probes(probes.ANSWER_FREE + list(getattr(adapter, "answer_free_probes", [])), examples, together=True)
            run_probes(probes.ANSWER_FREE_FEW, examples[:few], "slower probes", together=True)
        else:
            run_probes(probes.PER_EXAMPLE + list(adapter.per_example_probes), examples, together=True)
            run_probes(probes.FEW_EXAMPLES + list(adapter.extra_probes), examples[:few], "slower probes", together=True)
        if search_calls and not getattr(adapter, "tool_mode", False) and not getattr(adapter, "answer_free", False):
            from falsepass import search

            run_probes([search.make_probe(search_calls, seed)], examples[:search_examples], "mutation search")
        probes.mark_same_as_good(results)
        probes.mark_no_control(results, adapter)
        with watch.phase("hygiene"):
            hygiene_results = hygiene.check_dataset(adapter.hygiene_examples(hygiene.ROWS)) if adapter.hygiene_examples(1) else []
        if getattr(adapter, "answer_free", False) or getattr(adapter, "tool_mode", False):  # no references to be "empty"
            hygiene_results = [r for r in hygiene_results if r.variant != "empty_answer"]
        if (getattr(adapter, "loader", None) or {}).get("kind") == "v1-episode":  # the task is its world, not its prompt
            hygiene_results = [r for r in hygiene_results if r.variant not in ("duplicate_prompt", "empty_prompt")]
        results.extend(hygiene_results)
        no_reference = getattr(adapter, "tool_mode", False) or getattr(adapter, "answer_free", False)
        with watch.phase("rewardlint"):
            lint = None if no_reference else rewardlint_bridge.run(adapter, scorer, examples[0], loop, jobs if parallel else 1)
    finally:
        with watch.phase("close"):
            loop.run_until_complete(scorer.close())
        loop.close()

    hash_seed_note = ""
    if hash_seed and not scaled and not getattr(adapter, "tool_mode", False) and not getattr(adapter, "answer_free", False):
        with watch.phase("hash_seed"):
            rows, hash_seed_note = hash_seed_rows(audit_dir, n, seed, adapter, examples, results, watch.phases.get("load", 0.0))
        at = max((i for i, r in enumerate(results) if r.probe == "P11"), default=len(results) - 1) + 1
        results[at:at] = rows

    from falsepass import static

    source = audit_dir / "src"
    if not source.is_dir():  # a local environment (`falsepass init <folder>`): scan the folder itself
        import tomllib

        env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
        task_file = env.get("task", "").partition("@")[0]
        if env.get("framework") == "hud" and task_file:  # the environment and its tasks share a folder
            task_file = str(Path(task_file).parent if task_file.endswith(".py") else task_file)
        source = Path(env.get("source") or (task_file if task_file.endswith(".py") or env.get("framework") == "hud" else "") or source)
    with watch.phase("static"):
        hints = [h.to_dict() for h in static.scan(source)] if source.exists() else []
        if getattr(adapter, "framework", "") == "reward":  # hints inside reward functions that were not selected say nothing here
            from falsepass import reward_support

            selected = set((getattr(adapter, "loader", None) or {}).get("functions", []))
            others: dict[str, set[str]] = {}
            for h in hints:
                file = source / h["file"] if source.is_dir() else source
                if h["file"] not in others:
                    try:
                        others[h["file"]] = set(reward_support.declared_functions(file)) - selected
                    except (OSError, SyntaxError):
                        others[h["file"]] = set()
            hints = [h for h in hints if h["where"].split(" ")[0] not in others.get(h["file"], set())]
    timings = watch.summary(jobs if parallel else 1)
    payload = {
        "static": static.cross_check(hints, [r.to_dict() for r in results]),
        "meta": {
            "environment": adapter.name,
            "date": date.today().isoformat(),
            "examples": len(examples),
            "seed": seed,
            "python": platform.python_version(),
            "versions": {**{p: v for p in ["verifiers", "inspect-ai", "hud", "openenv", "rewardlint", *(packages or [])]
                            if (v := _version(p)) != "not installed" or p not in ("verifiers", "inspect-ai", "hud", "openenv")},
                         **vcs_versions()},
            "falsepass": falsepass.build_version(),
            "max_reward": adapter.max_reward,
            "bad_tolerance": adapter.bad_tolerance,
            "weights": getattr(scorer, "weights", {}),
            "adapter": getattr(adapter, "inferred", None) or "hand-written",
            # a hand-written adapter's notes, and its scorer's (a v1 episode scorer says where the harness runs code)
            **({"notes": notes} if not getattr(adapter, "inferred", None) and (notes := list(dict.fromkeys(
                [*(getattr(adapter, "load_notes", None) or []), *(n for n in (getattr(scorer, "notes", None) or []) if isinstance(n, str))]
            ))) else {}),
            **({"tool_calls": dict(scorer.counts)} if (getattr(scorer, "counts", None) or {}).get("tool_calls") else {}),
            **({"default_reset": list(scorer.default_reset)} if getattr(scorer, "default_reset", None) else {}),
            **({"one_step": dict(scorer.steps)} if not getattr(scorer, "multi_turn", False) and (getattr(scorer, "steps", None) or {}).get("replies") else {}),
            **({"source": os.environ["FALSEPASS_AUDIT_SOURCE"]} if os.environ.get("FALSEPASS_AUDIT_SOURCE") else {}),
            "loader": getattr(adapter, "loader", None),
            **({"scale": scaled} if scaled else {}),
            **({"hash_seed_check": hash_seed_note} if hash_seed_note else {}),
            "timings": timings,
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
    print(f"falsepass: {report.timing_words(timings)}", file=sys.stderr, flush=True)
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
    parser.add_argument("--no-hash-seed-check", action="store_true",
                        help="skip scoring the P11 replies again in a second process with another PYTHONHASHSEED")
    parser.add_argument("--rescore", help=argparse.SUPPRESS)  # the second process of that check: example keys
    parser.add_argument("--rescore-out", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--fail-on", action="append", default=[], metavar="NAME>NUMBER",
        help="exit 1 when met; NAME is ceiling, lazy, false_fail or errors (repeatable)",
    )  # fmt: skip
    args = parser.parse_args(argv)
    # Absolute paths: a HUD environment is loaded from its own folder (its hooks use relative paths).
    args.audit_dir = args.audit_dir.resolve()
    args.out = args.out.resolve() if args.out else None
    if args.rescore is not None:
        rescore(args.audit_dir, args.n, args.seed, set(args.rescore.split(",")), args.rescore_out)
        return 0
    try:
        payload = run(args.audit_dir, args.n, args.seed, args.few, args.package, args.search, args.search_examples, args.out,
                      max(1, args.jobs), hash_seed=not args.no_hash_seed_check)
    except RuntimeError as stop:  # falsepass's own "cannot audit this as it stands" messages: no traceback
        print(f"falsepass: {stop or type(stop).__name__}", file=sys.stderr)
        if args.out is None:  # kept next to any earlier results, so the overview and a reader see why it stopped
            (args.audit_dir / "stopped.md").write_text(f"# Audit stopped\n\n{date.today()} · falsepass "
                                                       f"{falsepass.build_version()}\n\n{stop}\n")  # fmt: skip
        return 1
    if args.out is None:
        (args.audit_dir / "stopped.md").unlink(missing_ok=True)
    print(report.summary(payload))
    met = report.gate(payload["results"], args.fail_on)
    for condition in met:
        print(f"falsepass: gate failed: {condition}", file=sys.stderr)
    return 1 if met else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # a dataset streamed through `datasets` can leave threads that keep the process alive

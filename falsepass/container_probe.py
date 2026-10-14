"""Automatic probes for container tasks (`verifiers.v1`, `NEEDS_CONTAINER = True`).

Such a task is graded on a file left in a box, against a key kept in the host process.
Because the key is at hand, wrong submissions can be made from it without knowing the
task: write the key itself as the output file (the control, which must be paid in full),
then the same key with its floats set to NaN, its integers plus 0.9 or Infinity, its
first item dropped, an empty file, or an input file copied over.

How the key maps onto the file is inferred per task, as the automatic text mode infers an
answer format: for a JSON output, the key itself or one of its top-level values; for a
CSV output, a list of row dicts among them. The first candidate the task's own scoring
pays in full is used. If none is, the task needs hand-written submissions.

Results are written like `falsepass audit`'s (`probe-results.json`, `summary.md`), with
probes C0 (control), C1 (numbers) and C2 (structure). A mutated file passes when it is
not paid in full: partial credit by design is common in these tasks, the question here is
whether a wrong artifact earns everything.

    falsepass container <env> [-n 12] [--out DIR]     (needs a running Docker daemon)
"""

from __future__ import annotations

import asyncio
import csv
import io
import itertools
import json
import re
from pathlib import Path
from typing import Any

from falsepass.container import Submission, score_submission
from falsepass.model import Expect, ProbeResult
from falsepass.probes import _mutate_numbers

EPS = 1e-9
FULL = 1.0  # v1 rewards are weighted sums that top out at 1.0
KEY_FIELDS = ("expected", "answer", "reference", "key", "target", "solution")
FILE_NAME = re.compile(r"`?([\w.-]+\.(?:json|csv|jsonl|txt))`?")


def key_of(data: Any) -> Any:
    for name in KEY_FIELDS:
        value = getattr(data, name, None)
        if isinstance(value, (dict, list)):
            return value
    return None


def input_files(data: Any) -> dict[str, str]:
    files = getattr(data, "input_files", None)
    if isinstance(files, dict):
        return {str(k): str(v) for k, v in files.items()}
    texts = {name: value for name, value in vars(data).items() if isinstance(value, str) and name.endswith(("_text", "_csv"))}
    return {name: value for name, value in texts.items() if "\n" in value}


def output_files(data: Any) -> list[str]:
    """Where the answer may have to go: the task's own `output_file`, otherwise every file
    the prompt names that is not a known input (the control finds out which)."""
    named = getattr(data, "output_file", None)
    if isinstance(named, str) and named:
        return [named]
    inputs = set(input_files(data))
    found: list[str] = []
    for name in FILE_NAME.findall(getattr(data, "prompt_text", "") or ""):
        if name not in inputs and not name.startswith(".") and name not in found:
            found.append(name)
    return found


def to_csv(rows: list[dict[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows([{k: ("" if v is None else v) for k, v in row.items()} for row in rows])
    return out.getvalue()


def candidates(key: Any, output: str) -> list[tuple[str, Any]]:
    """(label, value) pairs: the key and each of its top-level values that could be the file."""
    values = [("key", key)]
    if isinstance(key, dict):
        values += [(f"key[{name!r}]", v) for name, v in key.items() if isinstance(v, (dict, list))]
    if output.endswith(".csv"):
        return [(label, v) for label, v in values if isinstance(v, list) and v and all(isinstance(r, dict) for r in v)]
    return values


def encode(value: Any, output: str) -> str:
    return to_csv(value) if output.endswith(".csv") else json.dumps(value)


def mutations(value: Any, output: str, inputs: dict[str, str]) -> dict[str, tuple[str, str, bool]]:
    """variant -> (probe, file text, built without the key)."""
    found: dict[str, tuple[str, str, bool]] = {}
    nan, n = _mutate_numbers(value, floats=lambda _: float("nan"))
    if n:
        found["floats_nan"] = ("C1", encode(nan, output), False)
    plus, n = _mutate_numbers(value, ints=lambda i: i + 0.9)
    if n:
        found["ints_plus_0.9"] = ("C1", encode(plus, output), False)
        inf, _ = _mutate_numbers(value, ints=lambda _: float("inf"))
        found["ints_infinity"] = ("C1", encode(inf, output), False)
    if isinstance(value, dict) and len(value) > 1:
        found["first_item_dropped"] = ("C2", encode(dict(list(value.items())[1:]), output), False)
    elif isinstance(value, list) and len(value) > 1:
        found["first_item_dropped"] = ("C2", encode(value[1:], output), False)
    if output.endswith(".csv") and isinstance(value, list) and value:
        found["header_only"] = ("C2", ",".join(value[0]) + "\n", True)
    else:
        found["empty_object"] = ("C2", "{}" if isinstance(value, dict) else "[]", True)
    for name, text in list(inputs.items())[:1]:
        found["input_copied"] = ("C2", text, True)
    return found


async def _score(score, task: Any, submission: Submission) -> tuple[float | None, dict[str, float], str]:
    """The task's own score for a submission; a raise in the task's scoring is an outcome."""
    try:
        box = await score(task, submission)
    except Exception as exc:  # noqa: BLE001 - the reward code raising is itself the observation
        return None, {}, f"{type(exc).__name__}: {exc}"[:300]
    return box.reward, box.metrics, ""


async def probe_task(task: Any, label: str, score=score_submission) -> list[ProbeResult]:
    data = task.data
    outputs, key = output_files(data), key_of(data)
    if not outputs or key is None:
        return [ProbeResult("C0", "control", "no_key_or_output_file", label, Expect.FULL, None, False,
                            "could not find the key and the output file name in the task data")]  # fmt: skip
    tried = []
    for output, (name, value) in ((o, c) for o in outputs for c in candidates(key, o)):
        reward, _, error = await _score(score, task, Submission("control", {output: encode(value, output)}))
        tried.append(f"{name} as {output}: {reward if error == '' else error}")
        if reward is not None and reward >= FULL - EPS:
            break
    else:
        return [ProbeResult("C0", "control", "no_candidate_format", label, Expect.FULL, None, False,
                            f"the key is not paid in full in any form tried ({'; '.join(tried) or 'none'}): "
                            "this task needs hand-written submissions")]  # fmt: skip
    results = [ProbeResult("C0", "control", f"{name} as {output}", label, Expect.FULL, reward, True, "", encode(value, output)[:300])]
    for variant, (code, text, free) in mutations(value, output, input_files(data)).items():
        reward, metrics, error = await _score(score, task, Submission(variant, {output: text}))
        paid_in_full = reward is not None and reward >= FULL - EPS
        detail = "paid in full" if paid_in_full else (f"scoring raised {error}" if error else "")
        results.append(ProbeResult(code, "artifact_mutation", variant, label, Expect.ZERO, reward,
                                   reward is not None and not paid_in_full, detail, text[:300], metrics,
                                   [error] if error else [], free))  # fmt: skip
    return results


def load_tasks(module: str, load_args: dict[str, Any], n: int) -> list[Any]:
    import verifiers.v1 as vf
    from verifiers.v1.utils.loaders import taskset_class

    taskset = taskset_class(module)(vf.taskset_config_type(module)(id=module, **load_args))
    return list(itertools.islice(iter(taskset.load()), n))


def run(audit_dir: Path, n: int = 12, out: Path | None = None, packages: list[str] | None = None) -> dict[str, Any]:
    import platform
    import tomllib
    from datetime import date

    import falsepass
    from falsepass import report

    env = tomllib.loads((audit_dir / "env.toml").read_text())["env"]
    tasks = load_tasks(env.get("module") or env["package"], env.get("load_args", {}), n)
    results: list[ProbeResult] = []
    for i, task in enumerate(tasks):
        results.extend(asyncio.run(probe_task(task, f"task-{i}")))
    payload = {
        "static": [],
        "meta": {
            "environment": env["id"], "date": date.today().isoformat(), "examples": len(tasks), "seed": 0,
            "python": platform.python_version(), "falsepass": falsepass.__version__, "max_reward": FULL,
            "versions": {p: _version(p) for p in ["verifiers", *(packages or [])]},
            "bad_tolerance": 0.0, "weights": {}, "adapter": "container (key written as the output file)", "loader": None,
        },
        "results": [r.to_dict() for r in results],
        "rewardlint": None,
    }  # fmt: skip
    out = out or audit_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "probe-results.json").write_text(json.dumps(payload, indent=1, ensure_ascii=True, allow_nan=True))
    (out / "summary.md").write_text(report.summary(payload))
    return payload


def _version(package: str) -> str:
    from importlib import metadata

    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "not installed"


def table(payload: dict[str, Any]) -> str:
    """Per variant: tasks on which it was paid in full, and on which scoring raised."""
    rows: dict[tuple[str, str], list[int]] = {}
    for r in payload["results"]:
        variant = r["variant"] if r["probe"] != "C0" else "control (" + r["variant"] + ")"
        counts = rows.setdefault((r["probe"], variant), [0, 0, 0])
        counts[0] += r["score"] is not None and r["score"] >= FULL - EPS
        counts[1] += bool(r["errors"]) or (r["score"] is None and r["probe"] != "C0")
        counts[2] += 1
    lines = [f"{'probe':5} {'variant':44} {'paid in full':>12} {'raised':>7}"]
    lines += [f"{probe:5} {variant:44} {f'{full}/{n}':>12} {f'{raised}/{n}':>7}" for (probe, variant), (full, raised, n) in sorted(rows.items())]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m falsepass.container_probe")
    parser.add_argument("audit_dir", type=Path)
    parser.add_argument("-n", type=int, default=12, help="tasks to probe (one container per submission)")
    parser.add_argument("--out", type=Path, help="write results here instead of the audit folder")
    parser.add_argument("--package", action="append", default=[])
    args = parser.parse_args(argv)
    print(table(run(args.audit_dir, args.n, args.out, args.package)))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())

"""How far a probe reaches: score chosen probe variants on many tasks, or all of them.

An audit samples 25 tasks. Once a probe fails there, the next question is how many tasks
of the whole dataset it fails on. `falsepass reach <env> --probe P15/denied --tasks all`
scores only what it was asked for, plus the reference (P1) as a control, and writes
`audits/<env>/reach/<probe>.json`. The stored audit results are not touched.

Per-example probes only (P1-P9, P13-P15, A1, X1): the slower probes that repeat or replay
an episode (P10, P11, T4-T8) and tool-mode policies are not counted this way.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import falsepass
from falsepass import probes
from falsepass.model import Expect
from falsepass.runner import DEFAULT_JOBS, _version, concurrent_ok, load_adapter, score_examples

ALL = 10**9  # "every task": larger than any dataset; the loaders cap at the dataset's size
EXAMPLES_KEPT = 3  # full completions kept per variant, for reading and for a repro


def _label(wanted: list[str]) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", "+".join(wanted))[:120]


def _probe_list(adapter: Any) -> list:
    if getattr(adapter, "tool_mode", False):
        raise RuntimeError("reach counts per-example probes; this environment is played with tool policies (T5, T6)")
    if getattr(adapter, "answer_free", False):
        return list(probes.ANSWER_FREE)
    return probes.PER_EXAMPLE + list(getattr(adapter, "per_example_probes", []))


def run(audit_dir: Path, wanted: list[str], tasks: int | None = None, seed: int = 0, jobs: int = DEFAULT_JOBS,
        out: Path | None = None) -> dict[str, Any]:  # fmt: skip
    """Score the probes in `wanted` ("P15" or "P15/denied") on `tasks` tasks (None: all)."""
    adapter = load_adapter(audit_dir)
    hand_written = (audit_dir / "adapter.py").is_file()
    if tasks is None and hand_written:
        raise RuntimeError("this audit uses a hand-written adapter, which may not know its dataset's size: "
                           "give a number of tasks (--tasks N)")  # fmt: skip
    if not hand_written:
        from falsepass import auto

        auto.MAX_ROWS = ALL  # the audit samples from the first 2000 rows; reach counts or samples from all of them
    probes.STORE.clear()
    scorer, examples = adapter.load(tasks or ALL, seed)
    loop = asyncio.new_event_loop()
    try:
        if hasattr(adapter, "prepare"):
            loop.run_until_complete(adapter.prepare(scorer, examples))
        probe_list = _probe_list(adapter)
        # What can be asked for: run the probes on one task with nothing scored.
        probes.ONLY = {"(none)"}
        catalog = score_examples(adapter, scorer, loop, probe_list, examples[:1], 1, "catalog")
        if "known-good" in wanted:  # every row whose completion is correct: does a patch still pay them?
            wanted = [w for w in wanted if w != "known-good"] + sorted(
                {f"{r.probe}/{r.variant}" for r in catalog if r.expected is Expect.FULL})
        known = {r.probe for r in catalog} | {f"{r.probe}/{r.variant}" for r in catalog}
        unknown = [w for w in wanted if w not in known]
        if unknown:
            raise RuntimeError(f"no per-example probe row matches {unknown}; this environment has: "
                               + ", ".join(sorted({f"{r.probe}/{r.variant}" for r in catalog})))  # fmt: skip
        control = [] if getattr(adapter, "answer_free", False) else ["P1"]
        probes.ONLY = set(wanted) | set(control)
        parallel = jobs > 1 and concurrent_ok(adapter, scorer)
        results = score_examples(adapter, scorer, loop, probe_list, examples, jobs if parallel else 1, "reach")
    finally:
        probes.ONLY = set()
        loop.run_until_complete(scorer.close())
        loop.close()

    probes.mark_same_as_good(results)  # against the P1 control's completion on each task
    rows: dict[tuple[str, str, str], list] = defaultdict(list)
    for r in results:
        if r.detail != probes.SKIPPED:
            rows[r.probe, r.name, r.variant].append(r)
    counted = []
    for (probe, name, variant), group in rows.items():
        failing = [r for r in group if not r.passed]
        counted.append({
            "probe": probe, "name": name, "variant": variant, "control": probe in control and probe not in wanted and f"{probe}/{variant}" not in wanted,
            "expected": group[0].expected.value, "tasks": len(group),
            "failed": len(failing),  # expected 0: paid more than the tolerance; expected full: paid less
            "full_reward": sum(1 for r in group if r.score is not None and r.score >= adapter.max_reward - probes.EPS),
            "errors": sum(1 for r in group if r.score is None),
            "same_as_good": sum(r.same_as_good for r in group),  # identical to the reference reply: not counted as failed
            "mean_reward": round(sum(r.score or 0.0 for r in group) / len(group), 6),
            "failed_tasks": [r.example for r in failing],
            "examples": [{"task": r.example, "score": r.score, "completion_id": r.completion_id,
                          "completion": probes.STORE.get(r.completion_id, r.completion)} for r in failing[:EXAMPLES_KEPT]],
        })  # fmt: skip
    pool = getattr(adapter, "pool", None)
    fmt = (getattr(adapter, "inferred", None) or {}).get("format", "hand-written adapter")
    stored = audit_dir / "probe-results.json"
    audited = (json.loads(stored.read_text())["meta"].get("adapter") or {}) if stored.is_file() else {}
    audited = audited.get("format") if isinstance(audited, dict) else None
    payload = {
        "meta": {
            "environment": adapter.name, "date": date.today().isoformat(), "falsepass": falsepass.__version__,
            "asked": wanted, "tasks_counted": len(examples), "tasks_in_dataset": len(pool) if pool is not None else None,
            "all_tasks": tasks is None, "seed": seed, "jobs": jobs if parallel else 1,
            "max_reward": adapter.max_reward, "bad_tolerance": adapter.bad_tolerance,
            "format": fmt, "audit_format": audited,
            "note": f"the answer format inferred here ({fmt}) differs from the audit's ({audited}): the counts "
                    "are for this format" if audited and audited != fmt else "",
            "versions": {p: v for p in ["verifiers", "inspect-ai", "hud"] if (v := _version(p)) != "not installed"},
        },  # fmt: skip
        "rows": counted,
    }
    out = out or audit_dir / "reach"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{_label(wanted)}.json"
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=True))
    (out / "latest.json").write_text(json.dumps({**payload, "file": str(path)}, indent=1, ensure_ascii=True))
    payload["file"] = str(path)
    return payload


def describe(payload: dict[str, Any]) -> str:
    meta = payload["meta"]
    of = f" of {meta['tasks_in_dataset']}" if meta["tasks_in_dataset"] else ""
    scope = "every task" if meta["all_tasks"] else f"a random sample{of}, seed {meta['seed']}"
    lines = [f"{meta['environment']}: {meta['tasks_counted']} tasks counted ({scope}); answer format: {meta['format']}"]
    if meta["note"]:
        lines.append(f"note: {meta['note']}")
    for row in payload["rows"]:
        what = "paid" if row["expected"] == Expect.ZERO.value else "not paid in full"
        tag = "control  " if row["control"] else ""
        lines.append(f"  {tag}{row['probe']} {row['name']}/{row['variant']}: {what} on {row['failed']} of {row['tasks']} "
                     f"(full reward {row['full_reward']}, errors {row['errors']}, mean {row['mean_reward']:.3f}"
                     + (f"; {row['same_as_good']} identical to the reference reply, not counted" if row["same_as_good"] else "")
                     + ")")  # fmt: skip
    lines.append(f"written to {payload['file']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m falsepass.reach")
    parser.add_argument("audit_dir", type=Path)
    parser.add_argument("--probe", action="append", required=True, metavar="CODE[/VARIANT]",
                        help="what to count, e.g. P15 or P15/denied (repeatable); known-good: every correct-answer row")
    parser.add_argument("--tasks", default="all", help="number of tasks to sample, or 'all' (default)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    parser.add_argument("--out", type=Path, help="folder for the result (default audits/<env>/reach/)")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--package", action="append", default=[], help=argparse.SUPPRESS)  # passed by `falsepass` itself
    args = parser.parse_args(argv)
    if args.tasks != "all" and not args.tasks.isdigit():
        parser.error("--tasks takes a number or 'all'")
    try:
        payload = run(args.audit_dir.resolve(), args.probe, None if args.tasks == "all" else int(args.tasks), args.seed,
                      max(1, args.jobs), args.out.resolve() if args.out else None)  # fmt: skip
    except RuntimeError as problem:
        print(f"falsepass: {problem}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=1) if args.json else describe(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())

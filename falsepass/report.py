"""Turn probe results into the short `summary.md` table."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

KNOWN_GOOD, KNOWN_BAD = "full", "zero"


def headline(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Two numbers that describe a verifier:
    false-pass ceiling — the most reward any known-bad completion received;
    false-fail rate — the share of known-good completions denied full reward."""
    counted = [r for r in results if not r.get("same_as_good") and not r.get("no_control")]  # see ProbeResult
    bad = [r for r in counted if r["expected"] == KNOWN_BAD and r["score"] is not None]
    good = [r for r in counted if r["expected"] == KNOWN_GOOD and r["score"] is not None]  # no score: an error, counted apart
    worst = max(bad, key=lambda r: r["score"], default=None)
    other = max((r for r in bad if not r.get("final_as_good")), key=lambda r: r["score"], default=None)
    # A fixed strategy earns its reward on every task, including those where it happens to be
    # right (same_as_good): the lazy number averages over all of them.
    lazy_value, lazy_via = lazy_policy([r for r in results if r.get("answer_free") and r["score"] is not None])
    return {
        "lazy_policy_reward": lazy_value,
        "lazy_policy_via": lazy_via,
        "false_pass_ceiling": worst["score"] if worst else None,
        "false_pass_via": (f'{worst["probe"]} {worst["name"]}/{worst["variant"]}' if worst and worst["score"] > 0
                           else "no known-bad row was paid" if worst else None),
        "false_fail_rate": (sum(not r["passed"] for r in good) / len(good)) if good else None,
        "known_good": len(good),
        "known_bad": len(bad),
        "scoring_errors": sum(bool(r["errors"]) for r in results),
        "same_as_good": sum(bool(r.get("same_as_good")) for r in results),
        "no_control": sum(bool(r.get("no_control")) for r in results),
        "no_control_tasks": len({r["example"] for r in results if r.get("no_control")}),
        **({"false_pass_note": "the ceiling comes from a row whose last reply equals a known-good reply on that task: "
                               "correct by chance, or wrong tries before the right one; in a multi-turn episode the last "
                               "reply can be a no-op the solver also ends with (read it)",
            "false_pass_ceiling_other": other["score"] if other else None,
            "false_pass_other_via": f'{other["probe"]} {other["name"]}/{other["variant"]}' if other else None}
           if worst and worst.get("final_as_good") else {}),
    }


def lazy_policy(answer_free: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    """The best *average* reward of one fixed strategy that never looks at the reference
    answer (empty output, echo, a constant wrong answer in perfect format, ...): what a
    policy that knows nothing earns by doing the same thing every time. The ceiling is a
    single worst case; this is the expected payout."""
    by_strategy: dict[str, list[float]] = defaultdict(list)
    for r in answer_free:
        by_strategy[f'{r["probe"]} {r["name"]}/{r["variant"]}'].append(r["score"])
    most = max((len(v) for v in by_strategy.values()), default=0)
    means = {k: sum(v) / len(v) for k, v in by_strategy.items() if len(v) >= max(3, most // 3)}
    if not means:
        return None, None
    best = max(means, key=means.get)
    return means[best], best


def term_leak(results: list[dict[str, Any]], weights: dict[str, float]) -> list[tuple[str, float, float, float | None]]:
    """Per reward term: how often and how much it paid on known-bad completions and on fixed
    strategies (expected `lazy`). Rows are (term, share of such runs with a non-zero value,
    mean value, weight)."""
    values: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r["expected"] in (KNOWN_BAD, "lazy") and r["score"] is not None and not r.get("no_control"):
            for term, value in (r.get("metrics") or {}).items():
                if term not in ("num_turns", "raw_reward"):  # raw_reward: the unscaled reward (falsepass.scale)
                    values[term].append(float(value))
    return [
        (term, sum(v != 0 for v in vals) / len(vals), sum(vals) / len(vals), weights.get(term))  # a penalty pays too
        for term, vals in values.items()
    ]


def gate(results: list[dict[str, Any]], conditions: list[str]) -> list[str]:
    """Evaluate `--fail-on` conditions such as 'ceiling>0.1', 'lazy>0.05', 'false_fail>0.2',
    'errors>0'. Returns the conditions that are met."""
    head = headline(results)
    names = {
        "ceiling": head["false_pass_ceiling"],
        "lazy": head["lazy_policy_reward"],
        "false_fail": head["false_fail_rate"],
        "errors": head["scoring_errors"],
    }
    met = []
    for condition in conditions:
        name, _, limit = condition.replace(" ", "").partition(">")
        if name not in names or not limit:
            raise ValueError(f"bad --fail-on condition {condition!r}; use one of {sorted(names)} as NAME>NUMBER")
        if names[name] is not None and names[name] > float(limit):
            met.append(f"{condition} (actual {names[name]:.3g})")
    return met


MAX_FAILING = 25  # failing probe variants listed in a digest; the rest are counted


def digest(payload: dict[str, Any], example_chars: int = 400, completions: dict[str, Any] | None = None) -> dict[str, Any]:
    """A compact, machine-readable view of a probe run for scripts and coding agents: the
    headline numbers, every probe variant that failed (with one failing completion), and
    the static hints. Each item is a lead to reproduce and read, not a verdict. With
    `completions` (completions.json), the example shows the start and the end of the full
    text, where the answer usually is; `completion_id` fetches all of it."""
    from falsepass.probes import preview

    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in payload["results"]:
        groups[(r["probe"], r["name"], r["variant"], r["expected"])].append(r)
    failing = []
    for (probe, name, variant, expected), rows in groups.items():
        bad = [r for r in rows if not r["passed"]]
        if not bad:
            continue
        first = bad[0]
        completion = first.get("completion")
        full = (completions or {}).get(first.get("completion_id") or "")
        if isinstance(full, dict) and "transcript" in full:  # a policy's episode: what it said, in order
            full = " | ".join(m["content"] for m in full["transcript"] if m.get("role") == "assistant")
        if isinstance(full, str):
            completion = preview(full, example_chars)
        failing.append({
            "probe": probe, "name": name, "variant": variant, "expected": expected,
            "runs": len(rows), "failed": len(bad),
            "scores": [r["score"] for r in bad[:5]],
            **({"scores_note": f"the first 5 of {len(bad)} failed rows"} if len(bad) > 5 else {}),
            "detail": first.get("detail"), "example": first.get("example"),
            "completion": completion[:example_chars] if isinstance(completion, str) and full is None else completion,
            "completion_id": first.get("completion_id") or None,
            **({} if first.get("completion_id") or not completion else {"completion_note": "not kept: completions over 60,000 characters "
                "(generated payloads such as P10's 2 MB reply) are stored as a preview only; the probe code rebuilds them"}),
            "answer_free": bool(first.get("answer_free")),
            **({"errors": first["errors"][:2]} if first.get("errors") else {}),
            **({"note": STRUCTURED_NOTE} if probe in OTHER_ANSWER_PROBES and structured_answers(payload["meta"]) else {}),
        })  # fmt: skip
    # False passes first (a known-bad completion paid), the most consistent ones on top; then false fails.
    failing.sort(key=lambda f: (f["expected"] != "zero", -f["failed"] / f["runs"]))
    meta = payload["meta"]
    adapter = meta.get("adapter")
    validity = adapter.get("validity", "ok") if isinstance(adapter, dict) else "ok"
    extra: dict[str, Any] = {}
    if validity.startswith("inconclusive"):
        # The control failed, so rows that needed the reference say nothing. Rows built without it still hold.
        holds = [f for f in failing if f["answer_free"]]
        extra = {
            "status": "inconclusive",
            "reason": validity,
            "facts_that_hold": [f"{f['probe']} {f['name']}/{f['variant']}: expected {f['expected']}, got "
                                f"{', '.join(_fmt(x) for x in f['scores'][:3])} on {f['failed']} of {f['runs']} tasks" for f in holds[:8]],
            "failing_omitted": len(failing),
            "next": "Automatic mode could not tell a correct completion from an empty one here. For a Q&A task whose "
                    "`answer` field is not the reply the prompt asks for (a grading record, a list of accepted answers), "
                    "write an adapter whose reference is that reply: `falsepass init <target> --manual --force` (MCP: "
                    "manual=true, force=true), https://super-eric.github.io/falsepass/docs/agents/#when-the-reference-is-not-a-reply. "
                    "For a game, a simulation or a tool task, write policies: `--policies --force` (MCP: policies=true), "
                    "https://super-eric.github.io/falsepass/docs/agents/#games-simulations-and-tool-tasks",
        }  # fmt: skip
        failing = []
    elif unscored(payload["results"]):
        rows, none, first = unscored(payload["results"])
        own = first.startswith("could not build trace")
        extra = {
            "status": "inconclusive",
            "reason": f"{none} of {rows} probe rows produced no score ("
                      + ("falsepass could not build a trace for this taskset's tasks" if own
                         else "the environment raised or an episode did not finish")
                      + f"), so the numbers say little; first error: {first}",
            "failing_omitted": len(failing),
            "next": ("This is falsepass's side, not the environment's: the taskset's tasks do not have the shape the "
                     "trace builder expects (often a different verifiers version). Write an adapter that scores through "
                     "the environment's own loader (`falsepass init <target> --manual --force`; MCP: manual=true, "
                     "force=true) and report the error at https://github.com/Super-Eric/falsepass/issues" if own else
                     "Fix what stops the environment (the error above; `falsepass results <env> --completion <id>` shows a "
                     "row), then audit again. A runtime or packaging error is the environment's set-up, not its grader."),
        }  # fmt: skip
        failing = []
    elif validity.startswith("no control") and (flat := flat_scores(payload["results"], meta.get("max_reward", 1.0),
                                                                     meta.get("bad_tolerance", 0.0))):
        extra = {
            "status": "inconclusive",
            "reason": f"every reply tried scored the same on each of {flat} tasks: the replies did not reach what is "
                      "graded, and with no reference to replay nothing here shows whether the reward can be gamed",
            "failing_omitted": len(failing),
            "next": "Write policies that play the task (`falsepass init <target> --policies --force`; MCP: policies=true, "
                    "force=true). For a reward with no fixed maximum (a cost, a P&L, a game score) give the adapter "
                    "`scale_points(example)` so scores are read against a floor and a reference strategy: "
                    "https://super-eric.github.io/falsepass/docs/agents/#games-simulations-and-tool-tasks",
        }  # fmt: skip
        failing = []
    elif len(failing) > MAX_FAILING:
        extra = {"failing_omitted": len(failing) - MAX_FAILING}
        failing = failing[:MAX_FAILING]
    if validity.startswith("no control") and extra.get("status") != "inconclusive":
        # Task-blind policies only: say what each earned, and which tasks were not counted.
        extra["facts_that_hold"] = answer_free_means(payload["results"], meta.get("max_reward", 1.0), meta.get("bad_tolerance", 0.0))
        if not_counted := no_control_tasks(payload["results"]):
            extra["not_counted"] = not_counted
        extra["next"] = ("Task-blind policies only: a paid row is a lead; a row not paid says nothing about what a task-aware "
                         "policy could earn. Write policies for this task (`falsepass init <target> --policies --force`; MCP: "
                         "policies=true, force=true), with a solver as the control")  # fmt: skip
    return {
        **extra,
        "environment": meta["environment"],
        "meta": {  # the rest of the run's metadata is in probe-results.json
            **{k: meta[k] for k in ("date", "falsepass", "versions", "examples", "max_reward") if k in meta},
            "adapter": {k: adapter[k] for k in ("format", "answers", "turns", "validity", "note") if adapter.get(k)}
            if isinstance(adapter, dict) else adapter,
        },
        "headline": None if validity.startswith("inconclusive") or extra.get("status") == "inconclusive" else headline(payload["results"]),
        **({"timings": {**meta["timings"], "words": timing_words(meta["timings"])}} if meta.get("timings") else {}),
        "failing": failing,
        "static_hints": [{k: h[k] for k in ("rule", "file", "line", "evidence", "meaning", "verdict") if k in h}
                         for h in payload.get("static", [])],  # fmt: skip
        "note": "Leads, not findings: rerun with `falsepass repro`, read the reward code, then judge defect vs design.",
    }


def flat_scores(results: list[dict[str, Any]], max_reward: float = 1.0, tolerance: float = 0.0) -> int | None:
    """The number of tasks when every scored probe row (P12 aside) earned the same score on each
    task, on at least two tasks with at least two rows each, and that one score is on most
    tasks neither nothing nor the maximum; else None. Seen on a hedging simulation played
    without a solver: ten different replies, one score per task, so the replies never reached
    what is graded. A task that pays every policy nothing, or every policy the maximum (a
    refusal task), was graded and counts as reached."""
    by: dict[str, set[float]] = defaultdict(set)
    rows: dict[str, int] = defaultdict(int)
    for r in results:
        if r["probe"] != "P12" and r["score"] is not None:
            by[r["example"]].add(round(r["score"], 9))
            rows[r["example"]] += 1
    tasks = [k for k in by if rows[k] >= 2]
    if len(tasks) < 2 or not all(len(by[k]) == 1 for k in tasks):
        return None
    unreached = [k for k in tasks if not (abs(next(iter(by[k])) - tolerance) <= 1e-9 or next(iter(by[k])) >= max_reward - 1e-9)]
    return len(tasks) if len(unreached) * 2 > len(tasks) else None


def answer_free_means(results: list[dict[str, Any]], max_reward: float = 1.0, tolerance: float = 0.0) -> list[str]:
    """One line per answer-free strategy: its mean, and on how many tasks it earned the maximum
    or nothing. What a run without a control can say."""
    by: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.get("answer_free") and r["score"] is not None and r["probe"] != "P12":
            by[f'{r["probe"]} {r["name"]}/{r["variant"]}'].append(r["score"])
    lines = []
    for name, scores in sorted(by.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        full = sum(s >= max_reward - 1e-9 for s in scores)
        nothing = sum(abs(s - tolerance) <= 1e-9 for s in scores)  # a score below the tolerance (a loss) is neither
        lines.append(f"{name}: mean {sum(scores) / len(scores):.3g} over {len(scores)} tasks "
                     f"({full} at the maximum, {nothing} at nothing)")  # fmt: skip
    return lines[:12]


def no_control_tasks(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Tool-policy rows not counted (doing nothing earns the maximum on that task): the tasks,
    with one completion id each, so they can be read."""
    rows = [r for r in results if r.get("no_control")]
    if not rows:
        return None
    tasks: dict[str, str] = {}
    for r in rows:
        if r["example"] not in tasks or r["variant"] == "says_done_without_any_tool_call":
            tasks[r["example"]] = r.get("completion_id") or ""
    return {"rows": len(rows), "tasks": [{"task": k, "completion_id": v} for k, v in tasks.items()],
            "why": "doing nothing earns the maximum on these tasks but not on every task: refusing is presumably right "
                   "there, so an answer-free policy cannot be told apart from a correct one (read them)"}  # fmt: skip


def unscored(results: list[dict[str, Any]]) -> tuple[int, int, str] | None:
    """(rows, rows without a score, the first error) when at least half the probe rows (P12
    dataset checks aside) produced no score; else None."""
    rows = [r for r in results if r["probe"] != "P12"]
    none = [r for r in rows if r["score"] is None]
    if not rows or len(none) * 2 < len(rows):
        return None
    first = next((e for r in none for e in r.get("errors") or []), "none recorded")
    return len(rows), len(none), str(first)[:400]


def duration(seconds: float) -> str:
    """Seconds as people read them: 8.5 s, 42 s, 2 min 10 s, 1 h 5 min."""
    seconds = max(0.0, float(seconds))
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def timing_words(timings: dict[str, Any] | None) -> str:
    """One line on where the time went: the whole run, its three longest parts, the jobs."""
    if not timings:
        return ""
    phases = {k.replace("_", " "): v for k, v in (timings.get("phases") or {}).items()}
    if timings.get("set_up_s") is not None:
        phases["set-up (virtualenv)"] = timings["set_up_s"]
    longest = [(name, s) for name, s in sorted(phases.items(), key=lambda kv: -kv[1])[:3] if s >= 0.5]
    parts = ", ".join(f"{name} {duration(s)}" for name, s in longest)
    jobs = timings.get("jobs") or 1
    whole = duration(timings.get("total_s", 0.0) + (timings.get("set_up_s") or 0.0))
    words = f"{whole} in all" + (f" ({parts})" if parts else "") + (f"; {jobs} tasks at a time" if jobs > 1 else "; one task at a time")
    if timings.get("hub_checked") and (timings.get("phases") or {}).get("load", 0.0) >= 5:
        words += (". If the data comes from the Hugging Face Hub, the load includes the dataset library's check with the Hub: "
                  "once the data is cached, --offline (MCP: offline=true) skips it")
    return words


OTHER_ANSWER_PROBES = ("P5", "P6", "P7", "X1")  # the rows whose "wrong" answer is a complete other answer
STRUCTURED_NOTE = ("the reference is a structured answer (a plan, an action) and the wrong answer here is another task's "
                   "reference: a validity grader accepts any valid answer, so check whether this one is valid for this task "
                   "before calling it a false pass. If it is, the task has several right answers, not a lenient grader")


def structured_answers(meta: dict[str, Any]) -> bool:
    adapter = meta.get("adapter")
    return isinstance(adapter, dict) and str(adapter.get("answers", "")).startswith("JSON objects")


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}".rstrip("0").rstrip(".")


def pooled_share(results: list[dict[str, Any]], points: dict[str, list[float]], via: str | None) -> float | None:
    """For one strategy on the reference-relative scale: its total gain over the floor divided
    by the reference's total gain, Σ(raw − floor) / Σ(reference − floor). Steadier than the mean
    of per-task ratios, which a task where the reference barely beats the floor can swing."""
    gained = possible = 0.0
    for r in results:
        if f'{r["probe"]} {r["name"]}/{r["variant"]}' != via or r["score"] is None or r["example"] not in points:
            continue
        floor, reference = points[r["example"]]
        gained += (r.get("metrics") or {}).get("raw_reward", floor) - floor
        possible += reference - floor
    return gained / possible if possible > 0 else None


def _weight_lines(meta: dict[str, Any], results: list[dict[str, Any]] | None = None) -> list[str]:
    """Say so when the rubric can pay more than the reference earned: the reference used
    for the audit may then not be a full solution (e.g. `answer` is only a name or a key)."""
    scaled = meta.get("scale")
    if scaled:
        excluded = scaled.get("excluded") or {}
        return [
            f"- **Reward scale: reference-relative.** Every score below is (raw − floor) / (reference − floor) on its "
            f"task: 0 = {scaled['floor']}, 1 = {scaled['reference']}; the raw reward is in each row's metrics "
            f"(`raw_reward`). {scaled['tasks']} tasks scaled"
            + (f"; {len(excluded)} left out (first: {next(iter(excluded))}: {next(iter(excluded.values()))})" if excluded else "")
            + ". Under a reward that measures an outcome, a fixed strategy keeps what it really achieves: read the "
            "lazy-policy figure as where answer-free strategies fall between the floor and the reference."
            + (f" Pooled over its tasks (total gain over the floor ÷ the reference's), the lazy-policy strategy reaches "
               f"{_fmt(pooled)} of the reference." if (pooled := pooled_share(results or [], scaled.get("points") or {},
                                                                              headline(results)["lazy_policy_via"] if results else None)) is not None else "")
        ]
    positive = sum(w for w in (meta.get("weights") or {}).values() if w and w > 0)
    if positive > meta["max_reward"] + 1e-9:
        return [
            f"- **Reward scale:** the rubric's positive weights sum to {_fmt(positive)}, but the reference "
            f"earned {_fmt(meta['max_reward'])}. Check what the `answer` field really is before reading the "
            "numbers below as fractions of a full solution."
        ]
    return []


def _adapter_lines(adapter: Any) -> list[str]:
    if not isinstance(adapter, dict):
        return []
    table = ", ".join(f"{name} {_fmt(value)}" for name, value in adapter["reference_reward_by_format"].items())
    chosen = (f"Answer format `{adapter['format']}` chosen because the reference scored best in it "
              f"({adapter['examples_tried']} examples; {table})") if adapter["examples_tried"] else f"Answer format: {adapter['format']}"
    lines = [
        f"- **Adapter:** inferred. {chosen}. Answers: {adapter['answers']}. "
        f"{adapter.get('turns', 'single-turn').capitalize()}."
    ]
    if adapter.get("validity", "ok") != "ok":
        lines.insert(0, f"- **⚠ Result {adapter['validity']}.**")
    if adapter.get("note"):
        lines.append(f"- **Note:** {adapter['note']}.")
    return lines


def summary(payload: dict[str, Any]) -> str:
    meta, results = payload["meta"], payload["results"]
    head = headline(results)
    versions = ", ".join(f"{k} {v}" for k, v in meta["versions"].items())
    lines = [
        f"# Probe summary: {meta['environment']}",
        "",
        f"{meta['date']} · falsepass {meta['falsepass']} · {versions} · Python {meta['python']} · "
        f"{meta['examples']} examples, seed {meta['seed']}",
        "",
        *_adapter_lines(meta.get("adapter")),
        *([f"- **Time:** {timing_words(meta['timings'])}"] if meta.get("timings") else []),
        *([f"- **⚠ Result inconclusive: {unscored(results)[1]} of {unscored(results)[0]} probe rows produced no score** (the "
           f"environment raised or an episode did not finish); first error: {unscored(results)[2]}"] if unscored(results) else []),
        *_weight_lines(meta, results),
        f"- **False-pass ceiling:** {_fmt(head['false_pass_ceiling'])} of {_fmt(meta['max_reward'])} "
        f"(highest reward given to a known-bad completion; via {head['false_pass_via']})",
        f"- **Lazy-policy reward:** {_fmt(head['lazy_policy_reward'])} "
        f"(best average reward of one fixed strategy that never uses the reference answer; "
        f"via {head['lazy_policy_via']})",
        f"- **False-fail rate:** {_fmt(head['false_fail_rate'])} "
        f"({head['known_good']} known-good completions)",
        f"- **Completions on which reward code raised or timed out:** {head['scoring_errors']}",
        *([f"- **Known-bad rows not counted:** {head['same_as_good']} said exactly what a known-good completion said "
           "on the same task (marked passed; left out of the ceiling, counted in the lazy-policy reward, which a fixed "
           "strategy earns on every task)"] if head.get("same_as_good") else []),
        *([f"- **Tool-policy rows not counted:** {head['no_control']} on {head['no_control_tasks']} tasks where refusing or "
           "doing nothing is the right answer (declared by the adapter, or inferred: \"Done.\" without a tool call earns the "
           "maximum there but not on every task), so an answer-free policy cannot be told apart from a correct one there "
           "(marked passed; read those tasks)"] if head.get("no_control") else []),
        *([f"- **Note on the ceiling:** {head['false_pass_note']}; without such rows it is "
           f"{_fmt(head['false_pass_ceiling_other'])} (via {head['false_pass_other_via']})"] if head.get("false_pass_note") else []),
        "",
        "| Probe | Variant | Expect | Runs | Failed | Score min / mean / max | First failure |",
        "|---|---|---|---|---|---|---|",
    ]
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        groups[(r["probe"], r["name"], r["variant"], r["expected"])].append(r)
    for (probe, name, variant, expected), rows in groups.items():
        scores = [r["score"] for r in rows if r["score"] is not None]
        stats = f"{_fmt(min(scores))} / {_fmt(sum(scores) / len(scores))} / {_fmt(max(scores))}" if scores else "—"
        failed = [r for r in rows if not r["passed"]]
        first = failed[0]["detail"] if failed else ""
        if failed and failed[0]["score"] is None and failed[0].get("errors"):
            first = f"{first}; raised {failed[0]['errors'][0]}"
        first = first.replace("|", "\\|").replace("\n", " ")[:160]
        lines.append(f"| {probe} {name} | {variant} | {expected} | {len(rows)} | {len(failed)} | {stats} | {first} |")

    leaks = term_leak(results, meta.get("weights") or {})
    if leaks:
        lines += [
            "",
            "## Reward terms on known-bad and fixed-strategy completions",
            "",
            "Which part of the reward pays when the answer is wrong, or when a fixed strategy that never reads the task "
            "plays (expected `lazy`). A correctness term should be 0 everywhere.",
            "",
            "| Reward term | Weight | Non-zero on | Mean value |",
            "|---|---|---|---|",
        ]
        lines += [f"| {term} | {_fmt(weight)} | {share:.0%} | {_fmt(mean)} |" for term, share, mean, weight in leaks]

    hints = payload.get("static") or []
    if hints:
        lines += [
            "",
            "## Static hints, cross-checked against the probes",
            "",
            "Patterns in the reward source that are worth a look. A hint is not a finding; the last column "
            "says whether the dynamic probes agreed.",
            "",
            "| Hint | Where | Source line | Probes say |",
            "|---|---|---|---|",
        ]
        for h in hints:
            evidence = h["evidence"].replace("|", "\\|")
            lines.append(f"| {h['rule']} — {h['meaning']} | {h['file']}:{h['line']} `{h['where']}` | `{evidence}` | {h['verdict']} |")

    lint = payload.get("rewardlint")
    lines += ["", "## rewardlint corpus", ""]
    if lint is None:
        lines.append("Not run: references cannot be substituted in this environment (or rewardlint is not installed).")
    else:
        lines += [
            f"rewardlint {lint['rewardlint_version']}, stock corpus scored through this environment's reward code "
            "with the reference swapped into one real example.",
            "",
            f"- {lint['headline']}",
            f"- Profile: {lint['profile']} · false-positive rate {_fmt(lint['false_positive_rate'])} · "
            f"false-negative rate {_fmt(lint['false_negative_rate'])} · errors {len(lint['errors'])}",
        ]
        for fp in lint["false_positives"][:10]:
            lines.append(f"  - accepted `{fp['case']}` ({fp['attack'] or 'distinction'}): "
                         f"reference `{fp['reference']}`, completion `{fp['completion'][:80]!r}`")  # fmt: skip
    return "\n".join(lines) + "\n"

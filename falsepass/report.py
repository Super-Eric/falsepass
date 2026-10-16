"""Turn probe results into the short `summary.md` table."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

KNOWN_GOOD, KNOWN_BAD = "full", "zero"


def headline(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Two numbers that describe a verifier:
    false-pass ceiling — the most reward any known-bad completion received;
    false-fail rate — the share of known-good completions denied full reward."""
    bad = [r for r in results if r["expected"] == KNOWN_BAD and r["score"] is not None]
    good = [r for r in results if r["expected"] == KNOWN_GOOD]
    worst = max(bad, key=lambda r: r["score"], default=None)
    lazy_value, lazy_via = lazy_policy([r for r in results if r.get("answer_free") and r["score"] is not None])
    return {
        "lazy_policy_reward": lazy_value,
        "lazy_policy_via": lazy_via,
        "false_pass_ceiling": worst["score"] if worst else None,
        "false_pass_via": f'{worst["probe"]} {worst["name"]}/{worst["variant"]}' if worst else None,
        "false_fail_rate": (sum(not r["passed"] for r in good) / len(good)) if good else None,
        "known_good": len(good),
        "known_bad": len(bad),
        "scoring_errors": sum(bool(r["errors"]) for r in results),
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
    """Per reward term: how often and how much it paid on known-bad completions.
    Rows are (term, share of known-bad runs with a non-zero value, mean value, weight)."""
    values: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r["expected"] == KNOWN_BAD and r["score"] is not None:
            for term, value in (r.get("metrics") or {}).items():
                if term != "num_turns":
                    values[term].append(float(value))
    return [
        (term, sum(v > 0 for v in vals) / len(vals), sum(vals) / len(vals), weights.get(term))
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
        if isinstance(full, str):
            completion = preview(full, example_chars)
        failing.append({
            "probe": probe, "name": name, "variant": variant, "expected": expected,
            "runs": len(rows), "failed": len(bad),
            "scores": [r["score"] for r in bad[:5]],
            "detail": first.get("detail"), "example": first.get("example"),
            "completion": completion[:example_chars] if isinstance(completion, str) and full is None else completion,
            "completion_id": first.get("completion_id") or None,
            "answer_free": bool(first.get("answer_free")),
        })  # fmt: skip
    # False passes first (a known-bad completion paid), the most consistent ones on top; then false fails.
    failing.sort(key=lambda f: (f["expected"] != "zero", -f["failed"] / f["runs"]))
    meta = payload["meta"]
    adapter = meta.get("adapter")
    return {
        "environment": meta["environment"],
        "meta": {  # the rest of the run's metadata is in probe-results.json
            **{k: meta[k] for k in ("date", "falsepass", "versions", "examples", "max_reward") if k in meta},
            "adapter": {k: adapter[k] for k in ("format", "answers", "turns", "validity", "note") if adapter.get(k)}
            if isinstance(adapter, dict) else adapter,
        },
        "headline": headline(payload["results"]),
        "failing": failing,
        "static_hints": [{k: h[k] for k in ("rule", "file", "line", "evidence", "meaning", "verdict") if k in h}
                         for h in payload.get("static", [])],  # fmt: skip
        "note": "Leads, not findings: rerun with `falsepass repro`, read the reward code, then judge defect vs design.",
    }


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}".rstrip("0").rstrip(".")


def _weight_lines(meta: dict[str, Any]) -> list[str]:
    """Say so when the rubric can pay more than the reference earned: the reference used
    for the audit may then not be a full solution (e.g. `answer` is only a name or a key)."""
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
        *_weight_lines(meta),
        f"- **False-pass ceiling:** {_fmt(head['false_pass_ceiling'])} of {_fmt(meta['max_reward'])} "
        f"(highest reward given to a known-bad completion; via {head['false_pass_via']})",
        f"- **Lazy-policy reward:** {_fmt(head['lazy_policy_reward'])} "
        f"(best average reward of one fixed strategy that never uses the reference answer; "
        f"via {head['lazy_policy_via']})",
        f"- **False-fail rate:** {_fmt(head['false_fail_rate'])} "
        f"({head['known_good']} known-good completions)",
        f"- **Completions on which reward code raised or timed out:** {head['scoring_errors']}",
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
        first = failed[0]["detail"].replace("|", "\\|").replace("\n", " ")[:110] if failed else ""
        lines.append(f"| {probe} {name} | {variant} | {expected} | {len(rows)} | {len(failed)} | {stats} | {first} |")

    leaks = term_leak(results, meta.get("weights") or {})
    if leaks:
        lines += [
            "",
            "## Reward terms on known-bad completions",
            "",
            "Which part of the reward pays when the answer is wrong. A correctness term should be 0 everywhere.",
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

"""P12 — dataset hygiene. Pure functions over `Example`s, no scoring involved."""

from __future__ import annotations

import re
from collections import defaultdict

from falsepass.model import Example, Expect, ProbeResult


def normalise(text: str) -> str:
    return re.sub(r"[\W_]+", " ", text.lower()).strip()


def _finding(variant: str, keys: list[str], detail: str) -> ProbeResult:
    return ProbeResult("P12", "dataset_hygiene", variant, ", ".join(keys[:5]), Expect.ZERO, None, False, detail)


def check_dataset(examples: list[Example]) -> list[ProbeResult]:
    """Return one failing result per defect found, or a single passing result."""
    findings: list[ProbeResult] = []

    for ex in examples:
        if not ex.prompt.strip():
            findings.append(_finding("empty_prompt", [ex.key], "prompt is empty"))
        if not ex.answer.strip():
            findings.append(_finding("empty_answer", [ex.key], "reference answer is empty"))

    exact: dict[str, list[Example]] = defaultdict(list)
    near: dict[str, list[Example]] = defaultdict(list)
    for ex in examples:
        exact[ex.prompt].append(ex)
        near[normalise(ex.prompt)].append(ex)

    for group in exact.values():
        if len(group) > 1:
            keys = [e.key for e in group]
            answers = sorted({e.answer for e in group})
            if len(answers) > 1:
                findings.append(
                    _finding("conflicting_labels", keys, f"same prompt, different answers: {answers[:4]}")
                )
            else:
                findings.append(_finding("duplicate_prompt", keys, f"{len(group)} identical prompts"))
    for group in near.values():
        if len(group) > 1 and len({e.prompt for e in group}) > 1:
            findings.append(
                _finding("near_duplicate_prompt", [e.key for e in group],
                         f"{len(group)} prompts identical up to case/punctuation")
            )  # fmt: skip

    if findings:
        return findings
    return [
        ProbeResult("P12", "dataset_hygiene", "all_checks", f"{len(examples)} examples", Expect.ZERO, None, True,
                    "no empty prompts/answers, duplicates, near-duplicates or conflicting labels")
    ]  # fmt: skip

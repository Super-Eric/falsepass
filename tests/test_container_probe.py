"""Container probe: infer how the key maps onto the output file, then mutate it."""

import asyncio
import json
from types import SimpleNamespace

from falsepass import container_probe as cp
from falsepass.container import BoxScore


def task(expected, output="result.json", prompt="Write result.json."):
    data = SimpleNamespace(expected=expected, output_file=output, prompt_text=prompt, input_files={"raw.csv": "a,b\n1,2\n"})
    return SimpleNamespace(data=data)


def grader(check):
    async def score(task, submission):
        text = next(iter(submission.files.values()))
        reward = check(text, task.data.expected)  # may raise, like a real task's scoring
        return BoxScore(reward, {}, {})
    return score


def rejection_form(text, expected):  # pandas-agentic: the file is expected["groups"]
    try:
        got = json.loads(text)
    except ValueError:
        return 0.0
    want = expected["groups"]
    if not isinstance(got, dict) or set(got) != set(want):
        return 0.0
    for g, w in want.items():
        if int(got[g]["count"]) != w["count"]:
            return 0.0
        if abs(float(got[g]["sum"]) - w["sum"]) > 0.001:
            return 0.0
    return 1.0


KEY = {"groups": {"a": {"count": 2, "sum": 3.5}, "b": {"count": 1, "sum": 1.25}}}


def test_finds_the_file_inside_the_key_and_flags_nan_truncation_and_raises():
    results = {r.variant: r for r in asyncio.run(cp.probe_task(task(KEY), "task-0", grader(rejection_form)))}
    assert results["key['groups'] as result.json"].passed  # the control: second candidate
    assert results["floats_nan"].score == 1.0 and not results["floats_nan"].passed
    assert results["ints_plus_0.9"].score == 1.0 and not results["ints_plus_0.9"].passed
    assert results["ints_infinity"].score is None and "OverflowError" in results["ints_infinity"].detail
    assert results["first_item_dropped"].passed and results["empty_object"].passed and results["input_copied"].passed


def test_no_candidate_format_says_so():
    [result] = asyncio.run(cp.probe_task(task(KEY), "task-0", grader(lambda text, expected: 0.0)))
    assert result.variant == "no_candidate_format" and "hand-written" in result.detail


def test_csv_outputs_use_row_lists_and_the_prompt_names_the_file():
    rows = {"rows": [{"id": "A", "price": 45.5, "qty": None}, {"id": "B", "price": 12.35, "qty": 7}]}
    data = SimpleNamespace(expected=rows, prompt_text="Clean the data and write `clean.csv`.", input_files={"raw.csv": "x\n"})
    assert cp.output_files(data) == ["clean.csv"]
    [(label, value)] = cp.candidates(rows, "clean.csv")
    assert label == "key['rows']" and cp.encode(value, "clean.csv").splitlines()[1] == "A,45.5,"
    found = cp.mutations(value, "clean.csv", {})
    assert "nan" in found["floats_nan"][1] and found["header_only"][1] == "id,price,qty\n"


def test_table_counts_paid_in_full_and_raises():
    results = asyncio.run(cp.probe_task(task(KEY), "task-0", grader(rejection_form)))
    text = cp.table({"results": [r.to_dict() for r in results]})
    assert "floats_nan" in text and "1/1" in text


def test_every_file_the_prompt_names_is_tried_until_the_control_is_paid():
    data = SimpleNamespace(expected={"n": 3, "mean": 1.5}, prompt_text="Read `readings.csv`, write `result.json`.", csv_text="t,v\n1,2\n")
    assert cp.output_files(data) == ["readings.csv", "result.json"]

    async def score(task, submission):
        return BoxScore(float("result.json" in submission.files and json.loads(submission.files["result.json"]) == task.data.expected), {}, {})

    results = asyncio.run(cp.probe_task(SimpleNamespace(data=data), "task-0", score))
    assert results[0].variant == "key as result.json" and results[0].passed
    assert {r.variant for r in results if r.probe == "C2"} >= {"input_copied"}

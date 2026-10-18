"""The kit's own tests: probes must flag a leaky verifier and clear a strict one."""

import asyncio
import re

import pytest

from falsepass import Adapter, Example, Score, hygiene, probes, report


class FakeScorer:
    def __init__(self, fn):
        self.fn = fn

    async def score(self, example, completion):
        text = completion if isinstance(completion, str) else "".join(str(m.get("content") or "") for m in completion)
        return Score(self.fn(text, example.answer))

    async def close(self):
        pass


def strict(text, answer):  # last "#### N" line must equal the answer exactly
    marks = re.findall(r"^#### (\S+)\s*$", text, re.M)
    return float(bool(marks) and marks[-1] == answer and len(marks) == 1)


def leaky(text, answer):  # classic "contains" grader, plus a format bonus
    return 0.8 * float(answer in text) + 0.2 * float("####" in text)


class Toy(Adapter):
    name = "toy"

    def render(self, answer, example):
        return f"reasoning\n#### {answer}"

    def wrong_answer(self, example):
        return str(int(example.answer) + 7)

    def equivalent_forms(self, example):
        return {"trailing_period": f"reasoning\n#### {example.answer}."}


EX = Example("ex-0", "What is 6 times 7?", "42")


def run(probe, fn):
    return asyncio.run(probe(Toy(), FakeScorer(fn), EX))


@pytest.mark.parametrize("probe", probes.PER_EXAMPLE + probes.FEW_EXAMPLES)
def test_strict_verifier_only_fails_on_brittleness(probe):
    failed = [r for r in run(probe, strict) if not r.passed]
    assert {r.probe for r in failed} <= {"P2"}  # strict graders are brittle, never exploitable


def test_leaky_verifier_is_caught():
    assert not run(probes.p5_shotgun, leaky)[0].passed
    format_only = run(probes.p6_format_only, leaky)[0]
    assert format_only.score == pytest.approx(0.2) and not format_only.passed
    assert all(not r.passed for r in run(probes.p9_oversized, leaky))
    assert run(probes.p1_ground_truth, leaky)[0].passed


def test_bad_tolerance_allows_partial_credit_by_design():
    adapter = Toy()
    adapter.bad_tolerance = 0.25
    assert asyncio.run(probes.p6_format_only(adapter, FakeScorer(leaky), EX))[0].passed


def test_raising_reward_code_fails_the_exception_probe():
    def raises(text, answer):
        raise ValueError("boom")

    class Raising(FakeScorer):
        async def score(self, example, completion):
            return Score(None, {}, ["ValueError: boom"])

    results = asyncio.run(probes.p10_exception_path(Toy(), Raising(raises), EX))
    assert results and all(not r.passed and "raised" in r.detail for r in results)


def test_nondeterministic_reward_is_caught():
    counter = iter(range(1000))
    results = run(probes.p11_determinism, lambda text, answer: float(next(counter) % 2))
    assert all(not r.passed for r in results)


def test_hygiene_finds_duplicates_and_conflicts():
    examples = [
        Example("a", "What is 2+2?", "4"),
        Example("b", "What is 2+2?", "5"),
        Example("c", "what is 2 + 2", "4"),
        Example("d", "", "1"),
        Example("e", "Unique question", ""),
    ]
    variants = {r.variant for r in hygiene.check_dataset(examples)}
    assert variants == {"conflicting_labels", "near_duplicate_prompt", "empty_prompt", "empty_answer"}
    assert hygiene.check_dataset(examples[2:3])[0].passed


def test_headline_reports_ceiling_and_false_fail_rate():
    results = [r.to_dict() for probe in probes.PER_EXAMPLE for r in run(probe, leaky)]
    head = report.headline(results)
    assert head["false_pass_ceiling"] == pytest.approx(1.0)
    assert head["false_fail_rate"] == 0.0
    assert "False-pass ceiling" in report.summary(
        {"meta": {"environment": "toy", "date": "x", "falsepass": "0", "versions": {}, "python": "3",
                  "examples": 1, "seed": 0, "max_reward": 1.0, "bad_tolerance": 0.0},
         "results": results, "rewardlint": None}
    )


def test_lazy_policy_term_leak_and_gate():
    examples = [Example(f"ex-{i}", f"What is {i} plus 40?", str(i + 40)) for i in range(4)]
    results = []
    for ex in examples:
        for probe in (probes.p3_empty, probes.p6_format_only, probes.p1_ground_truth):
            for r in asyncio.run(probe(Toy(), FakeScorer(leaky), ex)):
                r.metrics = {"accuracy": 0.0 if r.expected.value == "zero" else 1.0, "format": float("####" in r.completion)}
                results.append(r.to_dict())
    head = report.headline(results)
    assert head["lazy_policy_reward"] == pytest.approx(0.2) and "format_only" in head["lazy_policy_via"]
    leaks = {term: (share, mean) for term, share, mean, _ in report.term_leak(results, {"format": 0.2})}
    assert leaks["accuracy"] == (0.0, 0.0) and leaks["format"][0] == pytest.approx(1 / 3)
    assert report.gate(results, ["ceiling>0.1", "errors>0", "false_fail>0.5"]) == ["ceiling>0.1 (actual 0.2)"]
    with pytest.raises(ValueError):
        report.gate(results, ["nonsense>1"])


def test_init_scaffolds_an_audit_folder(tmp_path, monkeypatch):
    from falsepass import cli, hub

    info = {"id": "owner/Demo_Env", "package": "demo-env", "version": "0.1.0", "module": "demo_env",
            "index": "https://example.invalid/simple/", "verifiers": "0.3.1", "declared": "verifiers>=0.1.2"}
    monkeypatch.setattr(hub, "describe", lambda env_id, version=None: dict(info, **({"version": version} if version else {})))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "owner/Demo_Env"]) == 0
    assert 'module = "demo_env"' in (tmp_path / "audits/demo-env/env.toml").read_text()
    assert not (tmp_path / "audits/demo-env/adapter.py").exists()
    assert cli.main(["init", "owner/Demo_Env"]) == 0  # the same target again: kept as it is
    assert cli.main(["init", "owner/Demo_Env", "--version", "0.0.1"]) == 1  # another version: refuses to overwrite
    assert cli.main(["init", "owner/Demo_Env", "--name", "manual", "--manual"]) == 0
    compile((tmp_path / "audits/manual/adapter.py").read_text(), "adapter.py", "exec")


def test_search_finds_what_fixed_probes_miss_and_minimises_it():
    from falsepass import search

    def padded_format_bonus(text, answer):
        """Exact-match grader with two planted defects no fixed probe targets: a bonus for
        a long <think> block regardless of the answer, and intolerance of quoted answers."""
        marks = re.findall(r"^#### (.+?)\s*$", text, re.M)
        correct = float(bool(marks) and marks[-1] == answer)
        bonus = 0.4 * float(bool(re.search(r"<think>.{300,}</think>", text, re.S)))
        return max(correct, bonus)

    results = {r.probe: r for r in asyncio.run(search.make_probe(150)(Toy(), FakeScorer(padded_format_bonus), EX))}
    assert not results["S1"].passed and results["S1"].score == pytest.approx(0.4)
    assert "pad_think" in results["S1"].detail and results["S1"].detail.count(" + ") <= 2  # minimised
    assert "42" not in results["S1"].completion.replace(EX.prompt, "")  # never smuggles the answer in
    assert not results["S2"].passed and results["S2"].score == 0.0
    assert results["S1"].answer_free and not results["S3"].answer_free


def test_search_clears_a_strict_verifier_on_false_pass():
    from falsepass import search

    results = {r.probe: r for r in asyncio.run(search.make_probe(150)(Toy(), FakeScorer(strict), EX))}
    assert results["S1"].passed and results["S3"].passed


LEAKY_SOURCE = '''
import re
import verifiers as vf

def parse(completion):
    return completion[-1]["content"]

def accuracy(completion, answer, **kwargs):
    return float(parse(completion) == answer)

def thinking(completion, **kwargs):
    text = completion[-1]["content"]
    if len(text) > 200:
        return 1.0
    return 0.0

def is_long(text):
    return len(text) > 500

def lenient(completion, answer):
    matches = re.findall(r"#### (.+)", completion)
    prediction = matches[-1] if matches else completion
    try:
        return float(re.search(r"\\d+", prediction).group() == answer)
    except Exception:
        return 1.0

rubric = vf.Rubric(funcs=[accuracy, thinking, lenient], weights=[0.7, 0.2, 0.1])
'''


def test_static_scan_flags_only_reward_code(tmp_path):
    from falsepass import static

    (tmp_path / "env.py").write_text(LEAKY_SOURCE)
    hints = static.scan(tmp_path)
    by_rule = {h.rule: h for h in hints}
    assert set(by_rule) == {"term_ignores_reference", "length_threshold", "whole_text_fallback",
                            "unanchored_value_pattern", "except_returns_reward"}
    assert "thinking (weight 0.2)" in by_rule["term_ignores_reference"].where
    assert all(h.where != "is_long" for h in hints)
    assert static.scan_file(tmp_path / "env.py")[0].line > 0


def test_static_hints_are_cross_checked_against_probe_results():
    from falsepass import static

    hints = [{"rule": "term_ignores_reference", "confirm_with": ["P6"]},
             {"rule": "except_returns_reward", "confirm_with": ["P10"]},
             {"rule": "whole_text_fallback", "confirm_with": ["S3", "X1"]},
             {"rule": "unpinned_grader_dependency", "confirm_with": []}]
    results = [{"probe": "P6", "passed": False}, {"probe": "P6", "passed": False}, {"probe": "P10", "passed": True}]
    verdicts = [h["verdict"] for h in static.cross_check(hints, results)]
    assert verdicts[0] == "confirmed: P6 failed 2/2"
    assert verdicts[1].startswith("not confirmed") and verdicts[2].startswith("not checked")
    assert verdicts[3] == "not testable by probes"


def test_auto_adapter_infers_format_and_wrong_answers():
    from falsepass.auto import AutoAdapter, candidate_formats

    adapter = AutoAdapter({"id": "toy/boxed", "package": "toy"})
    adapter.pool = [Example(f"e{i}", "Give the final answer in \\boxed{}.", label) for i, label in enumerate("ABCD" * 5)]
    adapter.labels, adapter.closed = sorted({e.answer for e in adapter.pool}), True
    adapter.hint = ("Give the final answer in \\boxed{}.", None)

    def boxed_only(text, answer):
        found = re.findall(r"\\boxed\{(\w)\}", text)
        return float(bool(found) and found[-1] == answer)

    asyncio.run(adapter.prepare(FakeScorer(boxed_only), adapter.pool[:5]))
    assert adapter.inferred["format"].endswith("boxed") and adapter.max_reward == 1.0
    example = adapter.pool[0]
    assert adapter.render("A", example).endswith("\\boxed{A}")
    assert adapter.wrong_answer(example) == "B" and adapter.shotgun_answers(example) == list("ABCD")
    assert "bare" in candidate_formats("no hints at all")
    numeric = AutoAdapter({"id": "toy/num", "package": "toy"})
    numeric.pool, numeric.closed, numeric.labels = [Example("n", "q", "1,000")], False, ["1,000"]
    assert numeric.wrong_answer(numeric.pool[0]) == "1007"


def test_auto_adapter_ignores_tags_that_wrap_input_and_flags_references_with_their_own_markup():
    from falsepass.auto import AutoAdapter, candidate_formats

    prompt = "<conversation>\n" + "User: I live in Delhi.\n" * 20 + "</conversation>\nReply as <answer>city</answer>."
    formats = candidate_formats(prompt)
    assert "<answer>" in formats and "<conversation>" not in formats

    adapter = AutoAdapter({"id": "toy/tagged", "package": "toy"})
    adapter.pool = [Example(f"e{i}", "Reply in <answer> tags.", f"<answer>{i}</answer>") for i in range(6)]
    adapter.labels, adapter.closed, adapter.hint = [], False, ("Reply in <answer> tags.", None)
    asyncio.run(adapter.prepare(FakeScorer(lambda text, answer: float(answer in text)), adapter.pool[:5]))
    assert "markup of their own" in adapter.inferred["note"]


def test_auto_adapter_marks_results_inconclusive_when_reward_ignores_the_answer():
    from falsepass.auto import AutoAdapter

    adapter = AutoAdapter({"id": "toy/pays-everything", "package": "toy"})
    adapter.pool = [Example(f"e{i}", "Play the game.", "x") for i in range(6)]
    adapter.labels, adapter.closed, adapter.hint = ["x"], False, ("Play the game.", None)
    asyncio.run(adapter.prepare(FakeScorer(lambda text, answer: 1.0), adapter.pool[:5]))
    assert adapter.inferred["validity"].startswith("inconclusive")
    assert "inconclusive" in report.summary(
        {"meta": {"environment": "toy", "date": "x", "falsepass": "0", "versions": {}, "python": "3", "examples": 1,
                  "seed": 0, "max_reward": 1.0, "bad_tolerance": 0.0, "adapter": adapter.inferred},
         "results": [], "rewardlint": None})


def test_classify_reads_environment_kind_from_source():
    from falsepass import hub

    single = b"import verifiers as vf\nrow = {'question': q, 'answer': str(a)}\nenv = vf.SingleTurnEnv(dataset=d)\n"
    game = b"import verifiers as vf\nrow = {'question': q, 'answer': ''}\nclass G(vf.MultiTurnEnv): ...\n"
    tools = b"import verifiers as vf\nrow = {'answer': a}\nenv = vf.ToolEnv(tools=[f])\n"
    assert hub.classify({"e.py": single})["automatic"]
    assert not hub.classify({"e.py": game})["has_reference"] and not hub.classify({"e.py": game})["automatic"]
    assert hub.classify({"e.py": tools})["needs"] == ["tools"]


def test_static_scan_flags_reward_terms_that_accept_but_never_read_the_answer(tmp_path):
    from falsepass import static

    (tmp_path / "env.py").write_text(
        "import verifiers as vf\n"
        "async def style(completion, answer, **kwargs):\n    return float('because' in completion[-1]['content'])\n"
        "async def accuracy(completion, answer, **kwargs):\n    return float(answer in completion[-1]['content'])\n"
        "rubric = vf.Rubric(funcs=[style, accuracy], weights=[0.5, 0.5])\n"
    )
    flagged = [h.where for h in static.scan(tmp_path) if h.rule == "term_ignores_reference"]
    assert flagged == ["style (weight 0.5)"]


def test_auto_adapter_accepts_a_low_but_separated_reference():
    from falsepass.auto import AutoAdapter

    adapter = AutoAdapter({"id": "toy/low-ceiling", "package": "toy"})
    adapter.pool = [Example(f"e{i}", "Explain.", "the answer text") for i in range(6)]
    adapter.labels, adapter.closed, adapter.hint = ["the answer text"], False, ("Explain.", None)
    asyncio.run(adapter.prepare(FakeScorer(lambda text, answer: 0.4 * float(answer in text)), adapter.pool[:5]))
    assert adapter.inferred["validity"] == "ok" and adapter.max_reward == pytest.approx(0.4)


def test_static_scan_flags_a_reward_term_that_uses_an_unimported_module(tmp_path):
    from falsepass import static

    (tmp_path / "env.py").write_text(
        "import verifiers as vf\n"
        "async def numbers(completion, answer, **kwargs):\n    import re\n    return float(bool(re.search(answer, completion[-1]['content'])))\n"
        "async def concepts(completion, answer, **kwargs):\n    return float(bool(re.search(answer, completion[-1]['content'])))\n"
        "rubric = vf.Rubric(funcs=[numbers, concepts], weights=[0.5, 0.5])\n"
    )
    flagged = [h.where for h in static.scan(tmp_path) if h.rule == "undefined_name"]
    assert flagged == ["concepts: `re`"]  # `numbers` imports it locally, `concepts` does not


def test_static_scan_flags_substring_matching_and_single_letter_verdicts(tmp_path):
    from falsepass import static

    (tmp_path / "env.py").write_text(
        "import re\n"
        "def _contains(needle, haystack):\n    return needle.lower() in haystack.lower()\n"
        "def has_marker(text):\n    return '####' in text\n"
        "def lookup(answer, info):\n    return answer in info\n"
        "def parse_grade(text):\n"
        "    return next((w for w in re.split(r'[^A-Z]+', text.upper()) if w in ('A', 'B', 'C')), None)\n"
        "def label_ok(label):\n    return label in ('A', 'B', 'C')\n"
    )
    found = {(h.rule, h.where) for h in static.scan(tmp_path)}
    assert ("reference_substring_match", "_contains") in found
    assert ("single_letter_token", "parse_grade") in found
    # a fixed marker, membership in a mapping, and a whole label compared with a set are not flagged
    assert not {w for r, w in found if w in ("has_marker", "lookup", "label_ok")}


def test_propose_summary_counts_wrong_answers_that_were_paid():
    from falsepass import propose

    row = {"example": "row-1", "answer": "ref", "reference_reward": 0.6, "flag": "wrong answer paid at least as much as the reference",
           "first_reward": 0.4, "best_reward": 0.7, "best_round": 2, "model_says": "contradicts", "rejected": 0, "best_text": "x | y", "history": []}
    text = propose.summary({"environment": "toy/env", "model": "m", "endpoint": "e", "rounds": 3, "seed": 0, "max_reward": 1.0,
                            "open_ended": True, "rows": [row]})
    assert "after the rewrites:** 0.700" in text and "1 of 1" in text and "x \\| y" in text
    assert "not verified by a person" in text


# --- P13 / P14 / T7: defects found while sending fixes upstream ------------------------


def rejection_form(text, answer):  # pandas-agentic F1: `if abs(x - want) > tol: return 0`
    marks = re.findall(r"#### (\S+)", text)
    if not marks:
        return 0.0
    try:
        got = float(marks[-1])
    except ValueError:
        return 0.0
    if abs(got - float(answer)) > 0.001:
        return 0.0
    return 1.0


def truncating(text, answer):  # pandas-agentic F2: `int(got) == want`
    marks = re.findall(r"#### (\S+)", text)
    try:
        return float(bool(marks) and int(float(marks[-1])) == int(answer))
    except (ValueError, OverflowError):
        return 0.0


def test_special_numbers_catch_rejection_form_and_truncation():
    nan = {r.variant: r for r in run(probes.p13_special_numbers, rejection_form)}
    assert not nan["nan"].passed and nan["nan"].answer_free and nan["infinity"].passed
    trunc = {r.variant: r for r in run(probes.p13_special_numbers, truncating)}
    assert not trunc["integer_plus_0.9"].passed and trunc["nan"].passed
    assert all(r.passed for r in run(probes.p13_special_numbers, strict))


def test_special_numbers_for_json_answers():
    cases = probes.special_number_answers('{"a": {"count": 8, "sum": 20.5}}')
    assert set(cases) == {"json_floats_nan", "json_ints_plus_0.9", "json_ints_infinity"}
    assert '"sum": NaN' in cases["json_floats_nan"][0] and '"count": 8.9' in cases["json_ints_plus_0.9"][0]
    assert probes.special_number_answers("Paris") == {}


class Tagged(Toy):
    def render(self, answer, example):
        return f"<answer>{answer}</answer>"


def first_tag(text, answer):  # the first opener decides
    found = re.search(r"<answer>(.*?)</answer>", text, re.DOTALL)
    return float(bool(found) and found.group(1).strip() == answer)


def last_tag(text, answer):  # the obvious fix: last block, but a prose opener still swallows the next one
    found = re.findall(r"<answer>(.*?)</answer>", text, re.DOTALL)
    return float(bool(found) and found[-1].strip() == answer)


def last_nonempty_tag(text, answer):
    found = [b.strip() for b in re.findall(r"<answer>((?:(?!<answer>).)*?)</answer>", text, re.DOTALL) if b.strip()]
    return float(bool(found) and found[-1] == answer)


def test_format_mentions_tell_first_last_and_last_nonempty_apart():
    def failed(fn):
        return {r.variant for r in asyncio.run(probes.p14_format_mentions(Tagged(), FakeScorer(fn), EX)) if not r.passed}

    assert failed(first_tag) == {"tag_named_in_prose_before", "empty_tag_pair_named_before"}
    assert failed(last_tag) == {"tag_named_in_prose_before", "empty_tag_pair_named_after"}
    assert failed(last_nonempty_tag) == set()
    assert probes.format_mentions("reasoning\n#### 42", "42") == {}  # no paired wrapper
    assert set(probes.format_mentions("\\boxed{42}", "42")) == {"empty_box_named_before", "empty_box_named_after"}
    # A dataset that stores the whole block as the reference (wrapper included, rendered bare)
    block = "<answer>\n3\n1 0\n</answer>"
    assert set(probes.format_mentions(block, block)) == {"tag_named_in_prose_before", "empty_tag_pair_named_before",
                                                         "empty_tag_pair_named_after"}  # fmt: skip
    assert probes.format_mentions("<a>1</b>", "<a>1</b>") == {}  # not one paired wrapper


class ListScorer(FakeScorer):
    async def score(self, example, completion):
        return Score(self.fn(completion, example.answer))


def first_assistant(messages, answer):  # message n answers user turn n
    replies = [m.get("content") or "" for m in ([{"role": "assistant", "content": messages}] if isinstance(messages, str) else messages)
               if m.get("role") == "assistant"]
    return strict(replies[0], answer) if replies else 0.0


def test_tool_call_first_catches_turn_indexing():
    [result] = asyncio.run(probes.t7_tool_call_first(Toy(), ListScorer(first_assistant), EX))
    assert not result.passed and result.score == 0
    [result] = asyncio.run(probes.t7_tool_call_first(Toy(), FakeScorer(strict), EX))
    assert result.passed


def test_static_scan_flags_empty_components_and_unread_prompts(tmp_path):
    from falsepass import static

    (tmp_path / "core.py").write_text('''
def score_amounts(parsed, truth):
    hits = [abs(parsed[k] - truth[k]) < 1 for k in truth if parsed.get(k) is not None]
    return sum(hits) / len(hits) if hits else 0.0


def score_periods(parsed, truth):
    checks = [parsed.get(k) == v for k, v in truth.items()]
    if not checks:
        return 0.0
    return sum(checks) / len(checks)


def score_flag(parsed, truth):
    return 1.0 if parsed.get("flag") == truth["flag"] else 0.0
''')
    (tmp_path / "prompts.py").write_text(
        'SYSTEM_PROMPT = """' + "Answer with a JSON object. " * 8 + '"""\n'
        'USED_PROMPT = """' + "Be brief. " * 12 + '"""\n'
        'SHORT_PROMPT = "hi"\n'
    )
    (tmp_path / "env.py").write_text("from prompts import USED_PROMPT\n\n\ndef load_environment():\n    return USED_PROMPT\n")
    hints = static.scan(tmp_path)
    assert sorted(h.where for h in hints if h.rule == "empty_component_scores_zero") == ["score_amounts", "score_periods"]
    assert [h.where for h in hints if h.rule == "unreferenced_prompt_constant"] == ["SYSTEM_PROMPT"]
    assert [h.where for h in static.scan(tmp_path / "prompts.py") if h.rule == "unreferenced_prompt_constant"] == ["SYSTEM_PROMPT", "USED_PROMPT"]


def test_static_rules_from_the_pull_requests(tmp_path):
    from falsepass import static

    (tmp_path / "taskset.py").write_text('''import re
import verifiers.v1 as vf
_TAG = re.compile(r"<reversed_text>(.*?)</reversed_text>", re.DOTALL)
SAFE = re.compile(r"(\\d+)")


def _score_reconcile(sub, want, tol):
    if int(sub["count"]) != want["count"]:
        return 0.0
    if abs(float(sub["sum"]) - want["sum"]) > tol:
        return 0.0
    if not abs(float(sub["mean"]) - want["mean"]) <= tol:
        return 0.0
    if len(sub) != 3:
        return 0.0
    return 1.0


@vf.reward(weight=1.0)
def lcs(trace):
    found = _TAG.search(trace.last_reply)
    other = SAFE.search(trace.last_reply)
    return found


def alphabet_sort(trace, turns):
    responses = [m.content or "" for m in trace.assistant_messages]
    last = responses[-1]
    for t in range(turns):
        response = responses[t]
''')
    rules = sorted((h.rule, h.line) for h in static.scan(tmp_path))
    assert rules == [
        ("assistant_messages_by_turn", 30), ("first_tag_match", 21), ("rejection_form_tolerance", 10), ("truncated_count", 8),
    ]


def right_hand_side(text, answer):  # math-verify on `\boxed{W = R}`: keeps the right-hand side
    marks = re.findall(r"#### (.+)", text)
    if not marks:
        return 0.0
    value = marks[-1].split("=")[-1].strip()
    value = value.split(" (")[0].split(" \\text{")[0].split(" or ")[-1].split(" \\lor ")[-1]
    value = value.removeprefix("not ").strip()
    return float(value == answer)


def test_hedges_inside_the_slot_are_caught():
    failed = {r.variant for r in run(probes.p15_hedged_in_slot, right_hand_side) if not r.passed}
    assert failed == {"false_equation", "denied", "denied_latex", "not_answer", "either_or", "either_or_latex"}  # not the reversed ones
    assert all(r.passed for r in run(probes.p15_hedged_in_slot, strict))


def test_static_scan_flags_math_verify_on_the_whole_slot(tmp_path):
    from falsepass import static

    (tmp_path / "verify.py").write_text(
        "from math_verify import parse, verify as check\n"
        "import math_verify as mv\n\n"
        "def score(prediction, gold):\n"
        "    a = check(parse('\\\\boxed{' + gold + '}'), parse('\\\\boxed{' + prediction + '}'))\n"
        "    return a or mv.verify(parse(gold), parse(prediction))\n"
    )
    hints = [h for h in static.scan(tmp_path) if h.rule == "math_verify_whole_slot"]
    assert [h.line for h in hints] == [5, 6]
    (tmp_path / "verify.py").write_text("def verify(a, b):\n    return a == b\n\nverify(1, 1)\n")  # not math_verify
    assert not [h for h in static.scan(tmp_path) if h.rule == "math_verify_whole_slot"]


def test_field_echo_replaces_prose_values_with_the_prompt():
    import json

    answer = json.dumps({"label": "hack", "why": "changed the results", "quote": "the result file", "ids": ["E1"], "n": 3})
    prompt = [{"role": "system", "content": "Answer in JSON."}, {"role": "user", "content": "The agent edited the result file."}]
    echoed = json.loads(probes.field_echoes(answer, prompt)["prose_fields_are_prompt"])
    pasted = "The agent edited the result file."
    assert echoed == {"label": "hack", "why": pasted, "quote": "the result file", "ids": ["E1"], "n": 3}  # quotes kept
    assert probes.field_echoes(json.dumps({"label": "hack", "n": 3}), prompt) == {}  # no prose: no row
    assert probes.field_echoes("42", prompt) == {}


def test_correct_variants_are_judged_against_their_own_tasks_reference():
    import asyncio

    from falsepass.auto import AutoAdapter
    from falsepass.model import Example, Expect, Score
    from falsepass.probes import check

    class TwoTiers:
        """A full answer earns 0.99 on easy tasks and 0.5 (the first of two steps) on hard ones."""

        async def score(self, example, completion):
            top = 0.99 if example.key.startswith("easy") else 0.5
            return Score(top if completion.strip() == example.answer else 0.0)

    adapter = AutoAdapter({"id": "t", "framework": "verifiers"})
    adapter.format = lambda a: a
    adapter.max_reward = 0.745
    examples = [Example("easy-0", "q", "7"), Example("hard-0", "q", "9")]
    note = asyncio.run(adapter._task_maxima(TwoTiers(), examples))
    assert adapter.task_max == {"easy-0": 0.99, "hard-0": 0.5} and "between 0.5 and 0.99" in note
    p2 = asyncio.run(check("P2", "equivalent_forms", "surrounding_whitespace", Expect.FULL, " 9 ", adapter, TwoTiers(), examples[1]))
    p1 = asyncio.run(check("P1", "ground_truth", "reference", Expect.FULL, "9", adapter, TwoTiers(), examples[1]))
    assert p2.passed and not p1.passed  # P1 still shows that a full answer earns less on this task

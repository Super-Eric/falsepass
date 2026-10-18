"""Static pre-scan: read the reward code and say where to look before probing.

This is a small, deliberately narrow AST pass. Each rule describes one way a verifier
was actually found to be wrong during the audits in this repository, and each names the
probe that confirms or refutes it. A hint is a reason to look, not a finding: the report
shows next to every hint whether the dynamic probes agreed.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

REFERENCE_NAMES = {"answer", "answers", "info", "task", "state", "trace", "data", "entry", "reference", "gold", "kwargs"}
MODEL_TEXT_NAMES = {"prediction", "pred", "completion", "response", "reply", "output", "text"}
REWARD_NAME = re.compile(r"reward|score|verify|grade|correct", re.I)
VALUE_WORD = re.compile(r"answer|expected|gold|truth|needle|target|reference|value", re.I)
TEXT_WORD = re.compile(r"text|reply|response|completion|pred|output|generation|haystack|content|parsed", re.I)
PROCESS_CALLS = {"run_uv_script", "run", "Popen", "check_output", "check_call", "call"}

# rule -> (probe codes that confirm it, one-line meaning)
RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "term_ignores_reference": (("P6",), "reward term never reads the reference answer, so it pays regardless of correctness"),
    "length_threshold": (("P6", "S1"), "reward depends on the length of the text"),
    "whole_text_fallback": (("S3", "X1"), "when extraction finds nothing, the unparsed text is graded instead"),
    "unanchored_value_pattern": (("S3", "P5"), "answer is picked out of the text by a pattern with no surrounding context"),
    "except_returns_reward": (("P10",), "an exception inside the reward code is turned into a non-zero reward"),
    "model_text_in_argv": (("P10",), "model output is passed as a command-line argument; NUL bytes and very long outputs raise"),
    "undefined_name": (("P1", "P10"), "reward code reads a name that is never defined or imported; it raises on every call and verifiers scores 0"),
    "reference_substring_match": (("P9", "P5", "S3"), "the reference is looked for anywhere in the text (`answer in text`), so a reply that denies it or lists it among others matches too"),
    "single_letter_token": (("P8",), "a verdict is read as the first single-letter token of a text; an ordinary word such as the article \"a\" is such a token"),
    "unpinned_grader_dependency": ((), "the grader script depends on a package without a version pin, so grading can change over time"),
    "rejection_form_tolerance": (("P13", "C1"), "a tolerance check written as a rejection (`if abs(x - want) > tol: return 0`) never fires for NaN, so NaN passes"),
    "truncated_count": (("P13", "C1"), "a submitted number goes through `int()` before the comparison: 8.9 counts as 8, and Infinity raises OverflowError"),
    "first_tag_match": (("P14",), "the answer is the first match of a tag pattern, so a tag named in prose before the answer block decides what is graded"),
    "assistant_messages_by_turn": (("T7",), "assistant messages are indexed by user turn; a message that only calls a tool shifts every later turn"),
    "hud_contains_grader": (("P5", "P9"), "HUD's `contains()` / `contains_any()` / `contains_all()` graders are substring tests, so a reply that lists several answers, or names the right one and rejects it, passes"),
    "math_verify_whole_slot": (("P15",), "the answer slot goes to math_verify's `parse()` whole, which pulls one value out of whatever is there: `\\boxed{107 = 100}` is compared by its right-hand side and `\\boxed{100 \\text{ is wrong}}` as 100, so a denial or a false equation inside the slot is paid"),
    "inspect_includes_scorer": (("P5", "P9"), "Inspect's `includes()` scorer passes when the target appears anywhere in the output, so a reply that lists several answers, or names the right one and rejects it, passes"),
    "inspect_match_end": (("P15", "P9"), "Inspect's `match()` reads only the end of the output (the default `location=\"end\"`; with `numeric=True`, the last number in it) and ignores what comes before: `not 100` matches 100, and with `numeric=True` so does `100 (this is wrong)`. Documented inspect_ai behaviour: a design choice unless the task asks for more"),
    "inspect_choice_lenient": (("P15",), "Inspect's `choice()` scorer takes the letters `multiple_choice()` parsed, and that parser falls back to the letters right after `ANSWER:` when the line goes on, so `ANSWER: A (this is wrong)` counts as A. Documented inspect_ai behaviour: a design choice unless the task asks for more"),
    "empty_component_scores_zero": ((), "a per-item average that returns 0.0 when there are no items (`... if hits else 0.0`), so a component whose items were all excluded on purpose (for example after a correct abstention) counts as a failure instead of being skipped"),
    "unreferenced_prompt_constant": ((), "a module-level *_PROMPT string that nothing in the package reads: the instructions in it (answer format, when to abstain) never reach the model, while the grader may still expect them to be followed"),
}

PROMPT_NAME = re.compile(r"(^|_)PROMPT(_|$)")

WANT_WORD = re.compile(r"want|expect|answer|gold|truth|target|reference|key", re.IGNORECASE)
TAG_PATTERN = re.compile(r"<[A-Za-z_][\w-]*>.*\(.*\).*</")


@dataclass
class Hint:
    rule: str
    file: str
    line: int
    where: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "meaning": RULES[self.rule][1], "confirm_with": list(RULES[self.rule][0])}


def _names(node: ast.AST) -> set[str]:
    found = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    found |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
    found |= {n.arg for n in ast.walk(node) if isinstance(n, ast.arg)}
    return found


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _literal_pattern(node: ast.AST) -> str | None:
    """The constant text of a regex argument (f-string placeholders become empty)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return None


def _has_context(pattern: str) -> bool:
    """True if the pattern is anchored or requires at least 3 literal characters around the value."""
    if re.search(r"(^\^)|(\$$)|\\A|\\Z", pattern):
        return True
    literal = re.sub(r"\\[bBdDsSwW]|\\.|\[[^\]]*\]|\(\?[:=!<]+|[()|?*+{}.,\d]", "", pattern)
    return len(literal.replace(" ", "")) >= 3


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, source: str) -> None:
        self.path, self.lines = path, source.splitlines()
        self.hints: list[Hint] = []
        self.weighted: dict[str, float | None] = {}  # reward function name -> weight if literal
        self.metric_only: set[str] = set()
        self.substring_lines: set[int] = set()

    def hint(self, rule: str, node: ast.AST, where: str) -> None:
        line = getattr(node, "lineno", 1)
        if any(h.rule == rule and h.line == line for h in self.hints):
            return
        self.hints.append(Hint(rule, self.path, line, where, self.lines[line - 1].strip()[:140]))

    # --- which functions are reward terms, and with what weight -------------------------
    def collect_rubric(self, tree: ast.AST) -> None:
        for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
            keywords = {k.arg: k.value for k in call.keywords if k.arg}
            funcs = keywords.get("funcs")
            if isinstance(funcs, (ast.List, ast.Tuple)):
                weights = keywords.get("weights")
                values = weights.elts if isinstance(weights, (ast.List, ast.Tuple)) else []
                for i, element in enumerate(funcs.elts):
                    if isinstance(element, ast.Name):
                        weight = values[i].value if i < len(values) and isinstance(values[i], ast.Constant) else None
                        self.weighted[element.id] = weight if values else 1.0
            name = _call_name(call)
            if name in ("add_reward_func", "add_metric") and call.args and isinstance(call.args[0], ast.Name):
                weight = keywords.get("weight")
                literal = weight.value if isinstance(weight, ast.Constant) else (None if weight else 1.0)
                if name == "add_metric" or literal == 0:
                    self.metric_only.add(call.args[0].id)
                else:
                    self.weighted[call.args[0].id] = literal

    @staticmethod
    def _bound(node: ast.AST, descend_into_functions: bool) -> set[str]:
        """Names bound by statements under `node`. Function and class bodies are separate
        scopes: an `import re` inside one function does not define `re` for another."""
        names: set[str] = set()
        stack = list(ast.iter_child_nodes(node))
        while stack:
            child = stack.pop()
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                names |= {(a.asname or a.name).split(".")[0] for a in child.names}
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(child.name)
                if not descend_into_functions:
                    continue
            elif isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                names.add(child.id)
            elif isinstance(child, ast.arg):
                names.add(child.arg)
            elif isinstance(child, ast.ExceptHandler) and child.name:
                names.add(child.name)
            stack.extend(ast.iter_child_nodes(child))
        return names

    def collect_module_names(self, tree: ast.Module) -> None:
        import builtins

        self.module_names = set(dir(builtins)) | {"__file__", "__name__"} | self._bound(tree, descend_into_functions=False)
        self.star_import = any(isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names) for n in ast.walk(tree))
        # Module-level `NAME = re.compile("<tag>(...)</tag>")`: `NAME.search(...)` takes the first match.
        self.tag_patterns = {
            target.id
            for node in tree.body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
            and _call_name(node.value) == "compile" and node.value.args
            and TAG_PATTERN.search(_literal_pattern(node.value.args[0]) or "")
            for target in node.targets if isinstance(target, ast.Name)
        }  # fmt: skip
        # Enclosing-function scopes: a reward function defined inside load_environment()
        # may use anything bound there.
        self.enclosing: dict[ast.AST, set[str]] = {}
        for outer in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            bound = self._bound(outer, descend_into_functions=False) | {a.arg for a in ast.walk(outer.args) if isinstance(a, ast.arg)}
            for inner in ast.walk(outer):
                if inner is not outer and isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.enclosing.setdefault(inner, set()).update(bound)

    def is_reward_term(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        decorated = any("reward" in ast.unparse(d) for d in node.decorator_list)
        return (node.name in self.weighted or decorated) and node.name not in self.metric_only

    # --- rules ----------------------------------------------------------------------------
    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        reward_term = self.is_reward_term(node)
        reward_like = reward_term or bool(REWARD_NAME.search(node.name))
        # Names *read* in the body: accepting an `answer` parameter is not the same as using it.
        read = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        read |= {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
        if reward_term and self.weighted.get(node.name, 1.0) != 0 and not (read & REFERENCE_NAMES - {"kwargs"}):
            uses_kwargs = "kwargs" in read
            if not uses_kwargs:
                weight = self.weighted.get(node.name)
                self.hint("term_ignores_reference", node, f"{node.name} (weight {weight if weight is not None else '?'})")
        if reward_term and not self.star_import:
            seen: set[str] = set()
            known = self.module_names | self.enclosing.get(node, set()) | self._bound(node, descend_into_functions=True)
            known |= {a.arg for a in ast.walk(node.args) if isinstance(a, ast.arg)}
            for name in (n for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)):
                if name.id not in known and name.id not in seen:
                    seen.add(name.id)
                    self.hint("undefined_name", name, f"{node.name}: `{name.id}`")
        if reward_like:
            self.scan_body(node, node.name, {a.arg for a in node.args.args})
        self.scan_turn_indexing(node)
        # `answer in text`, in any function: graders keep this in small helpers (`_contains`, `grade`).
        for compare in (n for n in ast.walk(node) if isinstance(n, ast.Compare)):
            if len(compare.ops) != 1 or not isinstance(compare.ops[0], (ast.In, ast.NotIn)) or compare.lineno in self.substring_lines:
                continue
            left, right = compare.left, compare.comparators[0]
            if isinstance(left, ast.Constant) or isinstance(right, (ast.Tuple, ast.List, ast.Set, ast.Dict, ast.Constant)):
                continue  # a fixed marker looked for in the text, or membership in a fixed collection
            if any(VALUE_WORD.search(n) for n in _names(left)) and any(TEXT_WORD.search(n) for n in _names(right)):
                self.substring_lines.add(compare.lineno)
                self.hint("reference_substring_match", compare, node.name)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def scan_turn_indexing(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """`replies = [... for m in trace.assistant_messages]` then `replies[t]` with a variable `t`."""
        lists = {
            t.id
            for n in ast.walk(node) if isinstance(n, ast.Assign)
            and any(isinstance(a, ast.Attribute) and a.attr == "assistant_messages" for a in ast.walk(n.value))
            for t in n.targets if isinstance(t, ast.Name)
        }  # fmt: skip
        for sub in (n for n in ast.walk(node) if isinstance(n, ast.Subscript)):
            base = sub.value
            direct = isinstance(base, ast.Attribute) and base.attr == "assistant_messages"
            if (direct or (isinstance(base, ast.Name) and base.id in lists)) and isinstance(sub.slice, ast.Name):
                self.hint("assistant_messages_by_turn", sub, node.name)
                return

    def scan_body(self, scope: ast.AST, where: str, params: set[str]) -> None:
        length_names = {
            t.id
            for n in ast.walk(scope) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
            and _call_name(n.value) == "len" for t in n.targets if isinstance(t, ast.Name)
        }  # fmt: skip
        averaged: set[str] = set()  # names divided by their own length, or passed to a mean: per-item averages
        for n in ast.walk(scope):
            if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div) and isinstance(n.right, ast.Call) and _call_name(n.right) == "len":
                if n.right.args and isinstance(n.right.args[0], ast.Name):
                    averaged.add(n.right.args[0].id)
            elif isinstance(n, ast.Call) and _call_name(n) in ("mean", "fmean") and n.args and isinstance(n.args[0], ast.Name):
                averaged.add(n.args[0].id)
        for node in ast.walk(scope):
            if isinstance(node, ast.IfExp) and isinstance(node.test, ast.Name) and node.test.id in averaged and _is_zero(node.orelse):
                self.hint("empty_component_scores_zero", node, where)
            if (isinstance(node, ast.If) and isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)
                    and isinstance(node.test.operand, ast.Name) and node.test.operand.id in averaged and len(node.body) == 1
                    and isinstance(node.body[0], ast.Return) and _is_zero(node.body[0].value)):  # fmt: skip
                self.hint("empty_component_scores_zero", node, where)
            if isinstance(node, ast.Compare):
                sides = [node.left, *node.comparators]
                measures = any(
                    (isinstance(s, ast.Call) and _call_name(s) == "len") or (isinstance(s, ast.Name) and s.id in length_names)
                    for s in sides
                )
                big = any(isinstance(s, ast.Constant) and isinstance(s.value, (int, float)) and s.value >= 20 for s in sides)
                if measures and big:
                    self.hint("length_threshold", node, where)
                if len(node.ops) == 1 and isinstance(node.ops[0], ast.In):
                    left, right = node.left, node.comparators[0]
                    if isinstance(right, (ast.Tuple, ast.List, ast.Set)):
                        letters = [e.value for e in right.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                        if len(letters) == len(right.elts) >= 2 and all(len(v) == 1 and v.isalpha() for v in letters) and isinstance(left, ast.Name):
                            # `w in ("A", "B", "C")` over the words of a text: only when the word comes from a split
                            if any(_call_name(c) in ("split", "findall") for c in ast.walk(scope) if isinstance(c, ast.Call)):
                                self.hint("single_letter_token", node, where)

            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and len(node.test.ops) == 1:
                test = node.test
                rejects = isinstance(test.ops[0], (ast.Gt, ast.GtE)) and any(
                    isinstance(c, ast.Call) and _call_name(c) == "abs" for c in ast.walk(test.left)
                )
                returns_nothing = any(
                    isinstance(r, ast.Return) and isinstance(r.value, ast.Constant) and not r.value.value
                    for b in node.body for r in ast.walk(b)
                )  # fmt: skip
                if rejects and returns_nothing:
                    self.hint("rejection_form_tolerance", node, where)
            if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
                sides = [node.left, node.comparators[0]]
                for side, other in (sides, sides[::-1]):
                    truncates = isinstance(side, ast.Call) and _call_name(side) == "int" and bool(side.args)
                    of_len = truncates and isinstance(side.args[0], ast.Call) and _call_name(side.args[0]) == "len"
                    against_key = any(WANT_WORD.search(n) for n in _names(other)) and not any(WANT_WORD.search(n) for n in _names(side))
                    if truncates and not of_len and against_key:
                        self.hint("truncated_count", node, where)
                        break
            if isinstance(node, ast.IfExp) and isinstance(node.test, ast.Name) and isinstance(node.orelse, ast.Name):
                if node.orelse.id != node.test.id and node.test.id in _names(node.body):
                    self.hint("whole_text_fallback", node, where)
            elif isinstance(node, ast.Call):
                name = _call_name(node)
                if name == "search" and node.args:
                    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
                    compiled = isinstance(receiver, ast.Name) and receiver.id in self.tag_patterns
                    literal = isinstance(receiver, ast.Name) and receiver.id == "re" and TAG_PATTERN.search(_literal_pattern(node.args[0]) or "")
                    if compiled or literal:
                        self.hint("first_tag_match", node, where)
                if name in ("search", "findall", "finditer") and node.args:
                    pattern = _literal_pattern(node.args[0])
                    # a "value pattern" says what an answer looks like (digits or a character class)
                    value_like = pattern is not None and bool(re.search(r"\\d|\[[^\]]+\]", pattern))
                    if value_like and not _has_context(pattern):
                        self.hint("unanchored_value_pattern", node, where)
                if name in PROCESS_CALLS:
                    for keyword in node.keywords:
                        if keyword.arg == "args" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                            if any(_names(e) & MODEL_TEXT_NAMES for e in keyword.value.elts):
                                self.hint("model_text_in_argv", node, where)
            elif isinstance(node, ast.ExceptHandler):
                broad = node.type is None or "Exception" in ast.unparse(node.type)
                for ret in (n for n in ast.walk(node) if isinstance(n, ast.Return)):
                    value = ret.value
                    if broad and isinstance(value, ast.Constant) and value.value not in (0, 0.0, False, None, ""):
                        if isinstance(value.value, (int, float, bool)):
                            self.hint("except_returns_reward", ret, where)


def _is_zero(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and not isinstance(node.value, bool) and node.value in (0, 0.0)


def _prompt_constants(path: Path, root: Path | None) -> list[Hint]:
    """Module-level `SYSTEM_PROMPT = "..."` style constants (a string of 80 characters or more)."""
    source = path.read_text(errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    shown = str(path.relative_to(root)) if root else str(path)
    found = []
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        value = getattr(node, "value", None)
        # one literal, pieces joined with `+` or `.join(...)`, an f-string: 80 characters of text in all
        text = sum(len(c.value) for c in ast.walk(value) if isinstance(c, ast.Constant) and isinstance(c.value, str)) if value else 0
        long_text = text >= 80 or isinstance(value, ast.JoinedStr)
        for target in targets:
            if long_text and isinstance(target, ast.Name) and target.id.isupper() and PROMPT_NAME.search(target.id):
                found.append(Hint("unreferenced_prompt_constant", shown, node.lineno, target.id, source.splitlines()[node.lineno - 1].strip()[:140]))
    return found


def _scan_prompt_constants(files: list[Path], root: Path | None) -> list[Hint]:
    """Prompt constants that no file of the package names anywhere else (code, `__all__`,
    a config file): the definition line itself does not count."""
    texts = {f: f.read_text(errors="replace") for f in files if f.stat().st_size < 2_000_000}
    hints = []
    for path in (f for f in files if f.suffix == ".py"):
        for hint in _prompt_constants(path, root):
            pattern = re.compile(rf"\b{re.escape(hint.where)}\b")
            if not any(pattern.search(line) for f, text in texts.items() for i, line in enumerate(text.splitlines(), 1)
                       if not (f == path and i == hint.line)):  # fmt: skip
                hints.append(hint)
    return hints


def _scan_dependencies(path: Path, source: str) -> list[Hint]:
    hints = []
    block = re.search(r"^# /// script\n(.*?)^# ///", source, re.S | re.M)
    if block:
        for match in re.finditer(r'"([A-Za-z0-9_.\-\[\]]+)"', block.group(1)):
            line = source[: block.start(1) + match.start()].count("\n") + 1
            hints.append(Hint("unpinned_grader_dependency", str(path), line, "inline script metadata", match.group(1)))
    return hints


FRAMEWORK_GRADERS = [  # framework grader helpers that read only part of the reply: (module, function names, rule)
    ("inspect_ai.scorer", {"includes"}, "inspect_includes_scorer"),
    ("inspect_ai.scorer", {"match"}, "inspect_match_end"),
    ("inspect_ai.scorer", {"choice"}, "inspect_choice_lenient"),
    ("hud.graders", {"contains", "contains_any", "contains_all"}, "hud_contains_grader"),
]


def _reads_whole_reply(call: ast.Call) -> bool:
    """`match(location="begin" | "exact")` compares the start or the whole output: not this hint."""
    where = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "location"), None)
    return isinstance(where, ast.Constant) and where.value in ("begin", "exact")


def _scan_math_verify(tree: ast.AST, shown: str, source: str) -> list[Hint]:
    """`verify(gold, parse(slot))` with math_verify: parse() reads a value out of anything in the slot."""
    names = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "math_verify"
             for a in n.names if a.name == "verify"}  # fmt: skip
    modules = {a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names if a.name == "math_verify"}
    hints = []
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        func = call.func
        direct = isinstance(func, ast.Name) and func.id in names
        qualified = isinstance(func, ast.Attribute) and func.attr == "verify" and isinstance(func.value, ast.Name) and func.value.id in modules
        if direct or qualified:
            hints.append(Hint("math_verify_whole_slot", shown, call.lineno, "grader", source.splitlines()[call.lineno - 1].strip()[:140]))
    return hints


def _scan_inspect(tree: ast.AST, shown: str, source: str) -> list[Hint]:
    """Tasks graded with a framework's built-in grader that reads only part of the reply
    (Inspect `includes()`, `match()`, `choice()`; HUD `contains()`)."""
    imported: dict[str, str] = {}  # local name -> rule
    for node in (n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)):
        for module, functions, rule in FRAMEWORK_GRADERS:
            if node.module == module:
                imported.update({a.asname or a.name: rule for a in node.names if a.name in functions})
    aliases = {a.asname: a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names if a.asname}
    hints = []
    # A HUD template receives the reply from a yield (`answer = yield prompt`): `x in answer` there is a substring test.
    for func in (n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)):
        replies = {t.id for n in ast.walk(func) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Yield)
                   for t in n.targets if isinstance(t, ast.Name)}  # fmt: skip
        for compare in (n for n in ast.walk(func) if isinstance(n, ast.Compare) and replies):
            if len(compare.ops) == 1 and isinstance(compare.ops[0], ast.In) and not isinstance(compare.left, ast.Constant):
                if _names(compare.comparators[0]) & replies:
                    hints.append(Hint("reference_substring_match", shown, compare.lineno, func.name,
                                      source.splitlines()[compare.lineno - 1].strip()[:140]))  # fmt: skip
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        func = call.func
        rule = imported.get(func.id) if isinstance(func, ast.Name) else None
        if rule is None and isinstance(func, ast.Attribute):
            owner = ast.unparse(func.value)
            owner = aliases.get(owner, owner)
            rule = next((r for module, functions, r in FRAMEWORK_GRADERS
                         if func.attr in functions and (owner == module or owner.endswith(module.split(".")[-1]))), None)  # fmt: skip
        if rule == "inspect_match_end" and _reads_whole_reply(call):
            rule = None
        if rule:
            hints.append(Hint(rule, shown, call.lineno, "grader", source.splitlines()[call.lineno - 1].strip()[:140]))
    return hints


def scan_file(path: Path, root: Path | None = None) -> list[Hint]:
    source = path.read_text(errors="replace")
    shown = str(path.relative_to(root)) if root else str(path)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    scanner = _Scanner(shown, source)
    scanner.collect_rubric(tree)
    scanner.collect_module_names(tree)
    scanner.visit(tree)
    if path.name.startswith("verify"):  # stand-alone grader script: module-level code is reward code
        module_level = ast.Module(body=[n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef))], type_ignores=[])
        scanner.scan_body(module_level, "<module>", set())
    return (scanner.hints + [Hint(h.rule, shown, h.line, h.where, h.evidence) for h in _scan_dependencies(path, source)]
            + _scan_inspect(tree, shown, source) + _scan_math_verify(tree, shown, source))


def scan(root: Path) -> list[Hint]:
    """Scan one file or every Python file under a directory (tests and hidden folders skipped)."""
    if root.is_file():
        return scan_file(root) + _scan_prompt_constants([root], None)
    hints: list[Hint] = []
    files = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        parts = path.relative_to(root).parts
        if any(p.startswith(".") or p in ("tests", "test", "outputs", "__pycache__") for p in parts):
            continue
        files.append(path)
        if path.suffix == ".py":
            hints.extend(scan_file(path, root))
    return hints + _scan_prompt_constants(files, root)


def cross_check(hints: list[dict[str, Any]], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach to each hint whether a probe that targets it failed in the dynamic run."""
    failed: dict[str, int] = {}
    ran: dict[str, int] = {}
    for r in results:
        ran[r["probe"]] = ran.get(r["probe"], 0) + 1
        if not r["passed"]:
            failed[r["probe"]] = failed.get(r["probe"], 0) + 1
    checked = []
    for hint in hints:
        probes = hint["confirm_with"]
        hits = {p: failed[p] for p in probes if failed.get(p)}
        if not probes:
            verdict = "not testable by probes"
        elif hits:
            verdict = "confirmed: " + ", ".join(f"{p} failed {n}/{ran[p]}" for p, n in hits.items())
        elif any(p in ran for p in probes):
            verdict = "not confirmed: " + ", ".join(p for p in probes if p in ran) + " passed"
        else:
            verdict = "not checked: " + ", ".join(probes) + " did not run"
        checked.append({**hint, "verdict": verdict})
    return checked

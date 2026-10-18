"""Plain reward functions (TRL's GRPOTrainer convention) plus a dataset.

Many graders are not packaged as an environment: a training script defines functions such
as `accuracy_reward(completions, solution, **kwargs)` and hands them to TRL's
`GRPOTrainer` with a dataset. falsepass calls them the way the trainer does, one completion
at a time:

    func(prompts=[prompt], completions=[completion], completion_ids=[ids],
         trainer_state=..., log_extra=..., log_metric=..., **{column: [value] for each other column})

`completion` is the reply as text, or `[{"role": "assistant", "content": text}]` when the
dataset's prompts are conversations (TRL's rule) or the functions read `completion[0]["content"]`.
Several functions are summed with their weights, a `None` counting as nothing (TRL's
`nansum`); when every function returns `None` the completion is unscorable.

The reference a probe writes into its completions comes from the dataset's answer column
(`answer`, `solution`, `ground_truth`, ... or the column a function names in its
signature). A worked solution is reduced to its final answer for the probes (the last
`\\boxed{}`, or what follows `####`); the reward functions always receive the row as it is.

Datasets: a JSONL / JSON / CSV / Parquet file, `hf:owner/name` (the `datasets` library,
streamed), or `call:function` (a function in the reward file that returns the rows, as
training scripts often have), optionally passed row by row through `map` (another function
in the file).
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import inspect
import json
import os
import re
import sys
import types
from pathlib import Path
from typing import Any

from falsepass.model import Completion, Example, Score
from falsepass.scorers import _guarded

ANSWER_COLUMNS = ("answer", "solution", "ground_truth", "gold", "target", "label", "reference", "expected", "final_answer",
                  "expected_answer", "gold_answer", "correct_answer", "output", "response")  # fmt: skip
PROMPT_COLUMNS = ("prompt", "question", "problem", "query", "instruction", "input", "text")
TRAINER_ARGS = {"prompts", "completions", "completion_ids", "trainer_state", "log_extra", "log_metric", "environments"}
SOURCE_VAR = "FALSEPASS_SOURCE"  # a patched copy of the reward file or its folder (verify-fix, --source)
STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
PIP_NAMES = {"sklearn": "scikit-learn", "yaml": "pyyaml", "PIL": "pillow", "cv2": "opencv-python", "bs4": "beautifulsoup4",
             "math_verify": "math-verify", "dateutil": "python-dateutil", "Levenshtein": "levenshtein",
             "rouge_score": "rouge-score", "sentence_transformers": "sentence-transformers"}  # fmt: skip
MAX_ROWS = 2000


def _reply(completion: Completion) -> str:
    if isinstance(completion, str):
        return completion
    turns = [str(m.get("content") or "") for m in completion if m.get("role") == "assistant" and not m.get("tool_calls")]
    return turns[-1] if turns else ""


def is_conversation(value: Any) -> bool:
    """TRL's `is_conversational`: a list of messages with a role."""
    return isinstance(value, list) and bool(value) and isinstance(value[0], dict) and "role" in value[0]


def prompt_text(value: Any) -> str:
    """What the model reads: the system and user turns of a conversation, or the text."""
    if is_conversation(value):
        return "\n".join(str(m.get("content") or "") for m in value if m.get("role") in ("system", "user"))
    return "" if value is None else str(value)


def final_answer(value: Any) -> tuple[str, str]:
    """The answer a probe writes, from a reference column value, and how it was found."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) if value is not None else ""
    boxed = _last_boxed(text)
    if boxed is not None and (len(text) > 60 or "\n" in text):
        return boxed, "last \\boxed{} of the worked solution"
    if "####" in text and (len(text) > 60 or "\n" in text):
        return text.rsplit("####", 1)[1].strip(), "what follows #### in the worked solution"
    return text.strip(), ""


def _last_boxed(text: str) -> str | None:
    start = text.rfind("\\boxed{")
    if start < 0:
        return None
    depth, i = 0, start + len("\\boxed")
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j].strip()
    return None


# --- loading the functions -------------------------------------------------------------------


def resolve_target(spec: str) -> Path | None:
    """The reward file to import: `$FALSEPASS_SOURCE` (a patched file, or a folder holding a
    file of the same name) in place of `spec`; None when `spec` is a module name."""
    patched = os.environ.get(SOURCE_VAR)
    original = Path(spec)
    if not original.suffix == ".py":
        return None
    if patched:
        patched_path = Path(patched)
        return (patched_path / original.name if patched_path.is_dir() else patched_path).resolve()
    return original.resolve()


def import_target(spec: str, pythonpath: list[str] | None = None) -> types.ModuleType:
    """The module holding the reward functions: a file (its folder goes on sys.path), or an
    importable module name (`trl.rewards`, `open_r1.rewards`), found through `pythonpath`
    (folders put on sys.path, for a package that is not installed) when given."""
    for folder in reversed(pythonpath or []):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    path = resolve_target(spec)
    if path is None:
        try:
            return importlib.import_module(spec)
        except ImportError as missing:
            raise RuntimeError(import_advice(spec, missing, pythonpath or [])) from missing
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    name = "falsepass_rewards_" + re.sub(r"\W", "_", path.stem)
    loaded = importlib.util.spec_from_file_location(name, path)
    if loaded is None or loaded.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(loaded)
    sys.modules[name] = module
    try:
        loaded.loader.exec_module(module)
    except ImportError as missing:
        relative = "attempted relative import" in str(missing)
        raise RuntimeError(
            f"importing {path.name} failed: {missing}. "
            + ("It is part of a package: give the module name and the folder that holds the package, e.g. `falsepass init "
               "my_pkg.rewards --framework reward --path ./src --dataset ...` (the package is imported from there, its "
               "dependencies come from --with)." if relative else
               "Add the missing package with `falsepass init ... --with <package> --force`.")
        ) from missing
    return module


def import_advice(spec: str, missing: ImportError, pythonpath: list[str]) -> str:
    """One message for a failed import: the package to add, or the version the package's own
    setup.py / pyproject.toml pins when a name could not be imported from an installed one."""
    package = getattr(missing, "name", None) or ""
    if not package:  # "cannot import name 'x' from 'trl' (...)": the installed version differs
        found = re.search(r"from '([\w.]+)'", str(missing))
        package = found.group(1) if found else ""
    top = package.split(".")[0]
    pip = PIP_NAMES.get(top, top.replace("_", "-"))
    pinned = ""
    for folder in pythonpath:
        for manifest in (Path(folder).parent / "setup.py", Path(folder).parent / "pyproject.toml", Path(folder) / "pyproject.toml"):
            if manifest.is_file():
                pin = re.search(rf"[\"']({re.escape(pip)}\s*(?:\[[^\]]*\])?\s*[=<>~!][^\"']*)[\"']", manifest.read_text(errors="replace"), re.I)
                if pin:  # extras dropped: `trl[vllm]` pulls GPU-only wheels a grader does not need
                    pinned = re.sub(r"\[[^\]]*\]", "", pin.group(1).replace(" ", ""))
                    break
        if pinned:
            break
    wanted = pinned or pip
    also = [m for m in _missing_imports(pythonpath, spec.split(".")[0]) if m not in (pip, wanted.split("=")[0].split(">")[0].split("<")[0])]
    if also:  # every import of the package that would fail next, so one more init fixes them all
        extra = [f'--with "{_pinned(m, pythonpath) or m}"' for m in also[:12]]
        return (f"importing {spec} failed: {missing}. The package imports modules that are not installed here: add "
                f'`--with "{wanted}" {" ".join(extra)}` to `falsepass init ... --force` (versions from the package\'s own '
                "manifest where it pins one), or --path for a folder that holds them")
    why = "the installed version is not the one the package was written for" if isinstance(missing, ImportError) and not getattr(missing, "name", None) else "it is not installed"
    return (f"importing {spec} failed: {missing}. {why[0].upper() + why[1:]}: add `--with \"{wanted}\"` to `falsepass init ... --force`"
            + (f" (the package's own manifest pins {pinned})" if pinned else "") + ", or --path for a folder that holds it")


def _pinned(pip: str, pythonpath: list[str]) -> str:
    for folder in pythonpath:
        for manifest in (Path(folder).parent / "setup.py", Path(folder).parent / "pyproject.toml", Path(folder) / "pyproject.toml"):
            if manifest.is_file():
                pin = re.search(rf"[\"']({re.escape(pip)}\s*(?:\[[^\]]*\])?\s*[=<>~!][^\"']*)[\"']", manifest.read_text(errors="replace"), re.I)
                if pin:
                    return re.sub(r"\[[^\]]*\]", "", pin.group(1).replace(" ", ""))
    return ""


def _missing_imports(pythonpath: list[str], package: str) -> list[str]:
    """pip names of the top-level imports anywhere in `package` (under the pythonpath folders)
    that cannot be found in this environment."""
    import ast

    names: list[str] = []
    for folder in pythonpath:
        for file in sorted((Path(folder) / package).rglob("*.py")):
            try:
                tree = ast.parse(file.read_text(errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names += [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names.append(node.module.split(".")[0])
    missing = []
    for name in dict.fromkeys(names):
        if name in STDLIB or name == package:
            continue
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(PIP_NAMES.get(name, name.replace("_", "-")))
    return missing


def reward_functions(module: types.ModuleType) -> list[str]:
    """Functions of the module written for GRPOTrainer: they take `completions`."""
    found = []
    for name, obj in vars(module).items():
        if name.startswith("_") or not callable(obj) or inspect.isclass(obj):
            continue
        owner = getattr(obj, "__module__", None) or ""
        own = owner == module.__name__ or owner.startswith(module.__name__ + ".")  # a package re-exports its submodules' functions
        if not own and not isinstance(obj, functools.partial):
            continue
        try:
            parameters = inspect.signature(obj).parameters
        except (TypeError, ValueError):
            continue
        if "completions" in parameters:
            found.append(name)
    return found


def _accepts(func: Any, kwargs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The keyword arguments `func` takes, and the ones it would refuse (no `**kwargs`)."""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return kwargs, []
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return kwargs, []
    return {k: v for k, v in kwargs.items() if k in parameters}, sorted(k for k in kwargs if k not in parameters)


def reads_message_content(func: Any) -> bool:
    """The function indexes a completion as a message list (`completion[0]["content"]`)."""
    try:
        source = inspect.getsource(func)
    except (OSError, TypeError):
        return False
    return bool(re.search(r"\[0\]\s*\[\s*[\"']content[\"']\s*\]", source))


def format_hints(funcs: dict[str, Any]) -> str:
    """The answer markup the functions look for, from their string constants and the module
    patterns they use (`<think>`, `<reasoning>`, `<answer>`, `\\boxed`, `####`), written as a
    short text the format inference reads next to the prompt: a format reward that wants tags
    the prompt never names still shapes the best-paid reference format."""
    texts: list[str] = []
    for func in funcs.values():
        target = func.func if isinstance(func, functools.partial) else func
        stack = [getattr(target, "__code__", None)]
        while stack:  # the function and the code nested in it (comprehensions, helpers defined inside)
            code = stack.pop()
            if code is None:
                continue
            texts += [c for c in code.co_consts if isinstance(c, str)]
            texts += [t for c in code.co_consts if isinstance(c, tuple) for t in c if isinstance(t, str)]  # keyword names
            stack += [c for c in code.co_consts if hasattr(c, "co_consts")]
            for name in code.co_names:
                value = getattr(target, "__globals__", {}).get(name)
                if isinstance(value, str):
                    texts.append(value)
                elif isinstance(value, re.Pattern):
                    texts.append(value.pattern)
    joined = "\n".join(texts)
    tags = list(dict.fromkeys(re.findall(r"<([A-Za-z_][\w-]*)>", joined)))
    parts = [f"<{t}></{t}>" for t in tags[:6]]
    if "boxed" in joined:
        parts.append("\\boxed{}")
    if "####" in joined:
        parts.append("####")
    return " ".join(parts)


class _TrainerState(types.SimpleNamespace):
    """Stands in for transformers' TrainerState: the start of training."""


class RewardFunctionScorer:
    """Score a reply with the reward functions, called as GRPOTrainer calls them."""

    concurrent_safe = False

    def __init__(self, funcs: dict[str, Any], weights: dict[str, float], prompt_column: str, conversational: bool) -> None:
        self.funcs, self.func_weights = funcs, weights
        self.prompt_column, self.conversational = prompt_column, conversational
        self.refused: dict[str, list[str]] = {}  # function -> trainer keywords it has no parameter for

    @property
    def weights(self) -> dict[str, float]:
        return dict(self.func_weights)

    def arguments(self, example: Example, text: str) -> dict[str, Any]:
        row = example.raw["row"]
        prompt = row.get(self.prompt_column)
        if self.conversational and not is_conversation(prompt):
            prompt = [{"role": "user", "content": prompt_text(prompt)}]
        completion = [{"role": "assistant", "content": text}] if self.conversational else text
        state = _TrainerState(global_step=0, epoch=0.0, max_steps=0, num_train_epochs=0, log_history=[])
        kwargs: dict[str, Any] = {
            "prompts": [prompt], "completions": [completion],
            "completion_ids": [[0] * max(1, round(len(text) / 4))],  # about one token per four characters
            "trainer_state": state, "log_extra": lambda *a, **k: None, "log_metric": lambda *a, **k: None,
        }  # fmt: skip
        for column, value in row.items():
            if column not in ("prompt", "completion", "completion_ids"):
                kwargs[column] = [value]
        return kwargs

    async def score(self, example: Example, completion: Completion) -> Score:
        text = _reply(completion)
        kwargs = self.arguments(example, text)
        errors: list[str] = []
        values: dict[str, float | None] = {}

        async def run() -> None:
            for name, func in self.funcs.items():
                taken, refused = _accepts(func, kwargs)
                if refused:
                    self.refused[name] = refused
                result = func(**taken)
                if inspect.isawaitable(result):
                    result = await result
                result = list(result)
                if len(result) != 1:
                    raise RuntimeError(f"{name} returned {len(result)} rewards for one completion")
                value = result[0]
                values[name] = None if value is None else float(value)

        if not await _guarded(run(), errors):
            return Score(None, {k: v for k, v in values.items() if v is not None}, errors)
        given = {k: v for k, v in values.items() if v is not None and v == v}  # None and NaN count as nothing, as nansum does
        if not given:
            return Score(None, {}, ["every reward function returned None for this completion: the trainer cannot score it"])
        reward = sum(self.func_weights.get(k, 1.0) * v for k, v in given.items())
        return Score(reward, given, [])

    async def close(self) -> None:
        return None


def source_files(funcs: dict[str, Any]) -> dict[str, Path]:
    """The files that define the reward functions (for the static scan when the target is an
    installed module), by a path relative to their top-level package."""
    found: dict[str, Path] = {}
    for func in funcs.values():
        target = func.func if isinstance(func, functools.partial) else func
        try:
            file = Path(inspect.getsourcefile(target) or "")
        except TypeError:
            continue
        module = getattr(target, "__module__", "") or file.stem
        if file.is_file():
            found[str(Path(*module.split(".")[:-1], file.name))] = file
    return found


# --- the dataset -------------------------------------------------------------------------------


def read_rows(spec: str, module: types.ModuleType | None, split: str = "", config: str = "", limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    """Rows of the dataset `spec`: a local file, `hf:owner/name`, or `call:function` in the reward module."""
    if spec.startswith("call:"):
        if module is None:
            raise RuntimeError("`call:` datasets need the reward file")
        function = getattr(module, spec[5:], None)
        if function is None:
            raise RuntimeError(f"no function {spec[5:]!r} in the reward file for the dataset")
        parameters = inspect.signature(function).parameters
        data = function(**({"split": split} if split and "split" in parameters else {}))
        return _take(data, limit)
    if spec.startswith("hf:") or (not Path(spec).exists() and re.fullmatch(r"[\w.-]+/[\w.-]+", spec)):
        try:
            import datasets
        except ImportError as missing:
            raise RuntimeError("a Hugging Face dataset needs the `datasets` package: falsepass init ... --with datasets --force") from missing
        name = spec.removeprefix("hf:")
        try:
            data = datasets.load_dataset(name, config or None, split=split or "train", streaming=True)
        except ValueError as unknown:  # a wrong split names the ones that exist
            raise RuntimeError(f"loading {name}: {unknown}") from unknown
        return _take(data, limit)
    path = Path(spec)
    if not path.is_file():
        raise RuntimeError(f"dataset {spec!r} is neither a file, `hf:owner/name`, nor `call:function`")
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    elif path.suffix == ".json":
        data = json.loads(path.read_text())
        rows = data if isinstance(data, list) else data.get("data") or data.get("rows") or next(
            (v for v in data.values() if isinstance(v, list)), [])  # fmt: skip
    elif path.suffix == ".csv":
        import csv

        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
    elif path.suffix == ".parquet":
        try:
            import pandas
        except ImportError as missing:
            raise RuntimeError("a Parquet dataset needs pandas and pyarrow: falsepass init ... --with pandas --with pyarrow --force") from missing
        rows = pandas.read_parquet(path).to_dict("records")
    else:
        raise RuntimeError(f"unknown dataset format {path.suffix!r}: use .jsonl, .json, .csv or .parquet")
    return _take(rows, limit)


def _take(data: Any, limit: int) -> list[dict[str, Any]]:
    rows = []
    for row in data:
        rows.append(dict(row))
        if len(rows) >= limit:
            break
    return rows


def answer_column(rows: list[dict[str, Any]], funcs: dict[str, Any], given: str = "") -> str:
    """The reference column: the one given, else a column a function names in its signature,
    else the usual names."""
    columns = set(rows[0]) if rows else set()
    if given:
        if given not in columns:
            raise RuntimeError(f"no column {given!r} in the dataset; columns: {sorted(columns)}")
        return given
    for func in funcs.values():
        try:
            named = [p for p in inspect.signature(func).parameters if p not in TRAINER_ARGS]
        except (TypeError, ValueError):
            continue
        for name in named:
            if name in columns and name not in PROMPT_COLUMNS[:1]:
                return name
    return next((c for c in ANSWER_COLUMNS if c in columns), "")


def load(spec: str, max_rows: int, dataset: str, functions: list[str] | None = None, weights: list[float] | None = None,
         split: str = "", config: str = "", answer: str = "", prompt: str = "", mapper: str = "",
         conversational: bool | None = None, pythonpath: list[str] | None = None,
         ) -> tuple[RewardFunctionScorer, list[Example], dict[str, Any]]:  # fmt: skip
    """The scorer and the rows for the reward functions in `spec` (a file or a module name)."""
    module = import_target(spec, pythonpath)
    path = resolve_target(spec)
    if path is not None:
        os.chdir(path.parent)  # relative data paths in the file
    available = reward_functions(module)
    chosen = functions or available
    if not chosen:
        raise RuntimeError(f"no function taking `completions` in {spec}: name the reward functions with --func NAME")
    missing = [f for f in chosen if not callable(getattr(module, f, None))]
    if missing:
        raise RuntimeError(f"no function {', '.join(missing)} in {spec}; reward functions found: {available}")
    funcs = {name: getattr(module, name) for name in chosen}
    weight_list = list(weights or [])
    if weight_list and len(weight_list) != len(chosen):
        raise RuntimeError(f"{len(weight_list)} weights for {len(chosen)} functions ({chosen}): give one weight per function, in order")
    func_weights = {name: float(weight_list[i]) if weight_list else 1.0 for i, name in enumerate(chosen)}
    if not dataset:
        raise RuntimeError("no dataset: give one with --dataset (a .jsonl/.json/.csv/.parquet file, hf:owner/name, or call:function "
                           "for a function in the reward file that returns the rows)")  # fmt: skip
    rows = read_rows(dataset, module, split, config, max_rows)
    if mapper:
        transform = getattr(module, mapper, None)
        if transform is None:
            raise RuntimeError(f"no function {mapper!r} in {spec} to map the rows with")
        rows = [dict(transform(row)) for row in rows]
    if not rows:
        raise RuntimeError(f"the dataset {dataset} has no rows")
    columns = sorted(rows[0])
    prompt_column = prompt or next((c for c in PROMPT_COLUMNS if c in rows[0]), "")
    if not prompt_column:
        raise RuntimeError(f"no prompt column among {columns}: name it with --prompt-column")
    reference = answer_column(rows, funcs, answer)
    conversation_prompts = is_conversation(rows[0].get(prompt_column))
    message_readers = [name for name, func in funcs.items() if reads_message_content(func)]
    if conversational is None:
        conversational = conversation_prompts or bool(message_readers)
    notes = [f"reward functions: {', '.join(f'{n} (weight {w:g})' for n, w in func_weights.items())}"
             + (f"; also in the file, not used: {', '.join(f for f in available if f not in chosen)}" if set(available) - set(chosen) else ""),
             f"dataset columns: {', '.join(columns)}; prompt column `{prompt_column}`"
             + (f", reference column `{reference}`" if reference else ", no reference column found (answer-free probes only; --answer-column NAME)")]  # fmt: skip
    if conversational and not conversation_prompts:
        notes.append(f"completions are passed as message lists because {', '.join(message_readers) or 'the settings'} read "
                     "`completion[0][\"content\"]`; the dataset's prompts are text, so GRPOTrainer would pass text and "
                     "these functions would fail there: wrap the prompts as conversations in training")  # fmt: skip
    probe_column = reference
    if reference and not answer:
        # A column of worked solutions without a marked final answer cannot give the probes a value;
        # a short answer column next to it can (OpenR1-Math: `solution` and `answer`).
        sample = [final_answer(r.get(reference)) for r in rows[:200]]
        unmarked = sum(1 for value, how in sample if not how and (len(value) > 60 or "\n" in value))
        alternative = next((c for c in ANSWER_COLUMNS if c in rows[0] and c != reference), "")
        if unmarked > len(sample) / 2 and alternative:
            probe_column = alternative
            notes.append(f"`{reference}` holds worked solutions without a marked final answer in {unmarked} of {len(sample)} rows, "
                         f"so the probes take their reference from `{alternative}`; the functions still receive `{reference}`. "
                         f"If the functions read a value out of `{reference}` that differs from `{alternative}`, P1 shows it")  # fmt: skip
        elif unmarked > len(sample) / 2:
            notes.append(f"`{reference}` holds worked solutions without a marked final answer in {unmarked} of {len(sample)} rows "
                         "and no short answer column was found: the probes' reference is the whole solution, so expect the "
                         "control to fail; name a column of final answers with --answer-column")  # fmt: skip
    pool, how = [], ""
    for i, row in enumerate(rows):
        value, method = final_answer(row.get(probe_column)) if probe_column else ("", "")
        how = how or method
        pool.append(Example(f"row-{i}", prompt_text(row.get(prompt_column)), value, {"row": row}))
    if how:
        notes.append(f"the probes' reference is the {how} in `{probe_column}`; the functions receive the column as it is")
    scorer = RewardFunctionScorer(funcs, func_weights, prompt_column, conversational)
    found = {"notes": notes, "functions": chosen, "weights": [func_weights[n] for n in chosen], "conversational": conversational,
             "prompt_column": prompt_column, "answer_column": reference if probe_column == reference else "",
             "available": available, "format_hints": format_hints(funcs)}  # fmt: skip
    return scorer, pool, found


# --- `falsepass init` (runs outside the audit virtualenv: nothing is imported) ------------------


def uses_reward_functions(path: Path) -> bool:
    """A Python file that defines a function taking `completions` (GRPOTrainer's convention)."""
    if not (path.is_file() and path.suffix == ".py"):
        return False
    return bool(re.search(r"^\s*(async\s+)?def\s+\w+\s*\(\s*(prompts\s*[,:][^)]*?)?completions\b", path.read_text(errors="replace"), re.M))


def declared_functions(path: Path) -> list[str]:
    import ast

    tree = ast.parse(path.read_text(errors="replace"))
    return [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_")
            and "completions" in {a.arg for a in [*n.args.posonlyargs, *n.args.args, *n.args.kwonlyargs]}]  # fmt: skip


def third_party_imports(path: Path) -> list[str]:
    """pip names for the top-level imports of the file that are neither stdlib nor files next to it."""
    import ast

    tree = ast.parse(path.read_text(errors="replace"))
    local = {p.stem for p in path.parent.glob("*.py")} | {p.name for p in path.parent.iterdir() if p.is_dir()}
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module.split(".")[0])
    wanted = [n for n in dict.fromkeys(names) if n not in STDLIB and n not in local and n != "falsepass"]
    return [PIP_NAMES.get(n, n.replace("_", "-")) for n in wanted]


def describe(target: str, dataset: str, functions: list[str] | None = None, weights: list[float] | None = None,
             split: str = "", config: str = "", answer: str = "", prompt: str = "", mapper: str = "",
             pythonpath: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    """What `falsepass init` writes for reward functions: the file (or module), the dataset
    and what the audit virtualenv needs (the file's own imports; `datasets` for a Hub dataset)."""
    path = Path(target)
    is_file = path.is_file()
    extra: list[str] = []
    others: list[str] = []
    if is_file:
        path = path.resolve()
        if re.search(r"^\s*from\s+\.", path.read_text(errors="replace"), re.M):
            package = path.parent
            while (package.parent / "__init__.py").is_file():
                package = package.parent
            module = ".".join([*path.relative_to(package.parent).with_suffix("").parts])
            raise RuntimeError(f"{path.name} uses relative imports (`from .x import y`): it is part of a package and cannot be "
                               f"imported on its own. Give the module name and the folder that holds the package: "
                               f"`falsepass init {module} --framework reward --path {package.parent} --dataset ...`, plus "
                               "--with for the package's own third-party imports")  # fmt: skip
        found = declared_functions(path)
        if functions:
            unknown = [f for f in functions if f not in found and not re.search(rf"^{re.escape(f)}\s*=", path.read_text(), re.M)]
            if unknown:
                raise RuntimeError(f"no function {', '.join(unknown)} in {path.name}; functions taking `completions`: {found}")
        others = [f for f in found if functions and f not in functions]
        folder = path.parent
        requirements = folder / "requirements.txt"
        if requirements.is_file():
            extra = [line.split("#")[0].strip() for line in requirements.read_text().splitlines()
                     if line.split("#")[0].strip() and not line.startswith("-")]  # fmt: skip
        else:
            extra = third_party_imports(path)
    if (dataset.startswith("hf:") or (dataset and not Path(dataset).exists() and re.fullmatch(r"[\w.-]+/[\w.-]+", dataset))) and "datasets" not in extra:
        extra.append("datasets")
    if dataset and Path(dataset).suffix == ".parquet":
        extra += [p for p in ("pandas", "pyarrow") if p not in extra]
    if not is_file:  # a module name: install its distribution when PyPI has one under the top-level name
        from falsepass.hub import pypi_version

        top = target.split(".")[0]
        name = PIP_NAMES.get(top, top.replace("_", "-"))
        try:
            pypi_version(name)
            extra.insert(0, name)
        except Exception:  # noqa: BLE001 - not on PyPI (a local package: add it with --with ./path), or offline
            pass
    stem = path.stem if is_file else target.replace(".", "-")  # `trl.rewards` and `open_r1.rewards` stay apart
    info: dict[str, Any] = {
        "id": f"local/{stem.replace('_', '-').lower()}", "framework": "reward", "task": str(path) if is_file else target,
        "package": "", "version": "", "declared": "", "extra": extra, "others": others,
        "dataset": str(Path(dataset).resolve()) if dataset and Path(dataset).exists() else dataset,
    }  # fmt: skip
    if pythonpath:
        pythonpath = [str(Path(p).resolve()) for p in pythonpath]
    for key, value in (("functions", functions), ("weights", weights), ("split", split), ("dataset_config", config),
                       ("answer_column", answer), ("prompt_column", prompt), ("map", mapper), ("pythonpath", pythonpath)):  # fmt: skip
        if value:
            info[key] = value
    return info

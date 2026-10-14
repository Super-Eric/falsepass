"""A HUD environment with one planted grading defect, for trying falsepass.

The grader passes when the right count appears anywhere in the answer (a substring test,
as in `hud.graders.contains`), so a reply that lists many numbers passes.
"""

from hud import Environment

env = Environment(name="hud-toy")


@env.template(id="count_letter")
async def count_letter(word: str, letter: str):
    answer = yield f"How many times does the letter '{letter}' appear in '{word}'? Reply with the number only."
    yield 1.0 if str(word.count(letter)) in (answer or "") else 0.0  # planted defect: should compare the number

---
title: Case study: a correct “I can't tell” was paid less than “I can't tell, but here's a guess”
date: 2026-11-02
description: An environment rewards a model for saying when a question can't be answered from the facts given. Its grader paid the correct abstention less than the same abstention with a guess beside it. How it showed up, the fix, what the maintainer confirmed before merging it, and what the fix leaves open.
---

One of the environments I audited is a benefits-eligibility task with a twist. Each task is a
household's case file, and the model has to say what the household gets from each of three
benefit programs, and for the first of them whether it is eligible at all, as one JSON object.
The twist is the other half of the task: when a fact the answer depends on is missing from the
case file, the model is supposed to say so, by listing the program in an abstention field with
the name of the missing fact, "instead of guessing". Of the 1,200 packaged tasks, 276 are
built so that abstaining is the right answer.

The reward is a weighted sum of four terms: abstention (0.35), amounts (0.325), eligibility
(0.215) and periods (0.11). Answer keys were computed when the tasks were generated and ship
with the package, so scoring needs no model. That made it a good fit for the way I test
graders: construct a reply whose correct verdict I already know, score it with the
environment's own code, and see whether the two agree. As in the earlier write-up, I'm not
naming the environment.

## How it showed up

The first thing [falsepass](https://github.com/Super-Eric/falsepass) does on any environment
is score the reference answer. If the reference doesn't earn the maximum, something is off
before any probe runs. Here the shipped key holds the hidden values and no abstention, so for
the abstention tasks the reference is built from the key and the task's withheld fact, in the
shape the prompt asks for: the deciding program or programs listed in the abstention field
with the withheld fact, and their values left `null`. On a sample of 25 tasks, 5 references
failed, scoring 0.785 or 0.46 instead of 1.0. All five were abstention tasks.

The schema allows a null exactly there, and the maintainer's own list of known limits says
why: an earlier version had rejected such replies outright, so nulls were made legal for an
abstained program, and the scorers were changed to leave it out of the amount and eligibility
comparison instead of counting it wrong.

## The defect

I then scored a few reply shapes through the environment's own scoring function on all 276
abstention tasks:

| Reply on an abstention task | Mean reward | At 1.0 |
|---|---:|---:|
| abstain on the deciding programs, name the withheld fact, values `null` | 0.717 | 13/276 |
| the same abstention, with the key's own values kept beside it | 1.000 | 276/276 |
| the same abstention, with a fixed guess beside it (eligible, $0) | 0.822 | 2/276 |
| no abstention at all, the key's values | 0.650 | 0/276 |

The correct abstention earned 1.0 on 13 tasks: the ones where the first of the three programs
was not the deciding program. On the other 263 it lost the whole eligibility term. On the 66
tasks where all three programs depended on the missing fact it lost the amounts term too, and
came out at 0.460, below a reply that didn't abstain at all (0.650).

The cause was two lines in the scoring module. The amount and eligibility scorers do leave a
null program out of the comparison, as the limits document says. Then, when nothing is left
to compare, they return 0.0. For amounts, that zero is a documented guard against blanket
abstention. On a task where the abstention is exactly right, it is the second penalty the
earlier change was written to remove. The eligibility term only ever looks at the first
program, so every correct abstention on that program scored 0 on it.

None of the author's tests or scripted agents could show this. The one test that fed real null
abstentions to these two scorers checked only that they didn't raise. The environment's
ceiling agent keeps the key's values next to its abstentions, which is the second row of the
table, and that shape scores 1.0. The schema example in the system prompt has the same shape.
The reply that hit the defect was the one the prompt's wording asks for.

What that reward would teach: on an abstention task, writing a value beside the abstention is
worth up to +0.215, and on the three-program tasks up to +0.54. The first program is eligible
in 247 of the 276 keys, so a fixed guess of "eligible" is usually paid. The reward points a
model toward abstaining and guessing in the same breath.

## The fix

The change is small and stays out of the scoring module: the task's scoring step gives an
emptied term 1.0 when the abstention term rates the abstention fully correct, and 0.0
otherwise. So abstaining everywhere, abstaining when nothing is missing, and naming the wrong
fact all score exactly as before. Two tests pin it, one of which fails on the old code.

Before sending it I rescored all 1,200 tasks with and without the change. The correct null
abstention went from 0.717 to a mean of 1.000; so did its variants (entries in another order,
an extra entry for an unscored program, a coupled fact named instead of the withheld one, the
object inside a fence between two lines of prose). None of the 924 tasks where answering is
correct moved. The abstain-and-guess reply stayed at 0.822, and not abstaining stayed at
0.650.

I sent it as a pull request. The maintainer reran the measurements before replying: the
failing test on the old code, the rescoring of all 1,200 tasks (the numbers matched mine),
the baselines, two consistency diagnostics, the ceiling agent, and the three published model
runs re-scored from their cached replies. The replies behind those runs had hit the defect
too: between 64 and 125 tasks per run changed. None of the headline metrics changed on any
task; the composite score, which isn't a headline metric, rose by 0.018 to 0.038 per run. The fix was
merged unchanged and recorded in the environment's own list of known limits, with a
before-and-after table.

## What the fix leaves open

A value written beside a correct abstention is still read and still paid where it matches
the key, so the abstain-and-guess shape keeps its 0.822. It no longer beats the form the prompt
asks for, which was the defect, but it isn't rejected either. Whether it should be is a policy
question for the maintainer, and I didn't propose a change there: the prompt says "instead of
guessing", but the prompt's own example and the ceiling agent both write a value beside the
abstention, so a stricter scorer would have to change the example too.

Two other things I found on the same audit went in as separate follow-ups at the maintainer's
request: on the published install path the tasks carry no system prompt, so a model is never
shown the answer format or the names of the facts (open as a pull request), and the parser
reads one JSON object and nothing around it (a fenced one first, otherwise the last), so a
denied object scores like the bare object (recorded as by design; the issue is open). The published package still
predates the merge; the fix is on the repository's main branch.

## What to take from it

- **Score your reference answers, in the shape your prompt asks for.** The environment had a
  ceiling agent. It used the shape the prompt's example shows, not the one its wording asks
  for, and so it couldn't see the cap on the latter.
- **Test the terms together.** Each scorer was right on its own. The 0.0 that guards one term
  against blanket abstention became a penalty once another term had already judged the
  abstention correct.
- **Write the negative case next to the positive one.** The two tests that went in with the
  fix are the ones the earlier change should have had: a correct null abstention earns 1.0 on
  every term, and a wrong-fact abstention still earns nothing on the emptied terms.

The repro script scores every reply through the environment's own code and ran twice from
clean virtualenvs with identical output. The tool is Apache-2.0:
https://github.com/Super-Eric/falsepass.

# Probe summary: local/falsepass-toy-env

2026-10-14 · falsepass 0.2.0 · verifiers 0.3.1, rewardlint 0.1.0, falsepass-toy-env 0.1.0 · Python 3.12.15 · 10 examples, seed 0

- **Adapter:** inferred. Answer format `reasoning+<answer>` chosen because the reference scored best in it (5 examples; reasoning+<answer> 1, <answer> 1, bare 0, reasoning+boxed 0, boxed 0, reasoning+Answer: 0, Answer: 0). Answers: open. Single-turn.
- **False-pass ceiling:** 1 of 1 (highest reward given to a known-bad completion; via P5 shotgun/all_in_one_slot)
- **Lazy-policy reward:** 0 (best average reward of one fixed strategy that never uses the reference answer; via P3 empty/empty)
- **False-fail rate:** 0.108 (93 known-good completions)
- **Completions on which reward code raised or timed out:** 0

| Probe | Variant | Expect | Runs | Failed | Score min / mean / max | First failure |
|---|---|---|---|---|---|---|
| P1 ground_truth | reference | full | 10 | 0 | 1 / 1 / 1 |  |
| P2 equivalent_forms | surrounding_whitespace | full | 10 | 0 | 1 / 1 / 1 |  |
| P2 equivalent_forms | sentence_after | full | 10 | 0 | 1 / 1 / 1 |  |
| P2 equivalent_forms | crlf_line_endings | full | 10 | 0 | 1 / 1 / 1 |  |
| P2 equivalent_forms | padding_inside_slot | full | 10 | 0 | 1 / 1 / 1 |  |
| P2 equivalent_forms | trailing_period_inside_slot | full | 10 | 0 | 1 / 1 / 1 |  |
| P3 empty | empty | zero | 10 | 0 | 0 / 0 / 0 |  |
| P3 empty | whitespace | zero | 10 | 0 | 0 / 0 / 0 |  |
| P4 echo_prompt | prompt | zero | 10 | 0 | 0 / 0 / 0 |  |
| P5 shotgun | list_bare | zero | 10 | 0 | 0 / 0 / 0 |  |
| P5 shotgun | each_rendered | zero | 10 | 0 | 0 / 0 / 0 |  |
| P5 shotgun | all_in_one_slot | zero | 10 | 10 | 1 / 1 / 1 | expected zero, got 1.0 |
| P6 format_only | wrong_in_format | zero | 10 | 0 | 0 / 0 / 0 |  |
| P7 wrong_confident | plain | zero | 10 | 0 | 0 / 0 / 0 |  |
| P9 oversized | correct_then_retracted | zero | 10 | 10 | 1 / 1 / 1 | expected zero, got 1.0 |
| P9 oversized | answer_mentioned_and_denied | zero | 10 | 0 | 0 / 0 / 0 |  |
| P13 special_numbers | nan | zero | 10 | 0 | 0 / 0 / 0 |  |
| P13 special_numbers | infinity | zero | 10 | 0 | 0 / 0 / 0 |  |
| P13 special_numbers | integer_plus_0.9 | zero | 10 | 10 | 1 / 1 / 1 | expected zero, got 1.0 |
| P14 format_mentions | tag_named_in_prose_before | full | 10 | 0 | 1 / 1 / 1 |  |
| P14 format_mentions | empty_tag_pair_named_before | full | 10 | 10 | 0 / 0 / 0 | expected full, got 0.0 |
| P14 format_mentions | empty_tag_pair_named_after | full | 10 | 0 | 1 / 1 / 1 |  |
| P15 hedged_in_slot | false_equation | zero | 10 | 10 | 1 / 1 / 1 | expected zero, got 1.0 |
| P15 hedged_in_slot | denied | zero | 10 | 10 | 1 / 1 / 1 | expected zero, got 1.0 |
| P15 hedged_in_slot | denied_latex | zero | 10 | 10 | 1 / 1 / 1 | expected zero, got 1.0 |
| P15 hedged_in_slot | not_answer | zero | 10 | 10 | 1 / 1 / 1 | expected zero, got 1.0 |
| P15 hedged_in_slot | either_or | zero | 10 | 10 | 1 / 1 / 1 | expected zero, got 1.0 |
| P15 hedged_in_slot | either_or_latex | zero | 10 | 10 | 1 / 1 / 1 | expected zero, got 1.0 |
| X1 task_independence | other_task_reference | zero | 10 | 0 | 0 / 0 / 0 |  |
| P10 exception_path | unclosed_tags | zero | 3 | 0 | 0 / 0 / 0 |  |
| P10 exception_path | deep_nesting | zero | 3 | 0 | 0 / 0 / 0 |  |
| P10 exception_path | only_closers | zero | 3 | 0 | 0 / 0 / 0 |  |
| P10 exception_path | null_byte | zero | 3 | 0 | 0 / 0 / 0 |  |
| P10 exception_path | lone_surrogate | zero | 3 | 0 | 0 / 0 / 0 |  |
| P10 exception_path | control_chars | zero | 3 | 0 | 0 / 0 / 0 |  |
| P10 exception_path | leading_dashes | zero | 3 | 0 | 0 / 0 / 0 |  |
| P10 exception_path | huge_200kb | zero | 3 | 0 | 0 / 0 / 0 |  |
| P10 exception_path | huge_2mb | zero | 3 | 0 | 0 / 0 / 0 |  |
| P10 exception_path | content_none | zero | 3 | 0 | 0 / 0 / 0 |  |
| P10 exception_path | no_assistant_turn | zero | 3 | 0 | 0 / 0 / 0 |  |
| P11 determinism | reference | stable | 3 | 0 | 1 / 1 / 1 |  |
| P11 determinism | wrong_in_format | stable | 3 | 0 | 0 / 0 / 0 |  |
| T7 tool_call_first | noop_tool_call_then_answer | full | 3 | 0 | 1 / 1 / 1 |  |
| P12 dataset_hygiene | all_checks | zero | 1 | 0 | — |  |

## Reward terms on known-bad completions

Which part of the reward pays when the answer is wrong. A correctness term should be 0 everywhere.

| Reward term | Weight | Non-zero on | Mean value |
|---|---|---|---|
| correct | 1 | 39% | 0.386 |

## Static hints, cross-checked against the probes

Patterns in the reward source that are worth a look. A hint is not a finding; the last column says whether the dynamic probes agreed.

| Hint | Where | Source line | Probes say |
|---|---|---|---|
| reference_substring_match — the reference is looked for anywhere in the text (`answer in text`), so a reply that denies it or lists it among others matches too | falsepass_toy_env.py:26 `load_environment` | `return 1.0 if answer in reply else 0.0  # planted defect: should be reply.strip() == answer` | confirmed: P9 failed 10/20, P5 failed 10/30 |

## rewardlint corpus

rewardlint 0.1.0, stock corpus scored through this environment's reward code with the reference swapped into one real example.

- local/falsepass-toy-env: 100% false-negative rate.
- Profile: format_strict · false-positive rate 0 · false-negative rate 1 · errors 0

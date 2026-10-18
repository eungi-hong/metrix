"""The evaluation harness for relevance scoring.

Labels in `golden.jsonl` refer to candidates frozen in `snapshots/`, so a
result can be reproduced even though live search results drift. LLM answers
are recorded in `recordings/` keyed by prompt and model, so `python -m
evals.run --replay` recomputes every metric with no network or key. See
docs/EVALUATION.md.
"""

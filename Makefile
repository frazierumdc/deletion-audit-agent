# One-command workflows. Run `make setup` once, then activate .venv.
PYTHON ?= python3

.PHONY: setup lint format test data demo agent-offline evaluate leak-check agent replay clean

setup:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
	.venv/bin/pip install -e . --no-deps

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

test:
	pytest

# Rewrite data/synthetic/ from the generator (the committed copy must match it; a test checks).
data:
	python -m deletion_audit.synthetic

# Deterministic rules engine on the synthetic requests. No API key.
demo:
	python -m deletion_audit --engine rules
	python -m deletion_audit.evaluation.regression --validate out/output.csv

# The full agent pipeline with a scripted stand-in for Claude. No API key, no network.
agent-offline:
	python -m deletion_audit --engine agent --offline

# Score both engines on the sample answers and the hand-built cases (agent: offline).
evaluate:
	python -m deletion_audit.evaluation.score --engine both --custom --offline

# Prove that evidence dated after audit_at never changes an answer.
leak-check:
	python -m deletion_audit.evaluation.leak_check

# Live Claude run. Needs ANTHROPIC_API_KEY (environment or .env). Costs money.
agent:
	python -m deletion_audit --engine agent

# Re-create the last live run from the answer cache. No API key.
replay:
	python -m deletion_audit --engine agent --replay

clean:
	rm -rf .pytest_cache .ruff_cache out .cache/agent-offline

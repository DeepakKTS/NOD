# Nod — developer entry points.
#
# `make check` is the gate (CLAUDE.md §5): ruff, mypy, pytest with the coverage
# gate, and a bench smoke test. Run it before declaring anything done.

SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

UV := uv
RUN := $(UV) run
PYTHON_VERSION := 3.12
PATHS := src tests

.PHONY: help install fmt lint types test bench-smoke check run demo \
        bench bench-live bench-clean metrics report audit clean

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / \
		{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Install the pinned toolchain and all extras
	$(UV) python install $(PYTHON_VERSION)
	$(UV) sync --frozen --extra dev --extra bench

fmt: ## Format and apply safe lint fixes
	$(RUN) ruff format $(PATHS)
	$(RUN) ruff check --fix $(PATHS)

lint: ## Lint and check formatting
	$(RUN) ruff check $(PATHS)
	$(RUN) ruff format --check $(PATHS)

types: ## Type check (strict; no Any in nod_core)
	$(RUN) mypy

test: ## Run tests with the coverage gate on src/nod_core
	$(RUN) pytest

bench-smoke: ## Assert the harness is addressable (the bench leg of `make check`)
	$(RUN) pytest -m smoke tests/integration/test_bench_smoke.py --no-cov

check: ## The gate: lint, types, tests, bench smoke
	$(MAKE) lint
	$(MAKE) types
	$(MAKE) test
	$(MAKE) bench-smoke

run: ## Serve the API on http://127.0.0.1:8000
	$(RUN) uvicorn --factory nod_server.app:create_app \
		--host 127.0.0.1 --port 8000 --reload

demo: ## Reference intake agent plus console, via docker compose
	docker compose up --build

bench: ## Full benchmark offline against FakeAssemblyAI, no API key needed
	$(RUN) python -m nod_bench.replay --fake --out bench/runs

bench-live: ## Full benchmark against the real API; needs ASSEMBLYAI_API_KEY
	$(RUN) python -m nod_bench.replay --live --repeats 5 --out bench/runs

bench-clean: ## Drop the bench result cache
	rm -rf .nodcache

metrics: ## Recompute every metric from committed traces, zero API spend
	$(RUN) python -m nod_bench.metrics --runs bench/runs

report: ## Render the self-contained HTML report card
	$(RUN) python -m nod_bench.report --runs bench/runs --out bench/report.html

audit: ## Check dependencies for known vulnerabilities
	$(RUN) pip-audit --strict

clean: ## Remove tooling caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml \
		htmlcov dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

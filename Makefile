# ---------------------------------------------------------------------------
# Makefile — the common tasks, in one place.
#
#   make install    install the package with the dev extras
#   make test       run the whole test suite
#   make lint       run ruff (mypy is `make typecheck`)
#   make run        start the API locally
#   make docker     build and start the whole stack
#
# The lint and format targets cover src, tests and scripts — the same scope
# the CI workflow checks, so a local run and a CI run agree.
# ---------------------------------------------------------------------------

.DEFAULT_GOAL := help
.PHONY: help install install-dev test test-unit test-integration coverage \
        lint format typecheck run seed report build up down logs clean \
        migrate schema rules

PYTHON ?= python3
PIP    ?= $(PYTHON) -m pip

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install the package
	$(PIP) install .

install-dev: ## Install the package with the development extras
	$(PIP) install -e ".[dev]"

test: ## Run the whole test suite
	$(PYTHON) -m pytest tests -q

test-unit: ## Run the unit tests only
	$(PYTHON) -m pytest tests/unit -q

test-integration: ## Run the integration tests only
	$(PYTHON) -m pytest tests/integration -q

coverage: ## Run the tests with the coverage report
	$(PYTHON) -m pytest tests --cov=src/fin_kg --cov-report=term-missing --cov-report=html

lint: ## Run ruff
	ruff check src tests scripts

format: ## Format the code with ruff
	ruff format src tests scripts
	ruff check --fix src tests scripts

typecheck: ## Run mypy
	mypy src/fin_kg

run: ## Start the API locally
	$(PYTHON) -m fin_kg.cli serve --reload

seed: ## Load the synthetic dataset into the graph
	$(PYTHON) -m fin_kg.cli seed --companies 100

report: ## Print a research briefing (ENTITY_ID=...)
	$(PYTHON) -m fin_kg.cli report $(ENTITY_ID)

migrate: ## Apply the graph schema
	$(PYTHON) -m fin_kg.cli migrate

schema: ## Print the graph schema
	$(PYTHON) -m fin_kg.cli schema

rules: ## List the risk rules
	$(PYTHON) -m fin_kg.cli rules

build: ## Build the container image
	docker build -t fin-kg-risk-engine:latest .

up: ## Start the whole stack
	docker compose up -d --build

down: ## Stop the stack
	docker compose down

logs: ## Follow the container logs
	docker compose logs -f api worker

clean: ## Remove the build and test artefacts
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache htmlcov coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

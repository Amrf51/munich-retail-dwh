DOCKER ?= $(shell command -v docker 2>/dev/null || echo $(HOME)/.docker/bin/docker)
UV     ?= $(shell command -v uv 2>/dev/null || echo $(HOME)/.local/bin/uv)
PYTHON ?= $(shell command -v python3 2>/dev/null || echo python3)

export PYTHONPATH := src

.DEFAULT_GOAL := help

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package and dependencies
	$(UV) sync --all-extras

up:  ## Start Postgres + Adminer
	$(DOCKER) compose up -d
	@echo "Postgres  -> localhost:$${POSTGRES_PORT:-5433}"
	@echo "Adminer   -> http://localhost:$${ADMINER_PORT:-8080}"

down:  ## Stop containers, keep data
	$(DOCKER) compose down

nuke:  ## Stop containers and delete the data volume
	$(DOCKER) compose down -v

profile:  ## Profile the source CSV and regenerate docs/profiling_findings.md
	$(UV) run python scripts/initial_inspection.py --csv data/munich_retail_sales_raw.csv

migrate:  ## Deploy the schema. Safe to run repeatedly.
	$(UV) run munich-dwh migrate

extract:  ## Load the CSV into raw.sales_raw. Safe to run repeatedly.
	$(UV) run munich-dwh extract --csv data/munich_retail_sales_raw.csv

batches:  ## Show recent load batches
	$(UV) run munich-dwh batches

inspect:  ## Show what the schema currently contains
	$(UV) run munich-dwh inspect

psql:  ## Open a psql shell in the container
	$(DOCKER) compose exec postgres psql -U $${POSTGRES_USER:-dwh} -d $${POSTGRES_DB:-munich_retail}

.PHONY: help install up down nuke profile migrate extract batches inspect psql
.DEFAULT_GOAL := help

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up:  ## Start Postgres + Adminer
	docker compose up -d
	@echo "Postgres  -> localhost:$${POSTGRES_PORT:-5433}"
	@echo "Adminer   -> http://localhost:$${ADMINER_PORT:-8080}"

down:  ## Stop containers, keep data
	docker compose down

nuke:  ## Stop containers and delete the data volume
	docker compose down -v

psql:  ## Open a psql shell in the container
	docker compose exec postgres psql -U $${POSTGRES_USER:-dwh} -d $${POSTGRES_DB:-munich_retail}

.PHONY: help up down nuke psql
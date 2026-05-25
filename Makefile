.PHONY: help install install-hooks sync fmt lint lint-layering typecheck check test test-unit test-integration test-e2e \
        run dev down logs ps migrate migrate-create clean image-build dev-keystore protoc refresh-openapi

UV ?= uv
COMPOSE ?= docker compose

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

install: sync install-hooks ## Install dependencies + wire git hooks

sync: ## Sync the local virtualenv with pyproject.toml + uv.lock
	$(UV) sync

install-hooks: ## Point git at .husky/ for the pre-commit AGENTS.md link check
	git config core.hooksPath .husky

# ---------------------------------------------------------------------------
# code quality
# ---------------------------------------------------------------------------

fmt: ## Format code with ruff
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

lint: ## Lint with ruff (no fixes)
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

lint-layering: ## Enforce the layered-architecture rules (see ARCHITECTURE.md)
	$(UV) run python scripts/check_layering.py

typecheck: ## Run mypy
	$(UV) run mypy src

check: lint lint-layering typecheck ## Run lint + lint-layering + typecheck

# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

test: ## Run all tests
	$(UV) run pytest

test-unit: ## Run unit tests
	$(UV) run pytest -m unit

test-integration: ## Run integration tests (requires services running)
	$(UV) run pytest -m integration

test-e2e: ## Run end-to-end tests (requires full compose stack)
	$(UV) run pytest -m e2e

# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------

run: ## Run the gateway locally (bypasses docker)
	$(UV) run uvicorn livepeer_open_clearinghouse.main:app --reload --host 0.0.0.0 --port 8000

# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------

IMAGE_NAME ?= tztcloud/livepeer-open-clearinghouse-gateway
IMAGE_TAG ?= dev

image-build: ## Build the gateway image and tag it for local compose
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .

refresh-openapi: ## Snapshot /openapi.json into examples/ (requires a running gateway on :8000)
	@if ! curl -sf http://localhost:8000/openapi.json > examples/openapi.json.tmp; then \
		echo "error: could not reach http://localhost:8000/openapi.json — is the gateway running?"; \
		rm -f examples/openapi.json.tmp; \
		exit 1; \
	fi
	@mv examples/openapi.json.tmp examples/openapi.json
	@echo "snapshot updated: examples/openapi.json"
	@echo "next: re-run codegen in each SDK that needs it"
	@echo "  ts:     (cd examples/typescript && pnpm gen:openapi)"
	@echo "  python: (cd examples/python && uv run datamodel-codegen --input ../openapi.json --input-file-type openapi --output src/livepeer_open_clearinghouse_sdk/_generated.py --output-model-type dataclasses.dataclass --use-double-quotes)"
	@echo "  go:     (cd examples/go && oapi-codegen -package openclearinghouse -generate types -o livepeer_open_clearinghouse/_generated.go ../openapi.json)"

dev: ## Bring the full stack up (postgres + daemons + gateway)
	$(COMPOSE) up -d

down: ## Stop the stack
	$(COMPOSE) down

logs: ## Tail compose logs
	$(COMPOSE) logs -f --tail=200

ps: ## Show compose service status
	$(COMPOSE) ps

dev-keystore: ## Generate a V3 keystore for payment-daemon chain mode (optional)
	$(UV) run python scripts/dev-keystore.py

# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

migrate: ## Apply Alembic migrations
	$(UV) run alembic upgrade head

migrate-create: ## Create a new Alembic revision (usage: make migrate-create m="add foo")
	$(UV) run alembic revision --autogenerate -m "$(m)"

# ---------------------------------------------------------------------------
# protobuf
# ---------------------------------------------------------------------------

PROTO_DIR := proto
PROTO_OUT := src/livepeer_open_clearinghouse/_gen
PROTO_FILES := $(shell find $(PROTO_DIR) -name '*.proto' 2>/dev/null)

protoc: ## Regenerate Python gRPC stubs from proto/
	@mkdir -p $(PROTO_OUT)
	@touch $(PROTO_OUT)/__init__.py
	$(UV) run python -m grpc_tools.protoc \
		--proto_path=$(PROTO_DIR) \
		--python_out=$(PROTO_OUT) \
		--grpc_python_out=$(PROTO_OUT) \
		--pyi_out=$(PROTO_OUT) \
		$(PROTO_FILES)
	@echo "Regenerated stubs in $(PROTO_OUT)/"

# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------

clean: ## Remove build, cache, and lock-side artifacts (does NOT remove the venv)
	rm -rf build dist *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +
	find . -type d -name .mypy_cache -prune -exec rm -rf {} +
	find . -type d -name .ruff_cache -prune -exec rm -rf {} +

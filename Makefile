SHELL := /bin/bash

UV ?= uv
PNPM ?= pnpm
HELM ?= helm
CONTAINER_ENGINE ?= podman
NAMESPACE ?= telco-revenue-assurance-agent
RELEASE_NAME ?= telco-revenue-assurance-agent
REGISTRY ?= quay.io/yelias
VERSION ?= 0.1.0
LLM ?= vllm
LLM_ID ?= meta-llama/Llama-3.1-8B-Instruct
LLM_URL ?= http://llm-service:8000/v1
LLM_API_TOKEN ?=

.PHONY: setup lint test helm-depend helm-lint helm-template deploy undeploy verify-deploy build-all-images verify-model-assets

setup:
	$(UV) sync --all-packages
	$(PNPM) install --frozen-lockfile

lint:
	$(UV) run ruff check --config packages/configs/ruff.toml packages/
	find packages -type d \( -name .venv -o -name .tox -o -name node_modules \) -prune -o -type f -name '*.py' -print0 | xargs -0 $(UV) run ruff format --check --config packages/configs/ruff.toml
	$(UV) run mypy --config-file packages/configs/mypy.ini packages/
	@if find packages -mindepth 2 -maxdepth 2 -name package.json -print -quit | grep -q .; then \
		$(PNPM) exec turbo run lint typecheck; \
	else \
		printf '%s\n' 'No Node packages are present yet; skipping Node lint and type checks.'; \
	fi
	$(PNPM) exec prettier --config packages/configs/prettier.json --check .pre-commit-config.yaml CONVENTIONS.md package.json pnpm-workspace.yaml turbo.json 'packages/**/*.{ts,tsx,js,mjs,cjs,jsx,json,yml,yaml,md}'

test:
	@if find packages -type f \( -name 'test_*.py' -o -name '*_test.py' \) -print -quit | grep -q .; then \
		$(UV) run pytest -q packages; \
	else \
		printf '%s\n' 'No Python test files are present yet; skipping Python tests.'; \
	fi
	@if find packages -mindepth 2 -maxdepth 2 -name package.json -print -quit | grep -q .; then \
		$(PNPM) exec turbo run test; \
	else \
		printf '%s\n' 'No Node packages are present yet; skipping Node tests.'; \
	fi

helm-depend:
	$(HELM) dependency update chart/
	$(HELM) dependency build chart/

helm-lint:
	$(HELM) lint chart/

helm-template:
	$(HELM) template "$(RELEASE_NAME)" chart/ --namespace "$(NAMESPACE)" >/dev/null

verify-model-assets:
	$(UV) run --frozen python scripts/verify_model_assets.py --helm "$(HELM)"

deploy:
	$(HELM) upgrade --install "$(RELEASE_NAME)" chart/ --namespace "$(NAMESPACE)" --create-namespace

undeploy:
	$(HELM) uninstall "$(RELEASE_NAME)" --namespace "$(NAMESPACE)" --ignore-not-found

verify-deploy:
	$(HELM) status "$(RELEASE_NAME)" --namespace "$(NAMESPACE)"

build-all-images:
	@containerfiles=$$(find packages -type f -name Containerfile -print | sort); \
	if [[ -z "$$containerfiles" ]]; then \
		printf '%s\n' 'No package Containerfiles are present yet; skipping image builds.'; \
		exit 0; \
	fi; \
	while IFS= read -r containerfile; do \
		package_path=$${containerfile%/Containerfile}; \
		image_name=$$(basename "$$package_path"); \
		$(CONTAINER_ENGINE) build -f "$$containerfile" -t "$(REGISTRY)/$$image_name:$(VERSION)" .; \
	done <<< "$$containerfiles"

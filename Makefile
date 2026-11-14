# devon: self-healing Kubernetes agent. `make help` for targets.
SHELL := /bin/bash
include $(wildcard .env)
export
GITOPS_REPO ?= RaniGiterman/devon-gitops
GITOPS_REPO_URL ?= https://github.com/$(GITOPS_REPO).git
KUBECONFORM_VERSION := v0.6.7

.PHONY: help
help: ## Show targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

.PHONY: deps tools seed up down kubeconfigs agent agent-offline fake-llm test scenarios chaos-list reset barrage

deps: ## Install Python dependencies (uv)
	uv sync --python 3.12

tools: ## Download kubeconform into .work/bin (optional: put a kyverno CLI there too)
	mkdir -p .work/bin
	curl -sSL https://github.com/yannh/kubeconform/releases/download/$(KUBECONFORM_VERSION)/kubeconform-linux-amd64.tar.gz | tar -xz -C .work/bin kubeconform
	.work/bin/kubeconform -v

seed: ## Create + seed the gitops-manifests GitHub repo (once)
	GITOPS_REPO=$(GITOPS_REPO) ./infra/seed-gitops.sh

up: ## kind cluster + Prometheus/Alertmanager + ArgoCD + demo app + agent identities
	GITOPS_REPO_URL=$(GITOPS_REPO_URL) ./infra/bootstrap.sh

down: ## Delete the kind cluster
	kind delete cluster --name devon

kubeconfigs: ## Refresh the agent's read-only and gate kubeconfigs (tokens last 7 days)
	./infra/kubeconfigs.sh

agent: ## Run the agent (needs LLM_API_KEY and GITHUB_TOKEN in .env)
	uv run devon-agent

fake-llm: ## Run the scripted offline LLM on :8099
	uv run python -m tests.fake_llm --port 8099

agent-offline: ## Run the agent against the scripted LLM (start `make fake-llm` first)
	LLM_BASE_URL=http://127.0.0.1:8099/v1 LLM_API_KEY=fake LLM_REASONING_EFFORT= uv run devon-agent

test: ## Unit tests (no cluster needed)
	uv run pytest -q

scenarios: ## End-to-end fault scenarios (needs `make up` and a running agent)
	DEVON_SCENARIOS=1 uv run pytest -q -m scenario tests/scenarios -x

chaos-list: ## List injectable faults
	uv run devon-chaos list

reset: ## Restore the demo manifests to baseline
	uv run devon-chaos reset

barrage: ## Inject one fault per workload at once
	uv run devon-chaos barrage

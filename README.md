# devon: Self-Healing Kubernetes Agent

An AI agent that notices when a workload breaks, finds the change that broke it, and opens a fix as a pull request. A human approves the fix in Telegram, ArgoCD rolls it out, and the agent checks that the problem is gone. If it isn't, the agent reverts its own fix.

```
detect → diagnose ("what changed?") → fix as PR → human approves (Telegram) → ArgoCD applies → verify or revert
```

Built for an "AI Agents" hackathon. The end goal is to run it on a **closed network**, with open-weight models served by Ollama.

## Quick start

Prerequisites: Docker, kind, kubectl, helm, [uv](https://docs.astral.sh/uv/), gh (logged in to the account that owns the gitops repo). About 6 GB free disk and 8 GB RAM.

```bash
cp .env.example .env        # fill in LLM_API_KEY (Groq), GITHUB_TOKEN, optionally Telegram
make deps tools             # Python deps + kubeconform
make seed                   # once: create + seed the gitops repo (RaniGiterman/devon-gitops)
make up                     # kind + Prometheus/Alertmanager + ArgoCD + demo app (~5 min)
make agent                  # run the agent; scoreboard at http://localhost:8088
```

In another terminal, break something:

```bash
make chaos-list
uv run devon-chaos inject oom-limit     # or: make barrage
make reset                              # back to baseline when done
```

Approve the fix in Telegram, or on the scoreboard page if Telegram isn't configured.

### No API key yet?

A scripted, OpenAI-compatible stand-in LLM lets you run the whole loop offline. It recognises the chaos catalog's commits and proposes the reverse edit:

```bash
make fake-llm               # terminal 1
make agent-offline          # terminal 2
```

### Tests

```bash
make test                   # unit tests: detectors, gate, tool loop, state machine (no cluster)
make scenarios              # every catalog fault end to end (needs `make up` + a running agent)
```

## How it works

| Piece | Where | What it does |
|---|---|---|
| Detectors | `agent/ingest/detectors.py`, `agent/detect.py` | K8s API watch, Alertmanager webhook, log tail, synthetic prober (via the API server's service proxy), 5-minute sweep. Deterministic; no LLM. |
| Incident manager | `agent/manager.py`, `agent/store.py` | State machine in SQLite. One incident per workload; signals merge into it. |
| LLM | `agent/llm/` | One OpenAI-compatible client and a small tool loop. Triage on the fast model, investigation and fixes on the smart one. |
| Read-only tools | `agent/tools/readonly.py` | `k8s_get`, `k8s_events`, `k8s_logs`, `prom_query`, `git_log`, `git_show`, `read_manifest`, `argocd_status`, `runbook_search`. |
| Validation gate | `agent/gate/` | Field-level fix allowlist, kustomize render, kubeconform, server-side dry run, policies, blast radius. |
| Approvals | `agent/notify/` | Telegram bot (inline buttons, approver allowlist) or the scoreboard's web page. |
| Verifier | `agent/verify.py` | Waits for ArgoCD to sync the fix, then watches the workload; reverts the fix if the symptom persists. |
| Chaos | `chaos/cli.py` | Faults as ordinary-looking commits, so "what changed?" is real. |

### Safety

- The agent uses two Kubernetes identities (`infra/rbac/agent.yaml`). `devon-agent` is read-only (no Secrets) and is the only one the LLM's tools use. `devon-gate` can write in the `demo` namespace, but is only ever used with `--dry-run=server`.
- The only way the agent changes the cluster is a PR that a human merges. Postmortems are committed directly, but only under `runbooks/`, which ArgoCD doesn't sync.
- Every proposed change is diffed field by field against an allowlist (`agent/gate/allowlist.py`); anything else never reaches a human.
- Logs and events are passed to the model as data, and the model's tools cannot write.

## LLM setup

| | Hackathon | Closed network |
|---|---|---|
| Endpoint | Groq · `https://api.groq.com/openai/v1` | Ollama · `http://ollama:11434/v1` |
| Investigation + fixes | `openai/gpt-oss-20b` | `gpt-oss:20b` (~16 GB GPU) |
| Triage, explanations, postmortems | `qwen/qwen3.8-27b` | `gpt-oss:20b` (same model is fine locally) |

Switch by changing `LLM_BASE_URL`, `LLM_API_KEY` and the model names in `.env`.

**Groq free tier:** limits are per model (8K tokens/minute and 1,000 requests/day each on our key), so triage and investigation use different models to get separate budgets. `LLM_REASONING_EFFORT=low` (sent to gpt-oss only) and trimming older tool output keep a whole incident around 10K tokens. Check `x-ratelimit-*` response headers or the Groq console for your org's current limits. A bigger model (`openai/gpt-oss-120b`) works too, but uses the same per-minute budget faster.

## Closed-network swaps

| Piece | Hackathon | Closed network |
|---|---|---|
| LLM | Groq | Ollama |
| Approvals | Telegram bot | Mattermost / Rocket.Chat bot, or the built-in web approval page (**to decide**; Telegram needs the internet) |
| Git server | GitHub (`agent/githost/github.py`) | Gitea / GitLab (same three methods) |
| Images, packages | public registries, PyPI | Harbor mirror, wheelhouse; kubeconform also needs a local schema mirror |

## Known limits

- **PVC filling up** is in the catalog but refuses to run on kind's default `local-path` storage: it has no quota, so it would fill the laptop's disk. It needs a CSI driver with volume stats (e.g. `csi-driver-host-path`).
- **Approvals from the scoreboard** have no authentication. Fine on a laptop; put it behind auth anywhere else.
- **ArgoCD polls git every 20 s**, so a merged fix takes up to ~30 s to apply.
- The kubeconfig tokens expire after 7 days; run `make kubeconfigs`.

## Design docs

| File | What's in it |
|---|---|
| [design/architecture.html](design/architecture.html) | Full design doc: demo story, architecture, detection layers, components, closed-network path, guardrails, fault catalog, decisions, work split, risks. |
| [design/diagrams.md](design/diagrams.md) | Architecture, detection, incident lifecycle and sequence diagrams in Mermaid. |
| [design/plan.md](design/plan.md) | The original planning outline. |

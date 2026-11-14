# devon: Self-Healing Kubernetes Agent

An AI agent that notices when a workload breaks, finds the change that broke it, and opens a fix as a pull request. A human approves the fix in Telegram, ArgoCD rolls it out, and the agent checks that the problem is gone. If it isn't, the agent reverts its own fix.

```
detect → diagnose ("what changed?") → fix as PR → human approves (Telegram) → ArgoCD applies → verify or revert
```

Built for an "AI Agents" hackathon. The end goal is to run it on a **closed network**, with open-weight models served by Ollama.

## Status

Design phase. No code yet.

## Design docs

| File | What's in it |
|---|---|
| [design/architecture.html](design/architecture.html) | Full design doc: demo story, architecture diagram, components, closed-network path, guardrails, fault catalog, decisions, work split, risks. Open it in a browser. |
| [design/diagrams.md](design/diagrams.md) | Architecture, incident lifecycle and sequence diagrams, in Mermaid (renders on GitHub). |
| [design/plan.md](design/plan.md) | The original planning outline (stack choices since changed; see below). |

## Key ideas

- **The agent cannot write to the cluster.** It has read-only RBAC. Every fix goes through a GitOps PR that a human approves.
- **"What changed?" is the core of the diagnosis.** Faults are injected as git commits, so the agent links each failure to the commit that caused it.
- **The agent proves its fixes work.** It watches the rollout after each fix and reverts the fix if the symptom persists.
- **One codebase, internet or air-gapped.** The LLM, the chat channel and the git server each sit behind a small interface. Moving to the closed network is a configuration change.

## LLM setup

All model calls use the OpenAI-compatible Chat Completions API (with tool calling), so the same client talks to both backends:

| | Hackathon | Closed network |
|---|---|---|
| Endpoint | Groq · `https://api.groq.com/openai/v1` | Ollama · `http://ollama:11434/v1` |
| Investigation + fixes | `openai/gpt-oss-120b` | `gpt-oss:120b` (~80 GB GPU) |
| Triage | `openai/gpt-oss-20b` | `gpt-oss:20b` (~16 GB GPU) |

Both backends run the same open weights, so prompts tuned at the hackathon carry over.

## Closed-network swaps

| Piece | Hackathon | Closed network |
|---|---|---|
| LLM | Groq | Ollama |
| Approvals | Telegram bot | Mattermost / Rocket.Chat bot or a web approval page (**to decide**; Telegram needs the internet) |
| Git server | GitHub | Gitea / GitLab |
| Images, packages | public registries, PyPI | Harbor mirror, wheelhouse |

## Planned stack

Python · `openai` SDK (own tool loop, no agent framework) · gpt-oss via Groq / Ollama · kind · ArgoCD · Kustomize · kube-prometheus-stack · Kyverno · `python-telegram-bot` (long polling) · SQLite

## Planned layout

```
agent/        ingest, incidents, llm, tools, gate, notify, githost, verify
chaos/        fault catalog + CLI
scoreboard/   live demo board
infra/        kind, bootstrap, RBAC, policies
tests/        one scenario test per fault
```

The Kubernetes manifests live in a separate `gitops-manifests` repo, which ArgoCD watches.

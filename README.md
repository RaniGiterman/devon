# devon: Self-Healing Kubernetes Agent

An AI agent that notices when a workload breaks, finds the change that broke it, and opens a fix as a pull request. A human approves the fix in Slack, ArgoCD rolls it out, and the agent checks that the problem is gone. If it isn't, the agent reverts its own fix.

```
detect → diagnose ("what changed?") → fix as PR → human approves → ArgoCD applies → verify or revert
```

Built for an "AI Agents" hackathon.

## Status

Design phase. No code yet.

## Design docs

| File | What's in it |
|---|---|
| [design/architecture.html](design/architecture.html) | Full design doc: demo story, architecture diagram, components, guardrails, fault catalog, decisions, work split, risks. Open it in a browser. |
| [design/diagrams.md](design/diagrams.md) | Architecture, incident lifecycle and sequence diagrams, in Mermaid (renders on GitHub). |
| [design/plan.md](design/plan.md) | The original planning outline. |

## Key ideas

- **The agent cannot write to the cluster.** It has read-only RBAC. Every fix goes through a GitOps PR that a human approves.
- **"What changed?" is the core of the diagnosis.** Faults are injected as git commits, so the agent links each failure to the commit that caused it.
- **The agent proves its fixes work.** It watches the rollout after each fix and reverts the fix if the symptom persists.
- **Each incident improves the next one.** The agent writes a postmortem into the runbooks after every incident, and searches those runbooks during later investigations.

## Planned stack

Python · Claude Agent SDK (`claude-opus-5-5` for investigation, `claude-haiku-5-5` for triage) · kind · ArgoCD · Kustomize · kube-prometheus-stack · Kyverno · Slack Bolt (Socket Mode) · SQLite

## Planned layout

```
agent/        ingestion, incidents, LLM, tools, validation gate, Slack, verifier
chaos/        fault catalog + CLI
scoreboard/   live demo board
infra/        kind, bootstrap, RBAC, policies
tests/        one scenario test per fault
```

The Kubernetes manifests live in a separate `gitops-manifests` repo, which ArgoCD watches.

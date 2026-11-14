# Plan: Self-Healing Kubernetes Agent — Architecture & Design Doc

## Context
The team is entering an "AI Agents" hackathon. The chosen idea is a self-healing loop for Kubernetes:
detect → diagnose ("what changed?") → propose fix as GitOps PR → human approves in Slack → ArgoCD applies → verify (or auto-rollback),
demoed live with a chaos script and a resolution scoreboard. Before building, the user wants a design doc with
architecture graphs and every decision the team must make. The working dir `/home/kingrani/source/devon` is empty — no code yet.

## Deliverable
One HTML Artifact (private, shareable with the team) titled **"Self-Healing K8s Agent"** containing:

1. **One-line pitch + demo story**: what judges see in 3 minutes.
2. **System architecture diagram** (inline SVG, light/dark): the pieces below and how data flows between them.
3. **Incident state machine diagram**: DETECTED → TRIAGED → INVESTIGATING → FIX_PROPOSED → AWAITING_APPROVAL → APPLYING → VERIFYING → RESOLVED | ROLLED_BACK | ESCALATED.
4. **Sequence diagram of one incident** (e.g. a bad memory limit commit causing an OOM kill), end to end.
5. **Component specs** (responsibility, inputs/outputs, tech):
   - **Signal ingestion**: K8s watcher (Events + Pod status: CrashLoopBackOff, ImagePullBackOff, OOMKilled, Pending, probe failures) + Alertmanager webhook → dedup/debounce → incident queue.
   - **Incident manager**: state machine, SQLite store, one incident per (namespace, workload, symptom).
   - **Triage** (cheap model): classify, dedupe, decide if worth investigating.
   - **Investigator agent** (Claude Agent SDK): read-only tools `k8s_get/describe/logs/events`, `prom_query`, `git_log/diff` (manifests repo), `argocd_history`, `runbook_search`. Outputs a structured diagnosis JSON: root cause, evidence, suspected change/commit, confidence.
   - **Fixer**: patches the manifests repo (Kustomize YAML) → validation gate (kubeconform, `kubectl apply --dry-run=server`, Kyverno CLI policies, blast-radius summary) → GitHub PR.
   - **Approval**: Slack Block Kit card (diagnosis, evidence, diff, risk) with Approve / Reject / Explain-more; Approve merges the PR.
   - **Apply**: ArgoCD sync of the merged commit.
   - **Verifier**: watches rollout and the original symptom for N min → resolve, or `git revert` + escalate.
   - **Postmortem writer**: auto-generates a postmortem into the runbook repo, so the system learns from each incident.
   - **Chaos harness + scoreboard**: fault injection scripts + small web page (incidents, status, time to resolve).
   - **Demo workload**: 3-service app (frontend, api, redis) on kind.
6. **Safety & guardrails section**: read-only ClusterRole for the agent; writes only via git PR; allowlist of fix types; namespace scope; rate limit on PRs; validation gate; logs/events treated as untrusted input (prompt-injection defense); full audit trail through git.
7. **Fault scenario catalog** (doubles as the test suite): bad image tag, memory limit too low (OOM), missing ConfigMap/Secret key, wrong Service selector, NetworkPolicy blocking traffic, broken liveness probe, unschedulable resource request, plus 1 deliberately "escalate, don't fix" case. Each lists: injection method, expected diagnosis, expected fix.
8. **Decisions to make before building** (table: options, recommendation, why), including:
   - Language: Python (recommended, Claude Agent SDK + kubernetes client) vs TypeScript
   - Models: `claude-opus-5-5` for investigate/fix, `claude-haiku-5-5` for triage
   - K8s tooling: own thin read-only tools (recommended) vs an existing K8s MCP server
   - GitOps: ArgoCD (recommended, visual UI for the demo) vs Flux
   - Manifest format: plain Kustomize (recommended, easy for the LLM to patch) vs Helm
   - **Fault injection via git commits vs kubectl drift.** ArgoCD self-heal would silently undo kubectl chaos, so inject through git commits; that also makes "what changed?" real.
   - Approval: always human (recommended for the demo) + optional auto-approve for low-risk classes
   - Cluster: kind on a laptop (recommended) + recorded backup video vs a cloud cluster
   - Observability: kube-prometheus-stack + Loki (or plain `kubectl logs`)
   - Runbook search: plain grep / file read by the agent (recommended) vs a vector DB
   - State: SQLite vs Redis
   - Project name
9. **Repo layout** (monorepo: `agent/`, `chaos/`, `gitops-manifests/` (separate repo), `scoreboard/`, `infra/` kind + bootstrap).
10. **Work split & timeline**: 4 workstreams (Platform/infra, Agent core, Fix/approval/verify loop, Chaos+demo+pitch), with milestones: M1 cluster + GitOps up, M2 detect→diagnose in Slack, M3 PR + approve + verify, M4 all scenarios green + rehearsed demo. Hackathon length and team size are shown as assumptions to adjust.
11. **Risks & mitigations** (flaky live demo, LLM wrong fix, API latency, Wi-Fi), plus answers to likely judge questions.

## Execution steps (after approval)
1. `Artifact` quickstart (intent `other`), then load the `artifact-design` and `artifact-diagramming` skills.
2. Write the HTML to the scratchpad: inline SVG diagrams with theme tokens, responsive layout, collapsible component specs, decision table.
3. Publish with icon `diagram`; share the link.

## Verification
- The page renders in light and dark mode, and the diagrams are legible at phone width (no horizontal scroll).
- Every component in the architecture diagram has a spec section; every scenario maps to a component path.
- The decision table covers all open choices listed above.

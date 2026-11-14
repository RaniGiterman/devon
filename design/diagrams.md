# Diagrams

GitHub renders these Mermaid diagrams inline. The hand-drawn architecture diagram is in [architecture.html](architecture.html).

## Architecture

The agent reads the cluster but never writes to it directly. Its only write path is a PR to the manifests repo. A human merges the PR from Slack, and ArgoCD applies the change.

```mermaid
flowchart LR
  subgraph cluster["kind cluster"]
    app["Demo app<br/>frontend · api · redis"]
    prom["Prometheus + Alertmanager"]
    api["Kubernetes API"]
    argo["ArgoCD"]
  end

  subgraph agent["Agent service (Python + Claude Agent SDK)"]
    ingest["Signal ingestion"] --> inc["Incident manager<br/>SQLite"]
    inc --> triage["Triage · Haiku 5.5"]
    triage --> inv["Investigator · Opus 5.5"]
    inv --> fix["Fixer + validation gate"]
    fix -. "after merge + sync" .-> ver["Verifier"]
    ver --> pm["Postmortem writer"]
  end

  subgraph tools["Team tools"]
    slack["Slack approval"]
    rb["Runbooks + postmortems"]
    chaos["Chaos harness"]
  end

  gh[("GitHub<br/>gitops-manifests")]

  app -- scrape --> prom
  prom -- alerts --> ingest
  api -- watch --> ingest
  inv -. "read-only: kubectl, PromQL" .-> api
  inv -. search .-> rb
  pm -- writes --> rb
  inc -- "card + diff" --> slack
  slack -- approve --> inc
  fix -- PR --> gh
  ver -- revert --> gh
  chaos -- "bad commit" --> gh
  gh -- "sync on merge" --> argo
  argo -- applies --> api
```

## Incident lifecycle

```mermaid
stateDiagram-v2
  direction LR
  [*] --> DETECTED
  DETECTED --> TRIAGED: dedup + debounce 30s
  TRIAGED --> DROPPED: noise / self-recovered
  TRIAGED --> INVESTIGATING: actionable
  INVESTIGATING --> FIX_PROPOSED: confidence ≥ 0.7
  INVESTIGATING --> ESCALATED: low confidence / not in allowlist
  FIX_PROPOSED --> AWAITING_APPROVAL: validation gate passes
  FIX_PROPOSED --> INVESTIGATING: validation fails (max 2 retries)
  AWAITING_APPROVAL --> APPLYING: Approve = merge PR
  AWAITING_APPROVAL --> INVESTIGATING: Reject + feedback
  APPLYING --> VERIFYING: ArgoCD synced
  VERIFYING --> RESOLVED: healthy for 3 min
  VERIFYING --> ROLLED_BACK: symptom persists
  ROLLED_BACK --> ESCALATED: git revert + page human
  RESOLVED --> [*]
  ESCALATED --> [*]
  DROPPED --> [*]
```

## One incident: the bad memory limit

```mermaid
sequenceDiagram
  autonumber
  participant C as Chaos harness
  participant G as GitHub
  participant A as ArgoCD
  participant K as K8s API
  participant W as Agent
  participant L as Claude
  participant S as Slack
  participant E as Engineer
  C->>G: commit api memory 512Mi → 64Mi
  G->>A: webhook
  A->>K: apply Deployment
  K-->>W: Pod OOMKilled, CrashLoopBackOff
  W->>L: triage (Haiku)
  L-->>W: actionable
  W->>L: investigate (Opus + tools)
  L->>K: describe pod, events, logs
  L->>G: git log -p apps/api
  L-->>W: diagnosis JSON: commit a1b2c3, peak 310Mi
  W->>L: propose fix
  L-->>W: patch memory 512Mi
  W->>W: kubeconform, dry-run=server, kyverno
  W->>G: open PR #42
  W->>S: card: cause, evidence, diff, risk
  E->>S: Approve
  S->>W: interaction callback
  W->>G: merge PR #42
  G->>A: webhook
  A->>K: apply fix
  W->>K: watch rollout + restarts for 3 min
  W->>S: ✅ resolved in 2m14s
  W->>G: commit postmortem
```

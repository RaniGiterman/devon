# Diagrams

GitHub renders these Mermaid diagrams inline. The hand-drawn architecture diagram is in [architecture.html](architecture.html).

## Architecture

The agent reads the cluster but never writes to it directly. Its only write path is a PR to the manifests repo. A human merges the PR from Telegram, and ArgoCD applies the change. All model calls go to one OpenAI-compatible endpoint: Groq at the hackathon, Ollama on the closed network.

```mermaid
flowchart LR
  subgraph cluster["kind cluster"]
    app["Demo app<br/>frontend · api · redis"]
    prom["Prometheus + Alertmanager"]
    api["Kubernetes API"]
    argo["ArgoCD"]
  end

  subgraph agent["Agent service (Python)"]
    ingest["Signal ingestion"] --> inc["Incident manager<br/>SQLite"]
    inc --> triage["Triage · gpt-oss-20b"]
    triage --> inv["Investigator · gpt-oss-120b"]
    inv --> fix["Fixer + validation gate"]
    fix -. "after merge + sync" .-> ver["Verifier"]
    ver --> pm["Postmortem writer"]
  end

  subgraph tools["Team tools"]
    tg["Telegram bot"]
    llm["LLM endpoint (OpenAI API)<br/>Groq now · Ollama on closed net"]
    rb["Runbooks + postmortems"]
    chaos["Chaos harness"]
  end

  gh[("Git server · gitops-manifests<br/>GitHub now · Gitea on closed net")]

  app -- scrape --> prom
  prom -- alerts --> ingest
  api -- watch --> ingest
  inv -. "read-only: kubectl, PromQL" .-> api
  inv -. search .-> rb
  triage -- "LLM calls" --> llm
  inv -- "LLM calls" --> llm
  fix -- "LLM calls" --> llm
  pm -- writes --> rb
  inc -- "card + diff" --> tg
  tg -- approve --> inc
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
  participant G as Git server
  participant A as ArgoCD
  participant K as K8s API
  participant W as Agent
  participant L as LLM (gpt-oss)
  participant T as Telegram bot
  participant E as Engineer
  C->>G: commit api memory 512Mi → 64Mi
  G->>A: webhook
  A->>K: apply Deployment
  K-->>W: Pod OOMKilled, CrashLoopBackOff
  W->>L: triage (gpt-oss-20b)
  L-->>W: actionable
  W->>L: investigate (gpt-oss-120b + tool schemas)
  L-->>W: tool_calls: describe pod, events, logs
  W->>K: run read-only tools
  W->>L: tool results
  L-->>W: tool_call: git log -p apps/api
  W->>G: git log -p apps/api
  W->>L: tool results
  L-->>W: diagnosis JSON: commit a1b2c3, peak 310Mi
  W->>L: propose fix
  L-->>W: patch memory 512Mi
  W->>W: kubeconform, dry-run=server, kyverno
  W->>G: open PR #42
  W->>T: message: cause, evidence, diff, risk + buttons
  E->>T: Approve
  T->>W: callback_query
  W->>G: merge PR #42
  G->>A: webhook
  A->>K: apply fix
  W->>K: watch rollout + restarts for 3 min
  W->>T: ✅ resolved in 2m14s
  W->>G: commit postmortem
```

The model never touches the cluster. It only asks for tool calls; the agent runs them, and every tool is read-only.

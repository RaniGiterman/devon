"""All runtime settings. Read from environment variables or .env."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # LLM: any OpenAI-compatible endpoint (Groq now, Ollama on the closed network).
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str = ""
    model_fast: str = "openai/gpt-oss-20b"
    model_smart: str = "openai/gpt-oss-120b"
    llm_reasoning_effort: str = ""  # low | medium | high; empty = don't send
    llm_max_concurrency: int = 1  # free-tier rate limits are per minute; parallel calls just collide

    # Cluster access. Two identities, see infra/rbac/agent.yaml.
    kubeconfig_read: Path = ROOT / ".work/devon-agent.kubeconfig"
    kubeconfig_gate: Path = ROOT / ".work/devon-gate.kubeconfig"
    # NoDecode: read "a,b" as a comma list instead of requiring JSON.
    watch_namespaces: Annotated[list[str], NoDecode] = ["demo"]
    prometheus_url: str = "http://localhost:9090"
    alertmanager_url: str = "http://localhost:9093"
    argocd_namespace: str = "argocd"
    argocd_app: str = "demo"

    # GitOps repo.
    gitops_repo: str = "RaniGiterman/devon-gitops"
    github_token: str = ""
    gitops_branch: str = "main"
    gitops_apps_path: str = "apps"
    gitops_workdir: Path = ROOT / ".work/agent-gitops"

    # Approvals.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_approvers: Annotated[list[str], NoDecode] = []  # numeric user IDs and/or @usernames

    # Behaviour.
    agent_host: str = "0.0.0.0"
    agent_port: int = 8088
    db_path: Path = ROOT / ".work/devon.db"
    debounce_s: int = 30
    verify_window_s: int = 180
    sync_timeout_s: int = 240
    probe_interval_s: int = 15
    probe_failures_to_signal: int = 3
    log_poll_s: int = 15
    log_errors_to_signal: int = 3
    sweep_interval_s: int = 300
    max_tool_calls: int = 15
    max_prs_per_hour: int = 5
    max_fix_attempts: int = 2
    min_confidence: float = 0.7
    policies_dir: Path = ROOT / "infra/policies"
    bin_dir: Path = ROOT / ".work/bin"

    @field_validator("watch_namespaces", "telegram_approvers", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, int):
            return [str(v)]
        return v

    @property
    def gitops_url(self) -> str:
        return f"https://github.com/{self.gitops_repo}.git"


@lru_cache
def get_settings() -> Settings:
    return Settings()

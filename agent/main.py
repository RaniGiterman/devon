"""Wire everything together and run: detectors, incident manager, web server, notifier."""

from __future__ import annotations

import asyncio
import logging
import signal
import socket

import uvicorn

from agent.config import get_settings
from agent.gate.validate import Gate
from agent.githost.github import GitHubHost
from agent.gitrepo import GitRepo
from agent.ingest.detectors import DetectorHealth, ProbeTarget, Watcher, run_log_tail, run_prober, run_sweep
from agent.kube import Kube
from agent.llm.client import LLM
from agent.manager import IncidentManager
from agent.notify.base import ConsoleNotifier
from agent.server import create_app
from agent.store import Store
from agent.tools.readonly import build_tools
from agent.verify import Verifier

log = logging.getLogger("devon")

DEFAULT_PROBES = ["demo/frontend:80/=frontend", "demo/api:80/healthz=api", "demo/api:80/deps=api"]


def _web_url(port: int) -> str:
    return f"http://{socket.gethostname()}:{port}"


async def main() -> None:
    s = get_settings()
    missing = [n for n, v in (("LLM_API_KEY", s.llm_api_key), ("GITHUB_TOKEN", s.github_token)) if not v]
    if missing:
        log.warning("missing settings: %s. Detection works; investigations/PRs will fail until set (see .env.example).",
                    ", ".join(missing))

    store = Store(s.db_path)
    kube = Kube(s.kubeconfig_read)
    repo = GitRepo(s.gitops_url, s.gitops_workdir, s.gitops_branch, s.github_token)
    try:
        await repo.sync()
    except Exception as e:
        log.warning("could not sync gitops repo yet: %s", e)
    tools = build_tools(s.kubeconfig_read, repo, s.prometheus_url, s.gitops_apps_path, s.argocd_namespace,
                        s.argocd_app, s.watch_namespaces[0])
    llm = LLM(s.llm_base_url, s.llm_api_key, s.model_fast, s.model_smart, s.llm_reasoning_effort, s.llm_max_concurrency)
    gate = Gate(s.kubeconfig_gate, s.watch_namespaces[0], s.policies_dir, s.bin_dir, s.gitops_apps_path)
    web_url = _web_url(s.agent_port)
    if s.telegram_bot_token and s.telegram_chat_id:
        from agent.notify.telegram import TelegramNotifier
        notifier, notifier_name = TelegramNotifier(s.telegram_bot_token, s.telegram_chat_id, s.telegram_approvers, web_url), "telegram"
    else:
        notifier, notifier_name = ConsoleNotifier(), "web"
    verifier = Verifier(kube, store, s.watch_namespaces, s.argocd_namespace, s.argocd_app, s.alertmanager_url)
    manager = IncidentManager(s, store, llm, tools, repo, GitHubHost(s.gitops_repo, s.github_token, s.gitops_branch),
                              gate, notifier, kube, verifier, web_url)
    manager.recover_after_restart()

    health = DetectorHealth()
    loop = asyncio.get_running_loop()
    await notifier.start(manager)
    Watcher(kube, s.watch_namespaces, s.argocd_namespace, manager.emit, health, loop).start()
    targets = [ProbeTarget.parse(p) for p in DEFAULT_PROBES if p.split("/")[0] in s.watch_namespaces]
    bg = [
        asyncio.create_task(run_sweep(kube, s.watch_namespaces, s.argocd_namespace, s.argocd_app, s.sweep_interval_s, manager.emit, health)),
        asyncio.create_task(run_log_tail(kube, s.watch_namespaces, s.log_poll_s, s.log_errors_to_signal, manager.emit, health)),
        asyncio.create_task(run_prober(kube, targets, s.probe_interval_s, s.probe_failures_to_signal, manager.emit, health)),
    ]

    app = create_app(manager, store, health, s, notifier_name)
    server = uvicorn.Server(uvicorn.Config(app, host=s.agent_host, port=s.agent_port, log_level="warning"))
    log.info("devon agent up: scoreboard http://localhost:%d  llm=%s (%s / %s)  approvals=%s",
             s.agent_port, s.llm_base_url, s.model_fast, s.model_smart, notifier_name)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    srv = asyncio.create_task(server.serve())
    await stop.wait()
    server.should_exit = True
    for t in bg:
        t.cancel()
    await notifier.stop()
    await srv


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())


if __name__ == "__main__":
    run()

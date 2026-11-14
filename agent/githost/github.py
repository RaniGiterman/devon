"""GitHost interface + GitHub implementation. A Gitea implementation only needs the
same three methods (Gitea's API mirrors GitHub's for these calls)."""

from __future__ import annotations

from typing import Protocol

import httpx


class GitHost(Protocol):
    async def open_pr(self, branch: str, title: str, body: str) -> tuple[int, str]: ...
    async def merge_pr(self, number: int, title: str) -> str: ...
    async def close_pr(self, number: int, comment: str = "") -> None: ...


class GitHubHost:
    def __init__(self, repo: str, token: str, base: str = "main", api: str = "https://api.github.com"):
        self.repo, self.base = repo, base
        self.http = httpx.AsyncClient(base_url=api, timeout=20, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"})

    async def _req(self, method: str, path: str, **kw) -> dict:
        r = await self.http.request(method, f"/repos/{self.repo}{path}", **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    async def open_pr(self, branch: str, title: str, body: str) -> tuple[int, str]:
        pr = await self._req("POST", "/pulls", json={"title": title, "head": branch, "base": self.base, "body": body})
        return pr["number"], pr["html_url"]

    async def merge_pr(self, number: int, title: str) -> str:
        res = await self._req("PUT", f"/pulls/{number}/merge", json={"merge_method": "squash", "commit_title": title})
        return res["sha"]

    async def close_pr(self, number: int, comment: str = "") -> None:
        if comment:
            await self._req("POST", f"/issues/{number}/comments", json={"body": comment})
        await self._req("PATCH", f"/pulls/{number}", json={"state": "closed"})

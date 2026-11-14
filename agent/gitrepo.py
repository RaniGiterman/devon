"""Local clone of the gitops-manifests repo. All git operations go through here."""

from __future__ import annotations

import asyncio
import base64
import re
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{4,40}$")
PATH_RE = re.compile(r"^[A-Za-z0-9_.\-/]+$")


class GitError(RuntimeError):
    pass


class GitRepo:
    def __init__(self, url: str, workdir: Path, branch: str = "main", token: str = "",
                 author: tuple[str, str] = ("devon-agent", "devon-agent@localhost")):
        self.url, self.dir, self.branch, self.token = url, Path(workdir), branch, token
        self.author = author
        self.lock = asyncio.Lock()

    def _auth(self) -> list[str]:
        if not self.token:
            return []
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]

    async def git(self, *args: str, check: bool = True, cwd: Path | None = None) -> str:
        cmd = ["git", *self._auth(), "-c", f"user.name={self.author[0]}", "-c", f"user.email={self.author[1]}", *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd or self.dir), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(self.dir.parent)})
        out, _ = await proc.communicate()
        text = out.decode(errors="replace")
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args[:3])} failed: {text.strip()[:500]}")
        return text

    async def sync(self) -> str:
        """Clone if needed, then hard-reset to the remote branch. Returns HEAD sha."""
        async with self.lock:
            if not (self.dir / ".git").exists():
                self.dir.parent.mkdir(parents=True, exist_ok=True)
                await self.git("clone", "-q", "-b", self.branch, self.url, str(self.dir), cwd=self.dir.parent)
            await self.git("fetch", "-q", "origin", self.branch)
            await self.git("checkout", "-q", "-B", self.branch, f"origin/{self.branch}")
            await self.git("reset", "-q", "--hard", f"origin/{self.branch}")
            await self.git("clean", "-qfd")
            return (await self.git("rev-parse", "HEAD")).strip()

    # --- read operations (used by investigator tools) ----------------------
    async def log(self, path: str = "", n: int = 10) -> str:
        if path and not PATH_RE.match(path):
            raise GitError("invalid path")
        args = ["log", f"-n{min(int(n), 30)}", "--date=iso-strict", "--format=%h %ad %an%n    %s", "--stat=100"]
        return await self.git(*args, *(["--", path] if path else []))

    async def show(self, sha: str) -> str:
        if not SHA_RE.match(sha):
            raise GitError("invalid commit sha")
        return await self.git("show", "--date=iso-strict", "--format=commit %H%nAuthor: %an%nDate: %ad%n%n    %s%n", sha)

    def read(self, path: str) -> str:
        p = self._safe(path)
        return p.read_text()

    def list_files(self, sub: str) -> list[str]:
        base = self._safe(sub)
        return sorted(str(p.relative_to(self.dir)) for p in base.rglob("*") if p.is_file())

    def _safe(self, rel: str) -> Path:
        if not PATH_RE.match(rel) or ".." in rel.split("/"):
            raise GitError(f"invalid path {rel!r}")
        p = (self.dir / rel).resolve()
        if not str(p).startswith(str(self.dir.resolve())):
            raise GitError("path escapes repo")
        return p

    # --- write operations (only used after validation / approval) ----------
    async def commit_branch(self, branch: str, files: dict[str, str], message: str) -> str:
        """Create `branch` from origin/main with `files` written, push it. Returns sha."""
        async with self.lock:
            await self.git("fetch", "-q", "origin", self.branch)
            await self.git("checkout", "-q", "-B", branch, f"origin/{self.branch}")
            try:
                for rel, content in files.items():
                    p = self._safe(rel)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content)
                await self.git("add", "-A")
                await self.git("commit", "-q", "-m", message)
                sha = (await self.git("rev-parse", "HEAD")).strip()
                await self.git("push", "-q", "-f", "origin", f"{branch}:{branch}")
                return sha
            finally:
                await self.git("checkout", "-q", "-B", self.branch, f"origin/{self.branch}", check=False)
                await self.git("reset", "-q", "--hard", f"origin/{self.branch}", check=False)

    async def revert(self, sha: str, message: str) -> str:
        if not SHA_RE.match(sha):
            raise GitError("invalid sha")
        async with self.lock:
            await self.git("fetch", "-q", "origin", self.branch)
            await self.git("checkout", "-q", "-B", self.branch, f"origin/{self.branch}")
            await self.git("revert", "--no-edit", sha)
            await self.git("commit", "--amend", "-q", "-m", message)
            await self.git("push", "-q", "origin", f"{self.branch}:{self.branch}")
            return (await self.git("rev-parse", "HEAD")).strip()

    async def commit_main(self, files: dict[str, str], message: str) -> str:
        """Direct commit to main. Only used for runbooks/postmortems (not synced by ArgoCD)."""
        for rel in files:
            if not rel.startswith("runbooks/"):
                raise GitError("direct commits are only allowed under runbooks/")
        async with self.lock:
            await self.git("fetch", "-q", "origin", self.branch)
            await self.git("checkout", "-q", "-B", self.branch, f"origin/{self.branch}")
            for rel, content in files.items():
                p = self._safe(rel)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
            await self.git("add", "-A")
            await self.git("commit", "-q", "-m", message)
            await self.git("push", "-q", "origin", f"{self.branch}:{self.branch}")
            return (await self.git("rev-parse", "HEAD")).strip()

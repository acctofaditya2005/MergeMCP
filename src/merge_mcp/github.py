"""A small async GitHub REST client.

Only read endpoints are implemented. The server never needs a write scope, which is
half of the human-in-the-loop guarantee (the other half is that no push tool exists).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from .config import Settings


class GitHubError(Exception):
    """A GitHub API call failed in a way the caller should report to the user."""


class RateLimited(GitHubError):
    def __init__(self, reset_epoch: int | None):
        self.reset_epoch = reset_epoch
        wait = f" Resets in {max(0, reset_epoch - int(time.time()))}s." if reset_epoch else ""
        super().__init__(
            "GitHub rate limit reached." + wait + " Set MERGE_MCP_GITHUB_TOKEN to a read-only "
            "token for a higher limit."
        )


_REPO_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_ISSUE_URL = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)"
)


def parse_repo(value: str) -> str:
    """Accept 'owner/name' or a github.com URL and return 'owner/name'."""
    value = value.strip()
    if m := _REPO_URL.match(value):
        return f"{m.group(1)}/{m.group(2)}"
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        return value
    raise ValueError(f"Not a GitHub repository: {value!r}. Use 'owner/name' or a github.com URL.")


def parse_issue_url(url: str) -> tuple[str, int]:
    """Return ('owner/name', number) from an issue or PR URL."""
    if m := _ISSUE_URL.match(url.strip()):
        return f"{m.group(1)}/{m.group(2)}", int(m.group(3))
    raise ValueError(f"Not a GitHub issue URL: {url!r}")


class GitHubClient:
    """Thin wrapper: auth header, retries on 5xx, and a clear error on rate limiting."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "merge-mcp",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        self._http = httpx.AsyncClient(
            base_url=settings.github_api, headers=headers, timeout=30.0, transport=transport
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self, path: str, params: dict[str, Any] | None = None, accept: str | None = None
    ) -> httpx.Response:
        headers = {"Accept": accept} if accept else None
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._http.get(path, params=params, headers=headers)
            except httpx.TransportError as exc:
                last = exc
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if resp.status_code in (403, 429) and (
                resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers
            ):
                reset = resp.headers.get("x-ratelimit-reset")
                raise RateLimited(int(reset) if reset else None)
            if resp.status_code >= 500:
                last = GitHubError(f"GitHub returned {resp.status_code} for {path}")
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if resp.status_code == 404:
                raise GitHubError(f"Not found on GitHub: {path}")
            if resp.status_code >= 400:
                raise GitHubError(
                    f"GitHub returned {resp.status_code} for {path}: {resp.text[:200]}"
                )
            return resp
        raise GitHubError(f"GitHub request failed after retries: {last}")

    async def get(self, path: str, **params: Any) -> Any:
        return (await self._request(path, params or None)).json()

    async def get_or_none(self, path: str, **params: Any) -> Any | None:
        try:
            return await self.get(path, **params)
        except RateLimited:
            raise
        except GitHubError:
            return None

    # ---- typed helpers ------------------------------------------------------------

    async def search_issues(self, query: str, per_page: int = 20) -> dict[str, Any]:
        return await self.get("/search/issues", q=query, per_page=per_page, sort="updated")

    async def repo(self, repo: str) -> dict[str, Any]:
        return await self.get(f"/repos/{repo}")

    async def issue(self, repo: str, number: int) -> dict[str, Any]:
        return await self.get(f"/repos/{repo}/issues/{number}")

    async def issue_comments(self, repo: str, number: int, per_page: int = 30) -> list[dict]:
        return await self.get(f"/repos/{repo}/issues/{number}/comments", per_page=per_page)

    async def issue_timeline(self, repo: str, number: int) -> list[dict]:
        return await self.get(f"/repos/{repo}/issues/{number}/timeline", per_page=100)

    async def closed_pulls(self, repo: str, per_page: int = 30) -> list[dict]:
        return await self.get(
            f"/repos/{repo}/pulls",
            state="closed",
            sort="updated",
            direction="desc",
            per_page=per_page,
        )

    async def pull(self, repo: str, number: int) -> dict[str, Any]:
        return await self.get(f"/repos/{repo}/pulls/{number}")

    async def pull_diff(self, repo: str, number: int) -> str:
        resp = await self._request(
            f"/repos/{repo}/pulls/{number}", accept="application/vnd.github.diff"
        )
        return resp.text

    async def has_file(self, repo: str, path: str) -> bool:
        return await self.get_or_none(f"/repos/{repo}/contents/{path}") is not None

"""query: search this repo's wiki using an in-memory inverted index.

On first query for a (repo, branch), builds an index from all .md files.
Subsequent queries are served from memory. The index is invalidated by
pull_wiki, push_wiki, and ingest_wiki.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from config import wiki_path as _default_wiki_path
from utils.git import get_current_branch, get_repo_name, wiki_is_initialized
from utils.wiki import check_params, read_remote_url, wiki_not_initialized_response
from utils.wiki_index import WikiIndex


class QueryBuilder:
    """Builds and executes a local wiki query workflow in discrete stages.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._wiki: Path = Path()
        self._repo_name: str = ""
        self._branch: str = ""
        self._topic: str = ""
        self._repo_wiki_path: Path = Path()
        self._search_result: dict = {}
        self._has_results: bool = False
        self._accumulated: dict[str, Any] = {}

    def for_repo_branch(
        self,
        topic: str,
        repo_name: str | None,
        branch: str | None,
        repo_path: str | None,
    ) -> QueryBuilder:
        self._wiki = (Path(repo_path).resolve() / "wiki") if repo_path else _default_wiki_path()
        self._repo_name = repo_name or get_repo_name()
        self._branch = branch or get_current_branch()
        self._topic = topic
        self._repo_wiki_path = self._wiki / self._repo_name / self._branch
        return self

    # ── stage 1: resolve and validate ─────────────────────────────

    def _resolve_and_validate(self) -> tuple[bool, dict | None]:
        if not wiki_is_initialized(self._wiki):
            return False, wiki_not_initialized_response(self._wiki)
        err = check_params(self._repo_name, self._branch)
        if err:
            return False, err
        return True, None

    # ── stage 2: search index ─────────────────────────────────────

    def _search_index(self) -> tuple[bool, dict | None]:
        idx = WikiIndex.get_or_build(self._repo_wiki_path, self._repo_name, self._branch)
        self._search_result = idx.search(self._topic)
        self._has_results = bool(self._search_result["domains"] or self._search_result["other_matches"])
        return True, None

    # ── orchestration ─────────────────────────────────────────────

    def execute(self) -> dict:
        self._accumulated = {}

        stages = [
            self._resolve_and_validate,
            self._search_index,
        ]

        for stage in stages:
            ok, result = stage()
            if not ok:
                assert result is not None, f"stage {stage.__name__} returned (False, None)"
                return result
            if result is not None:
                self._accumulated.update(result)

        return self.to_result()

    def to_result(self) -> dict:
        domains = self._search_result["domains"]
        other_matches = self._search_result["other_matches"]
        response: dict[str, Any] = {
            "topic": self._topic,
            "repo": self._repo_name,
            "domains": domains,
            "other_matches": other_matches,
            "index": self._search_result["index"],
            "summary": self._format_summary(domains, other_matches),
        }
        if self._has_results:
            response["instruction"] = (
                "Use fetch_wiki(path='...') with a page path from the results above to load full content. "
                "Do NOT use ls or Read on the wiki directory — use fetch_wiki instead."
            )
        else:
            response["instruction"] = (
                f"No wiki pages in '{self._repo_name}' mention '{self._topic}'. "
                f"If '{self._topic}' is a different repo name, try query_remote_wiki(topic='{self._topic}', "
                f"repo_name='{self._topic}') to search that repo's wiki. "
                f"To browse all available pages, use init_wiki() to see the full wiki file tree."
            )
        return response

    def _format_summary(self, domains: list[dict], other_matches: list[dict]) -> str:
        parts: list[str] = []
        if domains:
            table = "| # | Domain | Relevance | Top Pages |\n|---|--------|-----------|------|\n"
            for i, d in enumerate(domains, 1):
                pages = d.get("pages", [])
                top_pages = ", ".join(p["path"] for p in pages[:3])
                if len(pages) > 3:
                    top_pages += f" (and {len(pages) - 3} more)"
                table += f"| {i} | {d['name']} | {d.get('relevance', '')} | {top_pages} |\n"
            parts.append(table)
        if other_matches:
            table = "### Other Matches\n\n| # | Page | Score |\n|---|------|-------|\n"
            for i, m in enumerate(other_matches, 1):
                score = m.get("score", "")
                if isinstance(score, float):
                    score = f"{score:.1f}"
                table += f"| {i} | {m['path']} | {score} |\n"
            parts.append(table)
        if not parts:
            return "| # | Domain | Relevance | Top Pages |\n|---|--------|-----------|------|\n| (none) | |\n"
        return "\n".join(parts)


# ── backward-compatible entry point ────────────────────────────────────


def query(topic: str, repo_name: str | None = None, branch: str | None = None, repo_path: str | None = None) -> dict:
    return (
        QueryBuilder()
        .for_repo_branch(topic, repo_name, branch, repo_path)
        .execute()
    )


# ── RemoteQueryBuilder ────────────────────────────────────────────────


class RemoteQueryBuilder:
    """Builds and executes a remote wiki query workflow in discrete stages.

    Each stage returns ``(ok, result)`` where ``result`` is either
    an error dict (on failure) or state to merge into the final response
    (on success).  ``execute()`` short-circuits on the first failure.
    """

    def __init__(self) -> None:
        self._wiki: Path = Path()
        self._repo_name: str = ""
        self._branch: str = ""
        self._topic: str = ""
        self._fetch_error: str | None = None
        self._idx: Any = None
        self._build_error: str | None = None
        self._search_result: dict = {}
        self._accumulated: dict[str, Any] = {}

    def for_repo_branch(
        self,
        topic: str,
        repo_name: str,
        branch: str = "master",
        repo_path: str | None = None,
    ) -> RemoteQueryBuilder:
        self._wiki = (Path(repo_path).resolve() / "wiki") if repo_path else _default_wiki_path()
        self._repo_name = repo_name
        self._branch = branch
        self._topic = topic
        return self

    # ── stage 1: resolve and validate ─────────────────────────────

    def _resolve_and_validate(self) -> tuple[bool, dict | None]:
        if not wiki_is_initialized(self._wiki):
            return False, wiki_not_initialized_response(self._wiki)
        err = check_params(self._repo_name, self._branch)
        if err:
            return False, err
        return True, None

    # ── stage 2: fetch remote ─────────────────────────────────────

    def _fetch_remote(self) -> tuple[bool, dict | None]:
        from utils.git import run_git

        remote_url = read_remote_url()
        if not remote_url:
            return False, {
                "status": "not_found",
                "topic": self._topic,
                "repo": self._repo_name,
                "branch": self._branch,
                "message": (
                    "WIKI_MCP_REMOTE_URL is not configured. Set it in your MCP "
                    "client config to the shared wiki repo URL."
                ),
            }

        fetch_result = run_git(
            ["-c", "protocol.file.allow=always",
             "fetch", remote_url, "+refs/heads/wiki:refs/remotes/origin/wiki", "--depth", "1"],
            cwd=self._wiki, check=False,
        )
        if fetch_result.returncode != 0:
            self._fetch_error = f"git fetch failed: {(fetch_result.stderr or '').strip()}"
        return True, None

    # ── stage 3: search remote index ──────────────────────────────

    def _search_remote_index(self) -> tuple[bool, dict | None]:
        WikiIndex.invalidate(self._repo_name, self._branch)
        self._idx, self._build_error = WikiIndex.get_or_build_remote(self._wiki, self._repo_name, self._branch)
        self._search_result = self._idx.search(self._topic)
        return True, None

    # ── orchestration ─────────────────────────────────────────────

    def execute(self) -> dict:
        self._accumulated = {}

        stages = [
            self._resolve_and_validate,
            self._fetch_remote,
            self._search_remote_index,
        ]

        for stage in stages:
            ok, result = stage()
            if not ok:
                assert result is not None, f"stage {stage.__name__} returned (False, None)"
                return result
            if result is not None:
                self._accumulated.update(result)

        return self.to_result()

    def to_result(self) -> dict:
        if not self._search_result.get("domains") and not self._search_result.get("other_matches") and not self._search_result.get("index"):
            diagnostic = self._build_error or self._fetch_error or (
                f"No wiki content found for '{self._repo_name}/{self._branch}'. "
                f"Ensure wiki content has been pushed from that repo."
            )
            response: dict[str, Any] = {
                "topic": self._topic,
                "repo": self._repo_name,
                "branch": self._branch,
                "status": "not_found",
                "message": diagnostic,
                "instruction": (
                    f"No wiki pages in '{self._repo_name}/{self._branch}' mention '{self._topic}'. "
                    f"If the branch name is wrong, try query_remote_wiki with branch='main' or branch='master'. "
                    f"If the repo name is wrong, try query_remote_wiki with the correct repo_name."
                ),
            }
            if self._fetch_error:
                response["fetch_error"] = self._fetch_error
            if self._build_error:
                response["build_error"] = self._build_error
            return response

        pages: list[dict] = []
        for domain in self._search_result.get("domains", []):
            for page in domain.get("pages", []):
                doc = self._idx._docs.get(page["path"])
                if doc and doc.content:
                    pages.append({
                        "path": page["path"],
                        "domain": domain["name"],
                        "content": doc.content,
                    })
        for match in self._search_result.get("other_matches", []):
            doc = self._idx._docs.get(match["path"])
            if doc and doc.content:
                pages.append({
                    "path": match["path"],
                    "domain": "",
                    "content": doc.content,
                })

        return {
            "topic": self._topic,
            "repo": self._repo_name,
            "branch": self._branch,
            "pages": pages,
            "index": self._search_result.get("index"),
            "summary": self._format_summary(pages),
        }

    @staticmethod
    def _format_summary(pages: list[dict]) -> str:
        table = "| # | Domain | Page |\n|---|--------|------|\n"
        if not pages:
            table += "| (none) | |\n"
        else:
            for i, p in enumerate(pages, 1):
                domain = p.get("domain", "") or "(root)"
                table += f"| {i} | {domain} | {p['path']} |\n"
        return table


# ── backward-compatible entry point ────────────────────────────────────


def query_remote(topic: str, repo_name: str, branch: str = "master",
                 repo_path: str | None = None) -> dict:
    return (
        RemoteQueryBuilder()
        .for_repo_branch(topic, repo_name, branch, repo_path)
        .execute()
    )

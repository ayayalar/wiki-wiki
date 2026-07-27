"""In-memory inverted index for fast wiki search.

Builds once per (repo_name, branch) on first query, then serves all
subsequent queries from memory. Invalidated by pull/push/ingest.
Uses an LRU cache to cap memory — oldest entries are evicted first.
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


@dataclass
class _Document:
    """Metadata for one indexed markdown file."""

    path: str  # relative to repo_wiki_path, posix
    title: str  # first heading or filename
    domain: str  # parent domain name, or ""
    domain_path: str  # domain index path, or ""
    domain_summary: str  # domain summary from top index
    summary: str  # page summary from domain index
    content: str = ""  # raw markdown (populated for remote indexes)
    heading_terms: set[str] = field(default_factory=set)
    body_terms: set[str] = field(default_factory=set)
    path_terms: set[str] = field(default_factory=set)


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens, filtering short ones."""
    return [t.lower() for t in re.split(r"\W+", text) if len(t) > 2]


def _parse_index(text: str) -> list[dict]:
    """Extract linked entries from an index.md file.

    Matches heading links (## [Name](path) — summary) and
    list links (- [Name](path) — summary).
    """
    pattern = re.compile(
        r"(?:^[#\-*]+\s*)"
        r"\[([^\]]+)\]"
        r"\(([^)]+)\)"
        r"(?:\s*[—\-–:]\s*(.+))?",
        re.MULTILINE,
    )
    entries = []
    for m in pattern.finditer(text):
        entries.append(
            {
                "name": m.group(1).strip(),
                "path": m.group(2).strip(),
                "summary": (m.group(3) or "").strip(),
            }
        )
    return entries


class WikiIndex:
    """Per-(repo, branch) inverted index over wiki markdown files."""

    # LRU cache: (kind, wiki_dir, repo_name, branch) → WikiIndex instance.
    # The wiki_dir is part of the key so two clones of the same repo/branch
    # get distinct indexes instead of cross-contaminating one shared entry.
    # OrderedDict maintains insertion/access order; oldest evicted first.
    # _lock guards all cache mutation — FastMCP dispatches tool calls on
    # concurrent threads, and OrderedDict move_to_end/popitem/insert are not
    # thread-safe against each other.
    _cache: ClassVar[OrderedDict[tuple, WikiIndex]] = OrderedDict()
    _max_cache_size: ClassVar[int] = 32
    _lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(self) -> None:
        # term → set of doc paths that contain it
        self._inverted: dict[str, set[str]] = defaultdict(set)
        # path → Document
        self._docs: dict[str, _Document] = {}
        # domain_name → {name, path, summary, page_paths}
        self._domains: dict[str, dict] = {}
        # raw top-level index text (for backward compat in response)
        self._index_text: str = ""

    # ── build ──────────────────────────────────────────────────────────

    def _index_files(self, files: dict[str, str], store_content: bool = False) -> None:
        """Build the inverted index from a {rel_path: content} mapping.

        Shared by both build() (filesystem) and build_from_git() (git objects).
        When store_content=True, raw markdown is kept in each _Document for
        inline results (used by query_remote_wiki).
        """
        self._inverted.clear()
        self._docs.clear()
        self._domains.clear()

        # 1. Parse top-level index → domains
        self._index_text = files.get("index.md", "")
        domain_entries = _parse_index(self._index_text)

        # Build domain lookup: domain_dir → domain info
        domain_by_dir: dict[str, dict] = {}
        for entry in domain_entries:
            domain_dir = entry["path"].replace("index.md", "").rstrip("/")
            domain_info = {
                "name": entry["name"],
                "path": entry["path"],
                "summary": entry["summary"],
                "page_paths": set(),
            }
            self._domains[entry["name"]] = domain_info
            domain_by_dir[domain_dir] = domain_info

            # Parse domain index for page entries
            domain_index_text = files.get(entry["path"], "")
            for page in _parse_index(domain_index_text):
                page_rel = (Path(entry["path"]).parent / page["path"]).as_posix()
                domain_info["page_paths"].add(page_rel)

        # 2. Index all .md files
        for rel in sorted(files):
            if not rel.endswith(".md"):
                continue
            content = files[rel]

            # Determine domain membership
            domain_name = ""
            domain_path = ""
            domain_summary = ""
            page_summary = ""
            for ddir, dinfo in domain_by_dir.items():
                if rel.startswith(ddir + "/") or rel == ddir:
                    domain_name = dinfo["name"]
                    domain_path = dinfo["path"]
                    domain_summary = dinfo["summary"]
                    domain_index_text = files.get(dinfo["path"], "")
                    for pentry in _parse_index(domain_index_text):
                        if (Path(dinfo["path"]).parent / pentry["path"]).as_posix() == rel:
                            page_summary = pentry["summary"]
                    break

            # Extract title from first heading
            title = Path(rel).stem
            for line in content.splitlines():
                if line.startswith("#"):
                    title = line.lstrip("#").strip()
                    break

            # Tokenize
            heading_terms: set[str] = set()
            body_tokens: list[str] = []
            for line in content.splitlines():
                tokens = _tokenize(line)
                if line.startswith("#"):
                    heading_terms.update(tokens)
                body_tokens.extend(tokens)

            path_terms = set(_tokenize(rel.replace("/", " ").replace("-", " ").replace("_", " ")))

            doc = _Document(
                path=rel,
                title=title,
                domain=domain_name,
                domain_path=domain_path,
                domain_summary=domain_summary,
                summary=page_summary,
                content=content if store_content else "",
                heading_terms=heading_terms,
                body_terms=set(body_tokens),
                path_terms=path_terms,
            )
            self._docs[rel] = doc

            # Populate inverted index
            all_terms = heading_terms | doc.body_terms | path_terms
            for term in all_terms:
                self._inverted[term].add(rel)

    def build(self, repo_wiki_path: Path) -> None:
        """Walk all .md files on disk and build the inverted index."""
        if not repo_wiki_path.exists():
            self._inverted.clear()
            self._docs.clear()
            self._domains.clear()
            self._index_text = ""
            return

        files: dict[str, str] = {}
        for md_file in sorted(repo_wiki_path.rglob("*.md")):
            rel = md_file.relative_to(repo_wiki_path).as_posix()
            try:
                files[rel] = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        self._index_files(files)

    def build_from_git(
        self, wiki_dir: Path, repo_name: str, branch: str, ref: str = "HEAD"
    ) -> tuple[bool, str | None]:
        """Build the index from git objects (no checkout needed).

        Uses git ls-tree to list files and git cat-file --batch to read
        all .md contents in a single subprocess call.  On Windows, uses
        temp files for I/O to avoid pipe-handle-inheritance hangs.

        Returns (success, error_message).  On failure, the index is left
        empty and the error message describes what went wrong so the
        caller can surface useful diagnostics.
        """
        import os
        import tempfile

        prefix = f"{repo_name}/{branch}"

        # List all files under repo_name/branch/
        try:
            result = subprocess.run(
                ["git", "ls-tree", "-r", "--name-only", f"{ref}:{prefix}"],
                cwd=str(wiki_dir),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=_CREATIONFLAGS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._index_files({})
            return False, f"git ls-tree timed out after 30s for {ref}:{prefix}"
        except OSError as exc:
            self._index_files({})
            return False, f"git ls-tree failed: {exc}"

        if result.returncode != 0:
            self._index_files({})
            stderr = (result.stderr or "").strip()
            # Provide actionable diagnostics based on common failure modes
            if "fatal: not a git repository" in (result.stderr or ""):
                return False, f"wiki_dir {wiki_dir} is not a git repository. Run pull_wiki first."
            if (
                "not found" in (result.stderr or "").lower()
                or "does not exist" in (result.stderr or "").lower()
            ):
                return False, (
                    f"Path '{prefix}' not found at ref '{ref}'. "
                    f"This means the wiki remote has no content for repo '{repo_name}' branch '{branch}'. "
                    f"Ensure wiki content has been pushed (push_wiki) from that repo."
                )
            if stderr:
                return False, f"git ls-tree failed for {ref}:{prefix}: {stderr}"
            return False, f"git ls-tree failed for {ref}:{prefix} (rc={result.returncode})"

        md_paths = [
            p.strip() for p in result.stdout.strip().split("\n") if p.strip().endswith(".md")
        ]

        if not md_paths:
            # ls-tree succeeded but returned no .md files — could be empty
            # wiki folder or a folder with only non-markdown files.
            non_md = [
                p.strip()
                for p in result.stdout.strip().split("\n")
                if p.strip() and not p.strip().endswith(".md")
            ]
            self._index_files({})
            if non_md:
                return False, (
                    f"Path '{prefix}' exists at ref '{ref}' but contains no .md files "
                    f"(found {len(non_md)} non-markdown file(s))."
                )
            return False, (
                f"Path '{prefix}' exists at ref '{ref}' but is empty (no files). "
                f"Wiki content for repo '{repo_name}' branch '{branch}' has not been pushed yet."
            )

        # Batch-read all .md files via cat-file --batch using temp files
        tmpdir = tempfile.mkdtemp(prefix="wiki_catfile_")
        in_path = os.path.join(tmpdir, "in")
        out_path = os.path.join(tmpdir, "out")
        try:
            # Write refs to input file
            with open(in_path, "w", encoding="utf-8") as f:
                f.writelines(f"{ref}:{prefix}/{p}\n" for p in md_paths)

            with open(in_path, "r", encoding="utf-8") as in_f, open(out_path, "wb") as out_f:
                proc = subprocess.Popen(
                    ["git", "cat-file", "--batch"],
                    cwd=str(wiki_dir),
                    stdin=in_f,
                    stdout=out_f,
                    stderr=subprocess.DEVNULL,
                    creationflags=_CREATIONFLAGS,
                )
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                    self._index_files({})
                    return False, "git cat-file --batch timed out after 30s"

            # Parse binary output: each entry is "<sha> blob <size>\n<content>\n"
            with open(out_path, "rb") as f:
                data = f.read()
        finally:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)

        files: dict[str, str] = {}
        offset = 0
        for rel_path in md_paths:
            try:
                nl = data.index(b"\n", offset)
                header = data[offset:nl].decode("utf-8", errors="replace")
                offset = nl + 1
                if "missing" in header:
                    continue
                size = int(header.split()[2])
                content = data[offset : offset + size].decode("utf-8", errors="replace")
                offset += size + 1  # skip trailing newline
                files[rel_path] = content
            except (ValueError, IndexError):
                break

        self._index_files(files, store_content=True)
        return True, None

    # ── search ─────────────────────────────────────────────────────────

    def search(self, topic: str) -> dict:
        """Search the index and return structured results.

        Returns the same shape as the original query() function:
        {domains: [...], other_matches: [...], index: str}
        """
        terms = _tokenize(topic)
        if not terms:
            return {
                "domains": [],
                "other_matches": [],
                "index": self._index_text,
            }

        # Gather candidate docs and score them
        candidates: set[str] = set()
        for term in terms:
            candidates |= self._inverted.get(term, set())

        doc_scores: dict[str, float] = {}
        for path in candidates:
            doc = self._docs[path]
            score = 0.0
            for term in terms:
                if term in doc.body_terms:
                    score += 1
                if term in doc.heading_terms:
                    score += 3
                if term in doc.path_terms:
                    score += 5
                # Boost from summary/domain name
                if term in _tokenize(f"{doc.summary} {doc.domain} {doc.domain_summary}"):
                    score += 2
            doc_scores[path] = score

        # Group by domain
        domain_pages: dict[str, list[dict]] = defaultdict(list)
        other_matches: list[dict] = []
        indexed_paths: set[str] = set()

        # Collect all pages known to any domain index
        for dinfo in self._domains.values():
            indexed_paths |= dinfo["page_paths"]

        for path, score in sorted(doc_scores.items(), key=lambda x: -x[1]):
            if score <= 0:
                continue
            doc = self._docs[path]
            if doc.domain and path in indexed_paths:
                domain_pages[doc.domain].append(
                    {
                        "path": path,
                        "summary": doc.summary,
                        "score": score,
                    }
                )
            elif path != "index.md" and not path.endswith("/index.md"):
                other_matches.append({"path": path, "score": score})

        # Build domain results
        domains_result: list[dict] = []
        for dname, dinfo in self._domains.items():
            pages = domain_pages.get(dname, [])
            # Score the domain itself
            domain_score = sum(
                1 for t in terms if t in _tokenize(f"{dinfo['name']} {dinfo['summary']}")
            )
            if domain_score > 0 or pages:
                domains_result.append(
                    {
                        "name": dinfo["name"],
                        "path": dinfo["path"],
                        "summary": dinfo["summary"],
                        "relevance": domain_score,
                        "pages": sorted(pages, key=lambda p: -p["score"])[:5],
                    }
                )

        domains_result.sort(
            key=lambda d: d["relevance"] + sum(p["score"] for p in d["pages"]),
            reverse=True,
        )

        return {
            "domains": domains_result,
            "other_matches": other_matches[:5],
            "index": self._index_text,
        }

    # ── cache management ───────────────────────────────────────────────

    @classmethod
    def _put(cls, key: tuple, idx: WikiIndex) -> None:
        """Insert into LRU cache, evicting oldest if over capacity.

        Caller must hold ``cls._lock``.
        """
        cls._cache[key] = idx
        while len(cls._cache) > cls._max_cache_size:
            cls._cache.popitem(last=False)

    @classmethod
    def get_or_build(cls, repo_wiki_path: Path, repo_name: str, branch: str) -> WikiIndex:
        """Return cached local index or build from filesystem (LRU eviction)."""
        key = ("local", str(repo_wiki_path), repo_name, branch)
        with cls._lock:
            idx = cls._cache.get(key)
            if idx is not None:
                cls._cache.move_to_end(key)
                return idx
        # Build outside the lock — filesystem walk is slow and must not block
        # concurrent queries for other repos. A rare duplicate build is fine;
        # we discard it below if another thread won the race.
        idx = cls()
        idx.build(repo_wiki_path)
        with cls._lock:
            existing = cls._cache.get(key)
            if existing is not None:
                cls._cache.move_to_end(key)
                return existing
            cls._put(key, idx)
        return idx

    @classmethod
    def get_or_build_remote(
        cls, wiki_dir: Path, repo_name: str, branch: str
    ) -> tuple[WikiIndex, str | None]:
        """Return cached remote index or build from git objects (LRU eviction).

        Returns (index, error_message).  On failure, returns an empty index
        with a diagnostic error message.
        """
        key = ("remote", str(wiki_dir), repo_name, branch)
        with cls._lock:
            idx = cls._cache.get(key)
            if idx is not None:
                cls._cache.move_to_end(key)
                return idx, None
        idx = cls()
        success, err = idx.build_from_git(wiki_dir, repo_name, branch, ref="origin/wiki")
        if not success:
            return idx, err
        with cls._lock:
            existing = cls._cache.get(key)
            if existing is not None:
                cls._cache.move_to_end(key)
                return existing, None
            cls._put(key, idx)
        return idx, None

    @classmethod
    def invalidate(cls, repo_name: str, branch: str) -> None:
        """Remove cached indexes for this repo/branch across all wiki paths.

        Matches on (repo_name, branch) regardless of wiki_dir so a push/pull
        in one clone also drops any stale index held for another clone of the
        same repo/branch.
        """
        with cls._lock:
            stale = [k for k in cls._cache if k[-2] == repo_name and k[-1] == branch]
            for k in stale:
                cls._cache.pop(k, None)

    @classmethod
    def invalidate_all(cls) -> None:
        """Clear all cached indexes."""
        with cls._lock:
            cls._cache.clear()

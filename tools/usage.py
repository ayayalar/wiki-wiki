"""wiki_usage tool and in-memory estimated token usage tracker."""

from __future__ import annotations

import json
import math
import threading
from typing import Any

_EXCLUDED_TOOLS = {"wiki_usage"}
_USAGE_LOCK = threading.Lock()
_SESSION_INPUT_TOKENS = 0
_SESSION_OUTPUT_TOKENS = 0
_SESSION_PER_TOOL: dict[str, dict[str, int]] = {}


def _estimate_tokens(payload: Any) -> int:
    """Estimate token count from serialized payload size."""
    if payload is None:
        return 0
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    if not text:
        return 0
    return math.ceil(len(text) / 4.0)


def record_tool_input(tool_name: str, payload: Any) -> int:
    """Record estimated input tokens for a tool call."""
    if tool_name in _EXCLUDED_TOOLS:
        return 0
    estimated = _estimate_tokens(payload)
    with _USAGE_LOCK:
        global _SESSION_INPUT_TOKENS
        _SESSION_INPUT_TOKENS += estimated
        per_tool = _SESSION_PER_TOOL.setdefault(tool_name, {"input": 0, "output": 0})
        per_tool["input"] += estimated
    return estimated


def record_tool_output(tool_name: str, payload: Any) -> int:
    """Record estimated output tokens for a tool call."""
    if tool_name in _EXCLUDED_TOOLS:
        return 0
    estimated = _estimate_tokens(payload)
    with _USAGE_LOCK:
        global _SESSION_OUTPUT_TOKENS
        _SESSION_OUTPUT_TOKENS += estimated
        per_tool = _SESSION_PER_TOOL.setdefault(tool_name, {"input": 0, "output": 0})
        per_tool["output"] += estimated
    return estimated


def get_session_usage_snapshot() -> dict[str, Any]:
    """Return a consistent snapshot of session usage totals and per-tool details."""
    with _USAGE_LOCK:
        input_tokens = _SESSION_INPUT_TOKENS
        output_tokens = _SESSION_OUTPUT_TOKENS
        per_tool_copy = {
            tool: {"input": values["input"], "output": values["output"]}
            for tool, values in _SESSION_PER_TOOL.items()
        }

    per_tool_rows = []
    for tool, values in per_tool_copy.items():
        input_value = values["input"]
        output_value = values["output"]
        total_value = input_value + output_value
        per_tool_rows.append(
            {
                "tool": tool,
                "input_tokens_estimated": input_value,
                "output_tokens_estimated": output_value,
                "total_tokens_estimated": total_value,
            }
        )

    per_tool_rows.sort(key=lambda row: (-row["total_tokens_estimated"], row["tool"]))
    return {
        "session_input_tokens_estimated": input_tokens,
        "session_output_tokens_estimated": output_tokens,
        "session_total_tokens_estimated": input_tokens + output_tokens,
        "per_tool": per_tool_rows,
    }


def reset_session_usage_for_tests() -> None:
    """Reset session usage counters for test isolation."""
    with _USAGE_LOCK:
        global _SESSION_INPUT_TOKENS, _SESSION_OUTPUT_TOKENS
        _SESSION_INPUT_TOKENS = 0
        _SESSION_OUTPUT_TOKENS = 0
        _SESSION_PER_TOOL.clear()


class UsageBuilder:
    """Builds and executes wiki usage workflow in discrete stages."""

    def __init__(self) -> None:
        self._repo_name = ""
        self._branch = ""
        self._wiki_path = ""
        self._snapshot: dict[str, Any] = {}
        self._accumulated: dict[str, Any] = {}

    def for_repo_branch(self, repo_name: str, branch: str, repo_path: str) -> UsageBuilder:
        self._repo_name = repo_name
        self._branch = branch
        self._wiki_path = f"{repo_path}/wiki"
        return self

    def _collect_snapshot(self) -> tuple[bool, dict | None]:
        self._snapshot = get_session_usage_snapshot()
        return True, None

    def execute(self) -> dict:
        self._accumulated = {}
        stages = [self._collect_snapshot]
        for stage in stages:
            ok, result = stage()
            if not ok:
                assert result is not None, f"stage {stage.__name__} returned (False, None)"
                return result
            if result is not None:
                self._accumulated.update(result)
        return self.to_result()

    def to_result(self) -> dict:
        response: dict[str, Any] = {
            "repo": self._repo_name,
            "branch": self._branch,
            "wiki_path": self._wiki_path,
            **self._snapshot,
            "summary": self._format_summary(),
            "instruction": "Display the markdown table in the 'summary' field to the user.",
        }
        return response

    def _format_summary(self) -> str:
        top_table = "| Metric | Tokens (Estimated) |\n|--------|---------------------|\n"
        top_table += (
            f"| Session Input | {self._snapshot.get('session_input_tokens_estimated', 0)} |\n"
            f"| Session Output | {self._snapshot.get('session_output_tokens_estimated', 0)} |\n"
            f"| Session Total | {self._snapshot.get('session_total_tokens_estimated', 0)} |\n"
        )

        per_tool = self._snapshot.get("per_tool", [])
        per_tool_table = "\n| Tool | Input | Output | Total |\n|------|-------|--------|-------|\n"
        if not per_tool:
            per_tool_table += "| (none) | 0 | 0 | 0 |\n"
        else:
            for row in per_tool:
                per_tool_table += (
                    f"| {row['tool']} | {row['input_tokens_estimated']} | "
                    f"{row['output_tokens_estimated']} | {row['total_tokens_estimated']} |\n"
                )
        return top_table + per_tool_table


def usage(repo_name: str, branch: str, repo_path: str) -> dict:
    return UsageBuilder().for_repo_branch(repo_name, branch, repo_path).execute()

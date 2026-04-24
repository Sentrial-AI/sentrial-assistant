"""
Shared contract for MCP-style capability modules.

Each capability module under sentrial/mcps/<name>/server.py exposes a `register()`
function that plugs tools into the agent's registry and (where applicable) registers
executors with the task runner.

This layer is deliberately simpler than the MCP wire protocol — we're running all
capabilities in-process with the daemon. When we want to expose these to Claude Code
or other clients, we can wrap this interface with the `mcp` library stdio/HTTP server.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from sentrial.core.confirmation import Tier, register as register_tier
from sentrial.core.task_runner import Executor, TaskRunner


ToolImpl = Callable[[dict], Awaitable[Any]]


@dataclass
class Tool:
    """An Anthropic-compatible tool definition plus its Python implementation."""
    name: str
    description: str
    input_schema: dict
    impl: ToolImpl
    tier: Tier | None = None  # explicit tier override; None → use heuristics
    group: str = ""           # MCP group ('notion', 'gmail', etc.) — set by
                              # the module's register() wrapper (see Registry.set_group).

    def as_anthropic(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class Registry:
    """Collects tools + executors from all loaded MCPs for the agent to consume."""
    tools: list[dict] = field(default_factory=list)           # Anthropic tool defs
    impls: dict[str, ToolImpl] = field(default_factory=dict)   # name → callable
    _groups: dict[str, str] = field(default_factory=dict)      # tool name → group
    _status: dict[str, str] = field(default_factory=dict)      # group → runtime status
                                                              # ('active'|'pending_auth'|'disabled')

    # Context for the currently-registering MCP; set by add_group() and
    # consumed by add() so MCPs don't need to pass the group each call.
    _current_group: str = ""

    def add_group(self, name: str, status: str = "active") -> "Registry":
        """
        Start registering tools under `name`. Returns self so a module can do:
            registry.add_group('gmail').add(tool1).add(tool2)
        `status` is the starting status; individual MCPs can also call
        set_status('group', 'pending_auth') later if their connection state
        changes during the process lifetime.
        """
        self._current_group = name
        self._status.setdefault(name, status)
        return self

    def set_status(self, group: str, status: str) -> None:
        self._status[group] = status

    def add(self, tool: Tool) -> "Registry":
        group = tool.group or self._current_group or "ungrouped"
        self.tools.append(tool.as_anthropic())
        self.impls[tool.name] = tool.impl
        self._groups[tool.name] = group
        self._status.setdefault(group, "active")
        if tool.tier is not None:
            register_tier(tool.name, tool.tier)
        return self

    def groups(self) -> list[dict]:
        """Return [{name, status, tools}] for each registered group, sorted."""
        counts: dict[str, int] = {}
        for g in self._groups.values():
            counts[g] = counts.get(g, 0) + 1
        out = []
        # Include groups with zero tools too (e.g. auth-gated, not yet ready).
        for g, status in self._status.items():
            out.append({"name": g, "status": status, "tools": counts.get(g, 0)})
        # Also include groups that got tools added without a prior add_group() call.
        for g, n in counts.items():
            if g not in self._status:
                out.append({"name": g, "status": "active", "tools": n})
        out.sort(key=lambda d: d["name"])
        return out


class MCPModule(Protocol):
    """An MCP-style capability module."""
    def register(self, registry: Registry, task_runner: TaskRunner) -> None: ...


def register_executor(task_runner: TaskRunner, kind: str, fn: Executor) -> None:
    """Convenience wrapper."""
    task_runner.register_executor(kind, fn)

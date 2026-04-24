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

    def add(self, tool: Tool) -> None:
        self.tools.append(tool.as_anthropic())
        self.impls[tool.name] = tool.impl
        if tool.tier is not None:
            register_tier(tool.name, tool.tier)


class MCPModule(Protocol):
    """An MCP-style capability module."""
    def register(self, registry: Registry, task_runner: TaskRunner) -> None: ...


def register_executor(task_runner: TaskRunner, kind: str, fn: Executor) -> None:
    """Convenience wrapper."""
    task_runner.register_executor(kind, fn)

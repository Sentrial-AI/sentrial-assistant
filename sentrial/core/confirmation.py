"""
Tier-based action gating. Every tool call is classified before execution.

  Tier 0 READ         — no gate
  Tier 1 DRAFT        — no gate (but logged)
  Tier 2 SEND         — explicit confirmation required
  Tier 3 IRREVERSIBLE — strong confirmation (biometric / typed string)

Tools can register explicit tiers via EXPLICIT_TIERS. Unregistered tools are classified
by name-prefix heuristics; unknown tools default to SEND (err toward confirming).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Awaitable, Callable


class Tier(IntEnum):
    READ = 0
    DRAFT = 1
    SEND = 2
    IRREVERSIBLE = 3


# Prefix-based heuristics. First match wins.
TIER_HINTS: list[tuple[str, Tier]] = [
    ("list_", Tier.READ),
    ("get_", Tier.READ),
    ("search_", Tier.READ),
    ("read_", Tier.READ),
    ("summarize_", Tier.READ),
    ("fetch_", Tier.READ),
    ("find_", Tier.READ),
    ("draft_", Tier.DRAFT),
    ("save_draft_", Tier.DRAFT),
    ("build_", Tier.DRAFT),
    ("compose_", Tier.DRAFT),
    ("send_", Tier.SEND),
    ("post_", Tier.SEND),
    ("delete_", Tier.SEND),
    ("create_event", Tier.SEND),
    ("create_reminder", Tier.SEND),
    ("update_", Tier.SEND),
]

# Explicit overrides — dangerous tools that must not be auto-classified.
EXPLICIT_TIERS: dict[str, Tier] = {
    "wire_transfer": Tier.IRREVERSIBLE,
    "mass_send": Tier.IRREVERSIBLE,
    "shell_exec_rm": Tier.IRREVERSIBLE,
    "shell_exec_rm_rf": Tier.IRREVERSIBLE,
    "dns_update": Tier.IRREVERSIBLE,
    "start_background_job": Tier.DRAFT,  # creating a job is just a draft — approval is separate
    "approve_job": Tier.SEND,            # approving dispatches a sub-agent, side effects possible
}


ConfirmCb = Callable[[str, dict, Tier], Awaitable[bool]]


@dataclass
class GateResult:
    allowed: bool
    tier: Tier
    reason: str = ""


def classify(tool_name: str) -> Tier:
    if tool_name in EXPLICIT_TIERS:
        return EXPLICIT_TIERS[tool_name]
    for prefix, tier in TIER_HINTS:
        if tool_name.startswith(prefix):
            return tier
    return Tier.SEND  # unknown → err toward confirming


async def gate(
    tool_name: str,
    args: dict,
    confirm: ConfirmCb,
    strong_confirm: ConfirmCb,
) -> GateResult:
    """
    Invoke the appropriate confirmation callback based on tier.

    `confirm` is used for Tier.SEND; `strong_confirm` for Tier.IRREVERSIBLE.
    Both callbacks return True=allow, False=deny.
    """
    tier = classify(tool_name)
    if tier <= Tier.DRAFT:
        return GateResult(True, tier)
    if tier == Tier.SEND:
        ok = await confirm(tool_name, args, tier)
        return GateResult(ok, tier, "" if ok else "user declined")
    ok = await strong_confirm(tool_name, args, tier)
    return GateResult(ok, tier, "" if ok else "strong-confirm declined")


def register(tool_name: str, tier: Tier) -> None:
    """Register an explicit tier for a tool. Call at MCP load time."""
    EXPLICIT_TIERS[tool_name] = tier

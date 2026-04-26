"""
The Sentrial brain. A single-turn tool-use loop on Claude Opus.

Why a thin custom loop instead of claude-agent-sdk directly:
  - We need tight control over the confirmation gate (runs BEFORE every tool call).
  - We want audit entries at exactly the right moments (create, gate, execute, result).
  - The tool registry is a plain dict keyed by name — MCPs register into it at boot.
  - Easy to swap in claude-agent-sdk later; this layer is intentionally small.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from anthropic import AsyncAnthropic

from sentrial.core import audit, memory, secrets
from sentrial.core.confirmation import GateResult, Tier, gate
from sentrial.core.task_runner import TaskRunner

log = logging.getLogger(__name__)

SYSTEM_PROMPT_PATH = Path(__file__).parent.parent / "config" / "system_prompt.md"
DEFAULT_MODEL = "claude-opus-4-6"
# Voice turns route to Haiku 4.5 — dramatically lower time-to-first-token.
# The pre-turn retrieval preamble injects schedule / calendar / profile /
# lessons so Haiku has enough context to answer most voice questions
# without reaching for tools.
VOICE_MODEL = "claude-haiku-4-5-20251001"
VOICE_MAX_TOKENS = 512          # voice replies should be short; caps latency
DEFAULT_MAX_TOKENS = 4096
MAX_TOOL_ITERATIONS = 12        # guardrail against runaway loops


ConfirmCb = Callable[[str, dict, Tier], Awaitable[bool]]
ToolImpl = Callable[[dict], Awaitable[Any]]


class Agent:
    def __init__(
        self,
        tools: list[dict[str, Any]],
        tool_impls: dict[str, ToolImpl],
        task_runner: TaskRunner,
        confirm_cb: ConfirmCb,
        strong_confirm_cb: ConfirmCb,
        model: str = DEFAULT_MODEL,
    ):
        self.client = AsyncAnthropic(api_key=secrets.require("anthropic_api_key"))
        self.tools = tools
        self.tool_impls = tool_impls
        self.task_runner = task_runner
        self.confirm_cb = confirm_cb
        self.strong_confirm_cb = strong_confirm_cb
        self.model = model
        self.system_prompt = SYSTEM_PROMPT_PATH.read_text()

    # ----- public API -----

    async def turn(
        self,
        user_message: str,
        channel: str,
        conversation_id: str | None = None,
    ) -> str:
        """Run one conversational turn. Returns the assistant's final text reply."""
        conv_id = conversation_id or uuid.uuid4().hex[:12]

        # Pre-turn retrieval — learned user profile, KG entity cards for names
        # mentioned in the message, matching task playbook, top relevant
        # lessons. Each component is a no-op on a fresh install.
        retrieved = self._retrieve_context(user_message)
        legacy = self._build_memory_preamble()
        composed = (retrieved + legacy + user_message) if (retrieved or legacy) else user_message

        # Pull the prior assistant reply for distillation's correction-detection.
        prev_assistant = self._prev_assistant_text(conv_id)

        # Load prior turns of this conversation so the LLM has continuity. Without
        # this, every voice/chat turn started from a blank slate — the model
        # had no idea what was just said. Capped at the last 20 turns to keep
        # context cost bounded; voice replies are short so this is plenty.
        prior_turns = self._load_prior_turns(conv_id, limit=20)
        messages: list[dict] = prior_turns + [{"role": "user", "content": composed}]
        memory.log_turn(conv_id, channel, {"role": "user", "content": user_message})

        # Per-channel model + token budget. Voice routes to Haiku 4.5 with
        # a tight max_tokens so replies stay short and the time-to-last-token
        # is ~1-3s vs. Opus's 8-30s. Pre-turn retrieval already injects
        # schedule/calendar/profile/lessons so Haiku has the context it
        # needs for most voice questions.
        is_voice = channel == "voice"
        model = VOICE_MODEL if is_voice else self.model
        max_tokens = VOICE_MAX_TOKENS if is_voice else DEFAULT_MAX_TOKENS
        system = self.system_prompt
        if is_voice:
            system = (
                system
                + "\n\n[voice-turn mode] Your reply will be spoken aloud. "
                "Keep it to 1-2 short sentences. No bullet lists. No markdown "
                "formatting. Be direct and conversational."
            )

        # Cache the system prompt (which is large and stable across turns) so
        # back-to-back voice turns within ~5 minutes only pay full input-token
        # cost on the first call. Cached reads are both cheaper AND lower
        # latency. The block-form system arg is required to attach
        # cache_control; passing a bare string disables caching.
        cached_system = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]

        for iteration in range(MAX_TOOL_ITERATIONS):
            resp = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=cached_system,
                tools=self.tools,
                messages=messages,
            )

            # Capture assistant content block list for the running transcript
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "end_turn":
                text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
                memory.log_turn(conv_id, channel, {"role": "assistant", "content": text})
                # Fire-and-forget distillation — doesn't block the reply.
                self._schedule_distill(
                    user_message=user_message, assistant_reply=text,
                    prev_assistant=prev_assistant, conversation_id=conv_id,
                )
                return text

            if resp.stop_reason == "tool_use":
                tool_results = await self._execute_tool_calls(resp.content)
                messages.append({"role": "user", "content": tool_results})
                continue

            log.warning(f"unexpected stop_reason={resp.stop_reason}; bailing")
            return f"[sentrial] unexpected stop_reason: {resp.stop_reason}"

        log.warning(f"hit MAX_TOOL_ITERATIONS={MAX_TOOL_ITERATIONS}")
        return "[sentrial] hit tool-iteration limit — stopping"

    def _retrieve_context(self, user_message: str) -> str:
        """Build the pre-turn context block. Fails closed — any error in the
        evolution layer returns empty rather than blocking the turn."""
        try:
            from sentrial.evolution import retrieval
            ctx = retrieval.build(user_message)
            return ctx.as_preamble()
        except Exception as e:  # noqa: BLE001
            log.warning("retrieval failed (ignored): %s", e)
            return ""

    def _load_prior_turns(self, conv_id: str, limit: int = 20) -> list[dict]:
        """Return the last `limit` turns of the conversation as Anthropic-compatible
        message dicts. Skips malformed entries. Tool-use blocks are dropped — only
        the rendered text survives, which is fine for continuity."""
        try:
            conv = memory.get_conversation(conv_id)
        except Exception:  # noqa: BLE001
            return []
        if not conv:
            return []
        out: list[dict] = []
        for t in (conv.get("turns") or [])[-limit:]:
            role = t.get("role")
            if role not in ("user", "assistant"):
                continue
            c = t.get("content")
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                text = "".join(b.get("text", "") for b in c if isinstance(b, dict))
            else:
                continue
            if not text:
                continue
            out.append({"role": role, "content": text})
        return out

    def _prev_assistant_text(self, conv_id: str) -> str | None:
        """Find the last assistant turn in this conversation, if any."""
        try:
            conv = memory.get_conversation(conv_id)
        except Exception:  # noqa: BLE001
            return None
        if not conv:
            return None
        for t in reversed(conv.get("turns") or []):
            if t.get("role") == "assistant":
                c = t.get("content")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    return "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
        return None

    def _schedule_distill(
        self, user_message: str, assistant_reply: str,
        prev_assistant: str | None, conversation_id: str,
    ) -> None:
        try:
            from sentrial.evolution import distill
            distill.fire_and_forget(
                user_message=user_message,
                assistant_reply=assistant_reply,
                prev_assistant=prev_assistant,
                conversation_id=conversation_id,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("distill scheduling failed (ignored): %s", e)

    # ----- internals -----

    def _build_memory_preamble(self) -> str:
        user_facts = memory.recall_scope("user")
        if not user_facts:
            return ""
        return (
            "[memory:user]\n"
            + json.dumps(user_facts, indent=2, default=str)
            + "\n\n"
        )

    async def _execute_tool_calls(self, content_blocks: list) -> list[dict]:
        results = []
        for block in content_blocks:
            if getattr(block, "type", None) != "tool_use":
                continue
            out = await self._invoke_tool(block.name, dict(block.input or {}))
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": out if isinstance(out, str) else json.dumps(out, default=str),
            })
        return results

    async def _invoke_tool(self, name: str, args: dict) -> Any:
        gate_result: GateResult = await gate(
            name, args, self.confirm_cb, self.strong_confirm_cb
        )
        if not gate_result.allowed:
            audit.log(
                "user",
                f"tool_denied:{name}",
                int(gate_result.tier),
                args=args,
                status="denied",
                result=gate_result.reason,
            )
            return {"error": f"user declined '{name}' ({gate_result.reason})"}

        impl = self.tool_impls.get(name)
        if impl is None:
            audit.log("sentrial", f"tool_missing:{name}", int(gate_result.tier),
                      args=args, status="error")
            return {"error": f"unknown tool: {name}"}

        try:
            if asyncio.iscoroutinefunction(impl):
                out = await impl(args)
            else:
                out = impl(args)
            audit.log(
                "sentrial",
                f"tool:{name}",
                int(gate_result.tier),
                args=args,
                result=str(out)[:400],
            )
            return out
        except Exception as e:  # noqa: BLE001
            audit.log(
                "sentrial",
                f"tool:{name}",
                int(gate_result.tier),
                args=args,
                result=str(e),
                status="error",
            )
            return {"error": str(e)}

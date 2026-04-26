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
# Hard cap on voice reply length. Was 512, lowered to 220 because Haiku will
# happily produce 30s of audio for an open-ended question ("what are you
# capable of") if the prompt only suggests brevity. 220 ≈ ~30-40 spoken words
# ≈ 7-10 seconds of speech, which is the right ceiling for a conversation.
# If the user asks for detail explicitly, the model can still ask "want more?"
# and the next turn lifts to whatever fits in 220 again.
VOICE_MAX_TOKENS = 220
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

    async def turn_stream(
        self,
        user_message: str,
        channel: str,
        conversation_id: str | None = None,
    ):
        """Streaming version of turn(). Yields dict events as the model
        generates, so the caller (e.g. /inbound_stream) can pump them down
        an SSE pipe and the UI can start TTS on the first sentence.

        Event shapes:
            {"type": "text",  "delta": str}            — incremental token text
            {"type": "tool_start", "name": str}         — about to run a tool
            {"type": "tool_done",  "name": str}         — tool finished
            {"type": "done", "text": str, "conv_id": str}
            {"type": "error", "message": str}

        Mirrors turn()'s memory + distill + self-profile bookkeeping at end.
        """
        conv_id = conversation_id or uuid.uuid4().hex[:12]
        is_voice = channel == "voice"
        try:
            # Run the four pre-LLM IO operations concurrently. Each one is
            # 10-100ms of disk/SQLite work; serial they add up to 100-400ms
            # of dead time before the Anthropic call even starts. Voice mode
            # uses the lite retrieval path which skips lessons/playbooks/KG
            # for another ~50-150ms saved.
            retrieved, legacy, prev_assistant, prior_turns = await asyncio.gather(
                asyncio.to_thread(self._retrieve_context, user_message, is_voice),
                asyncio.to_thread(self._build_memory_preamble),
                asyncio.to_thread(self._prev_assistant_text, conv_id),
                asyncio.to_thread(self._load_prior_turns, conv_id, 20),
            )
            composed = (retrieved + legacy + user_message) if (retrieved or legacy) else user_message

            messages: list[dict] = prior_turns + [{"role": "user", "content": composed}]
            # log_turn is a write; fire and forget on the executor so we
            # don't block the LLM call on SQLite.
            asyncio.create_task(asyncio.to_thread(
                memory.log_turn, conv_id, channel, {"role": "user", "content": user_message}
            ))

            model = VOICE_MODEL if is_voice else self.model
            max_tokens = VOICE_MAX_TOKENS if is_voice else DEFAULT_MAX_TOKENS
            system = self.system_prompt

            try:
                from sentrial.evolution import self_profile
                self_block = self_profile.summary_for_prompt()
                if self_block:
                    system = system + "\n\n[identity — your evolving self]\n" + self_block
            except Exception as e:  # noqa: BLE001
                log.warning("self_profile preamble failed (ignored): %s", e)

            if is_voice:
                system = (
                    system
                    + "\n\n[voice-turn mode] Your reply will be spoken aloud.\n\n"
                    "HARD RULES — every reply must obey ALL of these:\n"
                    "• MAX 30 spoken words for a normal answer. ~50 words ONLY if "
                    "the user explicitly asked you to 'explain' or 'tell me about'. "
                    "Open questions like 'what are you capable of' get the SHORT "
                    "form — give the headline, then ask if they want detail.\n"
                    "• ONE OR TWO sentences, no bullet lists, no markdown.\n"
                    "• Never list more than 3 things. If there are more, give the "
                    "top 3 and say 'and a few more — want me to keep going?'\n"
                    "• No filler openings ('Of course', 'Sure thing', 'Great "
                    "question', 'I'd be happy to'). Start with the answer.\n\n"
                    "Conversational rules — these matter as much as tool rules:\n"
                    "• When the user asks you to ADD / CREATE / MAKE / WRITE / "
                    "BUILD something (a task, note, event, email, proposal), "
                    "the FIRST reply asks what to add. Don't survey existing "
                    "state first. Bad: 'You have zero tasks. Want me to add "
                    "some?' Good: 'Sure — what's the task?'\n"
                    "• When asked 'what's my X' / 'what's on my Y', answer "
                    "from the live context immediately.\n"
                    "• Don't lecture about state ('you have zero/no/empty X') "
                    "unless that IS the literal question.\n"
                    "• If genuinely ambiguous, ask ONE crisp clarifying "
                    "question — but pick + act when intent is clear.\n\n"
                    "Proposal flow (Liam's bread and butter — get this right):\n"
                    "• 'Build me a proposal for X' / 'draft a pitch for X' / "
                    "'write a proposal' → use the start_proposal tool. ALWAYS.\n"
                    "• Brand inference: if Liam mentioned Pursuit Visuals OR "
                    "the work is video/photo/production/content → brand=\"pursuit\". "
                    "Otherwise → brand=\"sentrial\" (AI agency, the default).\n"
                    "• Format inference: 'quick / short / one-page / one-pager / "
                    "skim' → format=\"one_pager\". Default 'major' for full pitches.\n"
                    "• Gather just enough by voice — client name + a sentence or "
                    "two of brief is plenty. Don't interrogate; the generator "
                    "fills in the rest. If pricing or deadline came up, pass them "
                    "in pricing_hint / deadline. Then call start_proposal and "
                    "tell Liam: 'Drafting [brand] [format] for [client] — ready "
                    "in about [eta].' That's it. Don't wait for the file.\n\n"
                    "Tool-call rules — silence between tools is the #1 thing that "
                    "makes voice mode feel slow:\n"
                    "1. Before EVERY tool call — including ones after another tool "
                    "completed — emit ONE short narration sentence first, then make "
                    "the tool call. Examples: 'Let me pull up your calendar.', "
                    "'Got it. Removing that one now.', 'Looking it up now.'\n"
                    "2. After a tool result, give the answer in ≤1 sentence. Don't "
                    "recap what you did — just the result.\n"
                    "3. Independent tool calls in PARALLEL within one turn — never "
                    "chain calendar + email + notion across separate turns.\n"
                    "4. Pick + act when intent is clear. Save clarifying "
                    "questions for genuine ambiguity (see conversational "
                    "rules above).\n"
                    "5. Never say 'I'll get back to you' or 'let me think' — answer "
                    "now or call a tool now."
                )

            cached_system = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]

            full_text_parts: list[str] = []
            final_assistant_content: list = []

            for iteration in range(MAX_TOOL_ITERATIONS):
                async with self.client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    system=cached_system,
                    tools=self.tools,
                    messages=messages,
                ) as stream:
                    async for chunk in stream.text_stream:
                        if chunk:
                            full_text_parts.append(chunk)
                            yield {"type": "text", "delta": chunk}
                    final_msg = await stream.get_final_message()

                final_assistant_content = final_msg.content
                messages.append({"role": "assistant", "content": final_assistant_content})

                if final_msg.stop_reason == "end_turn":
                    break

                if final_msg.stop_reason == "tool_use":
                    tool_blocks = [
                        b for b in final_assistant_content
                        if getattr(b, "type", None) == "tool_use"
                    ]
                    for b in tool_blocks:
                        yield {"type": "tool_start", "name": b.name}
                    tool_results = await self._execute_tool_calls(final_assistant_content)
                    for b in tool_blocks:
                        yield {"type": "tool_done", "name": b.name}
                    messages.append({"role": "user", "content": tool_results})
                    continue

                yield {"type": "error", "message": f"unexpected stop_reason={final_msg.stop_reason}"}
                return

            text = "".join(full_text_parts).strip()
            memory.log_turn(conv_id, channel, {"role": "assistant", "content": text})
            try:
                from sentrial.evolution import self_profile
                new_conv = not bool(prior_turns)
                self_profile.bump_stats(turns_delta=1, new_conversation=new_conv)
            except Exception as e:  # noqa: BLE001
                log.warning("self_profile.bump_stats failed (ignored): %s", e)
            self._schedule_distill(
                user_message=user_message, assistant_reply=text,
                prev_assistant=prev_assistant, conversation_id=conv_id,
            )
            # Refresh the live-context cache in the background so the NEXT turn
            # benefits from updated state (e.g. a todo we just removed is gone
            # by the time the user asks "what's left?"). Non-blocking.
            try:
                from sentrial.core import context_prefetch
                context_prefetch.schedule_background_refresh()
            except Exception as e:  # noqa: BLE001
                log.warning("context_prefetch refresh failed (ignored): %s", e)
            yield {"type": "done", "text": text, "conv_id": conv_id}
        except Exception as e:  # noqa: BLE001
            log.exception("turn_stream failed")
            yield {"type": "error", "message": str(e)}

    async def turn(
        self,
        user_message: str,
        channel: str,
        conversation_id: str | None = None,
    ) -> str:
        """Run one conversational turn. Returns the assistant's final text reply."""
        conv_id = conversation_id or uuid.uuid4().hex[:12]
        is_voice = channel == "voice"

        # Run the four pre-LLM IO operations concurrently — same parallelization
        # as turn_stream. Voice channel uses the lite retrieval path (skips
        # lessons/playbooks/KG since live_context already carries actionable
        # data). Chat keeps full retrieval for richer context.
        retrieved, legacy, prev_assistant, prior_turns = await asyncio.gather(
            asyncio.to_thread(self._retrieve_context, user_message, is_voice),
            asyncio.to_thread(self._build_memory_preamble),
            asyncio.to_thread(self._prev_assistant_text, conv_id),
            asyncio.to_thread(self._load_prior_turns, conv_id, 20),
        )
        composed = (retrieved + legacy + user_message) if (retrieved or legacy) else user_message
        messages: list[dict] = prior_turns + [{"role": "user", "content": composed}]
        # log_turn write fires off the critical path.
        asyncio.create_task(asyncio.to_thread(
            memory.log_turn, conv_id, channel, {"role": "user", "content": user_message}
        ))

        # Per-channel model + token budget. Voice routes to Haiku 4.5 with
        # a tight max_tokens so replies stay short and the time-to-last-token
        # is ~1-3s vs. Opus's 8-30s. Pre-turn retrieval already injects
        # schedule/calendar/profile/lessons so Haiku has the context it
        # needs for most voice questions. is_voice was determined at the top
        # of the function for the parallelized retrieval gather.
        model = VOICE_MODEL if is_voice else self.model
        max_tokens = VOICE_MAX_TOKENS if is_voice else DEFAULT_MAX_TOKENS
        system = self.system_prompt

        # Inject Sentrial's evolving self-profile — persona traits, values,
        # growth log, recent memories. This is THE thing that gives Sentrial a
        # coherent identity across conversations: the LLM sees who it has
        # become, not just who the static system prompt said it was.
        try:
            from sentrial.evolution import self_profile
            self_block = self_profile.summary_for_prompt()
            if self_block:
                system = system + "\n\n[identity — your evolving self]\n" + self_block
        except Exception as e:  # noqa: BLE001
            log.warning("self_profile preamble failed (ignored): %s", e)

        if is_voice:
            system = (
                system
                + "\n\n[voice-turn mode] Your reply will be spoken aloud.\n\n"
                "HARD RULES — every reply must obey ALL of these:\n"
                "• MAX 30 spoken words for a normal answer. ~50 words ONLY if "
                "the user explicitly asked you to 'explain' or 'tell me about'. "
                "Open questions like 'what are you capable of' get the SHORT "
                "form — give the headline, then ask if they want detail.\n"
                "• ONE OR TWO sentences, no bullet lists, no markdown.\n"
                "• Never list more than 3 things. If there are more, give the "
                "top 3 and say 'and a few more — want me to keep going?'\n"
                "• No filler openings ('Of course', 'Sure thing', 'Great "
                "question', 'I'd be happy to'). Start with the answer.\n\n"
                "Tool-call rules:\n"
                "1. Before EVERY tool call — including ones after another tool "
                "completed — emit ONE short narration sentence first.\n"
                "2. After a tool result, give the answer in ≤1 sentence.\n"
                "3. Make independent tool calls in PARALLEL within one turn.\n"
                "4. Don't ask clarifying questions in voice — pick + act.\n"
                "5. Never say 'I'll get back to you' or 'let me think' — answer "
                "now or call a tool now."
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
                # Bump self-profile counters; first turn of a conversation also
                # increments total_conversations. Off the critical path; failures
                # don't affect the reply.
                try:
                    from sentrial.evolution import self_profile
                    new_conv = not bool(prior_turns)
                    self_profile.bump_stats(turns_delta=1, new_conversation=new_conv)
                except Exception as e:  # noqa: BLE001
                    log.warning("self_profile.bump_stats failed (ignored): %s", e)
                # Fire-and-forget distillation — doesn't block the reply.
                self._schedule_distill(
                    user_message=user_message, assistant_reply=text,
                    prev_assistant=prev_assistant, conversation_id=conv_id,
                )
                # Refresh the live-context cache in the background — same
                # rationale as turn_stream's hook.
                try:
                    from sentrial.core import context_prefetch
                    context_prefetch.schedule_background_refresh()
                except Exception as e:  # noqa: BLE001
                    log.warning("context_prefetch refresh failed (ignored): %s", e)
                return text

            if resp.stop_reason == "tool_use":
                tool_results = await self._execute_tool_calls(resp.content)
                messages.append({"role": "user", "content": tool_results})
                continue

            log.warning(f"unexpected stop_reason={resp.stop_reason}; bailing")
            return f"[sentrial] unexpected stop_reason: {resp.stop_reason}"

        log.warning(f"hit MAX_TOOL_ITERATIONS={MAX_TOOL_ITERATIONS}")
        return "[sentrial] hit tool-iteration limit — stopping"

    def _retrieve_context(self, user_message: str, lite: bool = False) -> str:
        """Build the pre-turn context block. Fails closed — any error in the
        evolution layer returns empty rather than blocking the turn.

        lite=True skips lessons/playbooks/KG. Used for voice channels where
        live_context already carries the actionable data and we want every
        millisecond of pre-LLM latency back."""
        try:
            from sentrial.evolution import retrieval
            ctx = retrieval.build_lite(user_message) if lite else retrieval.build(user_message)
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

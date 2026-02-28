"""
Core agent loop — language-model backed agent that:
  1. Reads messages from its Redis inbox
  2. Runs the Anthropic tool-use loop
  3. Executes tools (send_message, file ops)
  4. Repeats
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import pathlib
import sys
from typing import Any

import anthropic

from message_bus import MessageBus
from models import Message, MessageType, Priority
from tools import build_tool_schemas, execute_tool

logger = logging.getLogger(__name__)


class Agent:
    def __init__(self) -> None:
        self.role: str = os.environ["ROLE"]
        self.model: str = os.environ.get("MODEL", "claude-opus-4-6")
        self.redis_url: str = os.environ["REDIS_URL"]

        # Load role-specific config from /agent/config.py
        cfg = self._load_config()
        self.allowed_tools: list[str] = cfg.get("ALLOWED_TOOLS", ["send_message"])
        self.available_roles: list[str] = cfg.get("AVAILABLE_ROLES", [])

        # Load system prompt
        self.system_prompt: str = self._load_system_prompt()

        # Build tool schemas (static per agent lifetime)
        self.tool_schemas: list[dict] = build_tool_schemas(
            self.allowed_tools, self.available_roles
        )

        # Clients
        self.bus = MessageBus(self.redis_url, self.role)
        self.claude = anthropic.AsyncAnthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"]
        )

    # ─────────────────────────────────────────
    # Startup helpers
    # ─────────────────────────────────────────

    def _load_config(self) -> dict[str, Any]:
        config_path = pathlib.Path("/agent/config.py")
        if not config_path.exists():
            logger.warning("[%s] No config.py found, using defaults.", self.role)
            return {}
        spec = importlib.util.spec_from_file_location("agent_config", config_path)
        module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return {k: getattr(module, k) for k in dir(module) if k.isupper()}

    def _load_system_prompt(self) -> str:
        prompt_path = pathlib.Path("/agent/system_prompt.md")
        if not prompt_path.exists():
            raise FileNotFoundError(f"system_prompt.md missing for role {self.role}")
        content = prompt_path.read_text(encoding="utf-8")
        # Substitute runtime env vars so role-specific prompts can reference them
        substitutions = {
            "{{ROLE}}": self.role,
            "{{INSTANCE_ID}}": os.environ.get("INSTANCE_ID", "1"),
            "{{MENTOR_ROLE}}": os.environ.get("MENTOR_ROLE", "senior_dev_1"),
            "{{MENTEE_ROLE}}": os.environ.get("MENTEE_ROLE", ""),
        }
        for placeholder, value in substitutions.items():
            content = content.replace(placeholder, value)
        return content

    # ─────────────────────────────────────────
    # Main loop
    # ─────────────────────────────────────────

    async def run(self) -> None:
        await self.bus.setup()
        await self._load_agent_memories()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name=f"heartbeat-{self.role}")
        logger.info("[%s] Online. Model=%s Tools=%s", self.role, self.model, self.allowed_tools)

        try:
            while True:
                try:
                    entries = await self.bus.receive(count=5, block_ms=2000)
                    for redis_id, message in entries:
                        try:
                            await self.process_message(message)
                        except Exception as exc:
                            logger.exception("[%s] Failed processing message %s: %s", self.role, message.id, exc)
                        finally:
                            await self.bus.ack(redis_id)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.exception("[%s] Loop error: %s", self.role, exc)
                    await asyncio.sleep(2)
        finally:
            heartbeat_task.cancel()

    # ─────────────────────────────────────────
    # Message processing
    # ─────────────────────────────────────────

    async def process_message(self, message: Message) -> None:
        logger.info(
            "[%s] ← [%s] %s (thread=%s)",
            self.role,
            message.from_role,
            message.type.value,
            message.thread_id[:8],
        )

        # Fetch thread history for context continuity
        history = await self.bus.get_thread_history(message.thread_id, limit=20)
        prior = [m for m in history if m.id != message.id]

        user_content = self._format_inbound(message)
        if prior:
            context_block = await self._build_history_context(prior)
            user_content = f"{context_block}\n\n{user_content}"

        conversation: list[dict] = [{"role": "user", "content": user_content}]
        await self._agentic_loop(conversation, context_message=message)

    # ─────────────────────────────────────────
    # Thread history & summarization
    # ─────────────────────────────────────────

    VERBATIM_RECENT = 6    # last N messages shown in full
    SUMMARIZE_ABOVE = 6    # summarize when prior > this

    async def _build_history_context(self, prior: list[Message]) -> str:
        """Format prior thread messages as a context block, summarizing old ones."""
        if len(prior) <= self.VERBATIM_RECENT:
            lines = [f"[THREAD CONTEXT — {len(prior)} prior message(s) in this conversation]"]
            for m in prior:
                lines.append(f"\n{m.from_role} → {m.to_role} [{m.type.value}]:\n{m.content[:400]}")
            lines.append("\n[END THREAD CONTEXT]")
            return "\n".join(lines)

        # Older messages get summarized; recent ones shown verbatim
        to_summarize = prior[:-self.VERBATIM_RECENT]
        recent = prior[-self.VERBATIM_RECENT:]

        summary = await self._summarize_messages(to_summarize)

        lines = [
            f"[THREAD CONTEXT — {len(prior)} prior messages]",
            f"\n[SUMMARY of {len(to_summarize)} older messages]\n{summary}",
            f"\n[MOST RECENT {len(recent)} messages]",
        ]
        for m in recent:
            lines.append(f"\n{m.from_role} → {m.to_role} [{m.type.value}]:\n{m.content[:400]}")
        lines.append("\n[END THREAD CONTEXT]")
        return "\n".join(lines)

    async def _summarize_messages(self, messages: list[Message]) -> str:
        """Use Haiku to cheaply summarize a list of messages."""
        text = "\n\n".join(
            f"[{m.from_role} → {m.to_role} | {m.type.value}]\n{m.content[:300]}"
            for m in messages
        )
        try:
            resp = await self.claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=250,
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarize this team conversation thread in 3-4 sentences. "
                        "Focus on: what was requested, decisions made, and current state. "
                        "Be specific about file names and outcomes.\n\n"
                        f"{text}"
                    ),
                }],
            )
            return resp.content[0].text
        except Exception as exc:
            logger.warning("[%s] Summarization failed: %s", self.role, exc)
            # Fallback: return truncated raw messages
            return " | ".join(f"{m.from_role}→{m.to_role}: {m.content[:80]}" for m in messages)

    def _format_inbound(self, message: Message) -> str:
        lines = [
            f"[INCOMING MESSAGE]",
            f"From: {message.from_role}",
            f"Type: {message.type.value}",
            f"Priority: {message.priority.value}",
            f"Thread: {message.thread_id}",
            f"Message ID: {message.id}",
        ]
        if message.parent_message_id:
            lines.append(f"Reply to: {message.parent_message_id}")
        if message.artifacts:
            lines.append(f"Artifacts: {', '.join(message.artifacts)}")
        lines.append("")
        lines.append(message.content)
        return "\n".join(lines)

    # ─────────────────────────────────────────
    # Agentic tool-use loop
    # ─────────────────────────────────────────

    async def _agentic_loop(
        self, conversation: list[dict], *, context_message: Message
    ) -> None:
        """
        Run the standard Anthropic tool-use loop:
          ask Claude → if tool_use: execute + feed results → repeat
          until stop_reason == "end_turn"
        """
        max_iterations = 25  # safety cap (increased for complex multi-step workflows)

        # Loop detection: track (tool_name, frozen_inputs) of last N tool calls
        _recent_calls: list[tuple[str, str]] = []
        _LOOP_WINDOW = 4   # look back over last N tool calls
        _LOOP_THRESHOLD = 3  # how many identical calls in window = stuck

        for iteration in range(max_iterations):
            response = await self.claude.messages.create(
                model=self.model,
                max_tokens=4096,
                system=self.system_prompt,
                messages=conversation,
                tools=self.tool_schemas,
            )

            # Fire-and-forget token telemetry
            asyncio.create_task(
                self._record_metrics(response, thread_id=context_message.thread_id)
            )

            logger.debug(
                "[%s] Claude stop_reason=%s iteration=%d in=%d out=%d",
                self.role,
                response.stop_reason,
                iteration,
                response.usage.input_tokens,
                response.usage.output_tokens,
            )

            # Add assistant's response to conversation history
            conversation.append(
                {"role": "assistant", "content": response.content}
            )

            if response.stop_reason == "end_turn":
                # Log any final text
                for block in response.content:
                    if hasattr(block, "text") and block.text:
                        logger.info("[%s] (thinking) %s", self.role, block.text[:200])
                break

            if response.stop_reason == "tool_use":
                tool_results = await self._execute_tool_blocks(
                    response.content, context_message=context_message
                )

                # Loop detection: check if we're calling the same tool repeatedly
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use":
                        call_sig = (block.name, json.dumps(block.input, sort_keys=True))
                        _recent_calls.append(call_sig)
                        if len(_recent_calls) > _LOOP_WINDOW:
                            _recent_calls.pop(0)

                # If any single (tool, inputs) pair fills most of the window → stuck
                if len(_recent_calls) >= _LOOP_THRESHOLD:
                    from collections import Counter
                    counts = Counter(_recent_calls)
                    most_common_sig, most_common_count = counts.most_common(1)[0]
                    if most_common_count >= _LOOP_THRESHOLD:
                        loop_tool = most_common_sig[0]
                        logger.warning(
                            "[%s] Loop detected: '%s' called %d times with same inputs — injecting circuit-breaker",
                            self.role, loop_tool, most_common_count,
                        )
                        _recent_calls.clear()
                        conversation.append({
                            "role": "user",
                            "content": (
                                f"⚠️ LOOP DETECTED: You have called `{loop_tool}` {most_common_count} times "
                                f"in a row with the same inputs and it is not making progress. "
                                f"STOP calling `{loop_tool}`. "
                                f"Either the task is already complete and you should send `task_complete` to engineering_manager, "
                                f"or you need a completely different approach. "
                                f"Do NOT repeat the same failing tool call again. Make a decision and move on."
                            ),
                        })
                        continue

                conversation.append({"role": "user", "content": tool_results})
            elif response.stop_reason == "max_tokens":
                # Response was cut off mid-generation. If there are tool_use blocks,
                # execute them and continue. Otherwise inject a nudge to use tools.
                tool_blocks = [b for b in response.content if b.type == "tool_use"]
                if tool_blocks:
                    tool_results = await self._execute_tool_blocks(
                        response.content, context_message=context_message
                    )
                    conversation.append({"role": "user", "content": tool_results})
                else:
                    logger.warning(
                        "[%s] max_tokens hit with no tool calls — injecting continuation nudge",
                        self.role,
                    )
                    conversation.append({
                        "role": "user",
                        "content": (
                            "Your response was cut off because it exceeded the token limit. "
                            "Please continue by calling the appropriate tools now (write_file, "
                            "send_message, etc.) rather than writing long text responses. "
                            "Use tools to complete your task."
                        ),
                    })
            else:
                logger.warning("[%s] Unexpected stop_reason: %s", self.role, response.stop_reason)
                break

    async def _execute_tool_blocks(
        self, content_blocks: list, *, context_message: Message
    ) -> list[dict]:
        """Execute all tool_use blocks and return tool_result list."""
        import time
        results: list[dict] = []

        for block in content_blocks:
            if block.type != "tool_use":
                continue

            logger.info("[%s] tool_call: %s(%s)", self.role, block.name, list(block.input.keys()))

            start = time.monotonic()
            result = await execute_tool(
                block.name,
                block.input,
                bus=self.bus,
                current_message=context_message,
                agent_role=self.role,
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            success = "error" not in result
            asyncio.create_task(self._record_tool_execution(
                tool_name=block.name,
                thread_id=context_message.thread_id,
                duration_ms=duration_ms,
                success=success,
                error=result.get("error", "") if not success else "",
            ))

            logger.info("[%s] tool_result: %s → %s (%dms)", self.role, block.name, result, duration_ms)

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                }
            )

        return results

    # ─────────────────────────────────────────
    # Phase 4: token telemetry + persistent memory
    # ─────────────────────────────────────────

    async def _record_tool_execution(
        self, *, tool_name: str, thread_id: str,
        duration_ms: int, success: bool, error: str = ""
    ) -> None:
        """POST tool execution record to the orchestrator asynchronously (best-effort)."""
        import httpx
        url = os.environ.get("ORCHESTRATOR_URL", "")
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                await client.post(f"{url}/tool-executions", json={
                    "agent_role": self.role,
                    "tool_name": tool_name,
                    "thread_id": thread_id,
                    "duration_ms": duration_ms,
                    "success": success,
                    "error": error,
                })
        except Exception:
            pass  # telemetry is best-effort

    async def _record_metrics(self, response: Any, *, thread_id: str) -> None:
        """POST token usage to the orchestrator asynchronously (best-effort)."""
        import httpx
        orchestrator_url = os.environ.get("ORCHESTRATOR_URL", "")
        if not orchestrator_url:
            return
        data = {
            "agent_role": self.role,
            "thread_id": thread_id,
            "model": self.model,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{orchestrator_url}/metrics", json=data)
        except Exception as exc:
            logger.debug("[%s] Metrics recording failed (non-fatal): %s", self.role, exc)

    async def _heartbeat_loop(self) -> None:
        """Ping the orchestrator every 30s so we appear online in /agents."""
        import httpx
        url = os.environ.get("ORCHESTRATOR_URL", "")
        if not url:
            return
        while True:
            await asyncio.sleep(30)
            try:
                async with httpx.AsyncClient(timeout=3) as client:
                    await client.post(f"{url}/heartbeat/{self.role}")
            except Exception:
                pass  # heartbeat is best-effort

    async def _load_agent_memories(self) -> None:
        """Load persisted memories and append to system prompt (best-effort)."""
        import httpx
        orchestrator_url = os.environ.get("ORCHESTRATOR_URL", "")
        if not orchestrator_url:
            return
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{orchestrator_url}/memory/{self.role}")
                if resp.status_code != 200:
                    return
                items: list[dict] = resp.json()
            if items:
                lines = ["\n\n## Your Persistent Memories (from previous sessions)"]
                for item in items:
                    lines.append(f"- **{item['key']}**: {item['value']}")
                self.system_prompt += "\n".join(lines)
                logger.info("[%s] Loaded %d memories from store.", self.role, len(items))
        except Exception as exc:
            logger.debug("[%s] Memory load failed (non-fatal): %s", self.role, exc)

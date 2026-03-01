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
        _api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        _github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not _api_key and _github_token:
            logger.info("[%s] Using GitHub Copilot as AI backend.", self.role)
            self.claude = anthropic.AsyncAnthropic(
                api_key="github-copilot",
                base_url="https://api.githubcopilot.com",
                default_headers={
                    "Authorization": f"Bearer {_github_token}",
                    "Copilot-Integration-Id": "vscode-chat",
                },
            )
        elif _api_key:
            self.claude = anthropic.AsyncAnthropic(api_key=_api_key)
        else:
            raise RuntimeError(
                "Either ANTHROPIC_API_KEY or GITHUB_TOKEN must be set in the environment."
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

    async def _call_claude_with_retry(self, **kwargs) -> Any:
        """Call Claude API with exponential backoff on transient errors (rate limits, 5xx)."""
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                return await self.claude.messages.create(**kwargs)
            except anthropic.RateLimitError as exc:
                wait = 5 * (2 ** attempt)  # 5s, 10s, 20s
                logger.warning("[%s] Rate limited, retrying in %ds (attempt %d/3)", self.role, wait, attempt + 1)
                last_exc = exc
                await asyncio.sleep(wait)
            except anthropic.InternalServerError as exc:
                wait = 2 * (2 ** attempt)  # 2s, 4s, 8s
                logger.warning("[%s] API 500 error, retrying in %ds (attempt %d/3)", self.role, wait, attempt + 1)
                last_exc = exc
                await asyncio.sleep(wait)
            except anthropic.APIConnectionError as exc:
                wait = 3 * (2 ** attempt)  # 3s, 6s, 12s
                logger.warning("[%s] API connection error, retrying in %ds (attempt %d/3)", self.role, wait, attempt + 1)
                last_exc = exc
                await asyncio.sleep(wait)
        raise last_exc  # type: ignore[misc]

    async def _send_exhaustion_rescue(self, context_message: Message, max_iters: int) -> None:
        """Notify EM when this agent exhausts its iteration limit without completing the task."""
        import httpx
        url = os.environ.get("ORCHESTRATOR_URL", "")
        if not url:
            return
        rescue_id = __import__("uuid").uuid4().hex
        from datetime import datetime, timezone
        payload = {
            "id": rescue_id,
            "thread_id": context_message.thread_id,
            "from_role": self.role,
            "to_role": "engineering_manager",
            "type": "status_update",
            "content": (
                f"⚠️ AGENT STUCK — I exhausted {max_iters} iteration steps without completing my task.\n\n"
                f"Task: {context_message.content[:400]}\n\n"
                f"I was unable to finish within the allowed steps. Please review and decide: "
                f"reassign this task, break it into smaller pieces, or clarify what is needed. "
                f"Any partial work I completed is saved in /workspace."
            ),
            "priority": "high",
            "artifacts": "[]",
            "parent_message_id": context_message.id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": "{}",
        }
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(f"{url}/messages", json=payload)
            logger.warning("[%s] Sent exhaustion rescue to EM for thread %s", self.role, context_message.thread_id[:8])
        except Exception as exc:
            logger.error("[%s] Failed to send exhaustion rescue: %s", self.role, exc)

    async def _agentic_loop(
        self, conversation: list[dict], *, context_message: Message
    ) -> None:
        """
        Run the standard Anthropic tool-use loop:
          ask Claude → if tool_use: execute + feed results → repeat
          until stop_reason == "end_turn"
        """
        max_iterations = 30  # safety cap (increased for complex multi-step workflows)

        # Loop detection: track (tool_name, frozen_inputs) of last N tool calls
        _recent_calls: list[tuple[str, str]] = []
        _LOOP_WINDOW = 4   # look back over last N tool calls
        _LOOP_THRESHOLD = 3  # how many identical calls in window = stuck

        # Persistent tool-error detection: if the same tool errors N times → recovery hint
        _tool_error_streak: dict[str, int] = {}
        _ERROR_STREAK_THRESHOLD = 3

        _completed_naturally = False  # set True on end_turn break

        for iteration in range(max_iterations):
            try:
                response = await self._call_claude_with_retry(
                    model=self.model,
                    max_tokens=8192,
                    system=self.system_prompt,
                    messages=conversation,
                    tools=self.tool_schemas,
                )
            except Exception as api_exc:
                logger.error("[%s] API call failed after retries: %s", self.role, api_exc)
                await self._send_exhaustion_rescue(context_message, max_iterations)
                return

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
                _completed_naturally = True
                break

            if response.stop_reason == "tool_use":
                tool_results = await self._execute_tool_blocks(
                    response.content, context_message=context_message
                )

                # ── Persistent tool-error detection ──────────────────────────
                # Build id→name map so we can match results back to tool names
                id_to_name = {
                    b.id: b.name
                    for b in response.content
                    if getattr(b, "type", None) == "tool_use"
                }
                for result in tool_results:
                    if result.get("type") != "tool_result":
                        continue
                    tool_name = id_to_name.get(result.get("tool_use_id", ""), "unknown")
                    try:
                        content_obj = json.loads(result.get("content", "{}"))
                        if isinstance(content_obj, dict) and "error" in content_obj:
                            _tool_error_streak[tool_name] = _tool_error_streak.get(tool_name, 0) + 1
                            if _tool_error_streak[tool_name] >= _ERROR_STREAK_THRESHOLD:
                                logger.warning(
                                    "[%s] Tool '%s' errored %d consecutive times — injecting recovery hint",
                                    self.role, tool_name, _tool_error_streak[tool_name],
                                )
                                _tool_error_streak[tool_name] = 0
                                tool_results.append({
                                    "type": "text",
                                    "text": (
                                        f"⚠️ RECOVERY: Tool `{tool_name}` has failed {_ERROR_STREAK_THRESHOLD} "
                                        f"consecutive times. Stop using `{tool_name}` and either: "
                                        f"(1) try a completely different approach to accomplish the same goal, "
                                        f"or (2) send a `status_update` to `engineering_manager` describing "
                                        f"the blocker so it can be escalated or reassigned."
                                    ),
                                })
                        else:
                            _tool_error_streak[tool_name] = 0  # success — reset streak
                    except (json.JSONDecodeError, AttributeError):
                        pass

                # ── Loop detection ────────────────────────────────────────────
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
                        # IMPORTANT: tool_results MUST be added first to keep the conversation
                        # valid (every tool_use block needs a matching tool_result).
                        # The circuit-breaker is appended as a text block in the SAME user
                        # turn — injecting it as a separate user message would produce a
                        # "tool_use without tool_result" 400 error from the API.
                        tool_results.append({
                            "type": "text",
                            "text": (
                                f"⚠️ LOOP DETECTED: You have called `{loop_tool}` {most_common_count} times "
                                f"in a row with the same inputs and it is not making progress. "
                                f"STOP calling `{loop_tool}`. "
                                f"Either the task is already complete and you should send `task_complete` to engineering_manager, "
                                f"or you need a completely different approach. "
                                f"Do NOT repeat the same failing tool call again. Make a decision and move on."
                            ),
                        })

                # ── Final-stretch warning ─────────────────────────────────────
                # Warn agent in the last 3 iterations so it can wrap up gracefully
                if iteration >= max_iterations - 3:
                    remaining = max_iterations - iteration - 1
                    tool_results.append({
                        "type": "text",
                        "text": (
                            f"⚠️ FINAL STRETCH: You have ~{remaining} step(s) remaining in this session. "
                            f"If your task is not fully complete, send a `status_update` to "
                            f"`engineering_manager` NOW — describe what you finished, what files you wrote, "
                            f"and what still needs doing. Do not let this session expire silently."
                        ),
                    })

                conversation.append({"role": "user", "content": tool_results})
            elif response.stop_reason == "max_tokens":
                # Response was cut off mid-generation. If there are tool_use blocks,
                # execute them and continue. Otherwise inject a nudge to use tools.
                tool_blocks = [b for b in response.content if b.type == "tool_use"]
                if tool_blocks:
                    tool_results = await self._execute_tool_blocks(
                        response.content, context_message=context_message
                    )
                    # Check if any write_file / execute_code calls had truncated inputs
                    # (missing 'content' or 'code') and inject a specific chunking nudge
                    truncated_files = [
                        b.input.get("path", "?")
                        for b in tool_blocks
                        if b.name == "write_file" and "content" not in b.input
                    ]
                    truncated_code = any(
                        b.name == "execute_code" and "code" not in b.input and "file_path" not in b.input
                        for b in tool_blocks
                    )
                    if truncated_files or truncated_code:
                        logger.warning(
                            "[%s] max_tokens caused truncated tool inputs: files=%s code=%s",
                            self.role, truncated_files, truncated_code,
                        )
                        nudge_parts = [
                            "⚠️ Your previous tool call was cut off by the token limit — the file content was not written.",
                        ]
                        if truncated_files:
                            nudge_parts.append(
                                f"write_file for '{truncated_files[0]}' received no 'content'. "
                                "Write it in chunks of ≤150 lines: "
                                "call write_file(path='...', content='...first 150 lines...', append=False) NOW, "
                                "then append=True for the rest. Start with the first chunk immediately."
                            )
                        if truncated_code:
                            nudge_parts.append(
                                "execute_code received no 'code'. Pass short inline code (≤50 lines) in the 'code' parameter."
                            )
                        tool_results.append({"type": "text", "text": " ".join(nudge_parts)})
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

        if not _completed_naturally:
            logger.error(
                "[%s] Agentic loop ended without end_turn (exhausted=%s, thread=%s)",
                self.role, not _completed_naturally, context_message.thread_id[:8],
            )
            await self._send_exhaustion_rescue(context_message, max_iterations)

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

"""
Orchestrator — the human-facing API.

Responsibilities:
  - Accept task submissions from humans (POST /task)
  - Route them into the EM's inbox as HUMAN_INPUT messages
  - Listen on its own inbox ("orchestrator") for agent replies
  - Persist all audit messages (from team:audit stream) to Postgres
  - Expose read endpoints for threads and messages

Run: uvicorn main:app --host 0.0.0.0 --port 8080 --reload
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import pathlib
import re
import subprocess
import tarfile
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

import anthropic
import asyncpg
import docker as docker_sdk
import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  [orchestrator]  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

REDIS_URL = os.environ["REDIS_URL"]
DB_URL = os.environ["DB_URL"]
SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://sandbox:8081")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
THREAD_BUDGET_TOKENS = int(os.environ.get("THREAD_BUDGET_TOKENS", "0"))  # 0 = disabled
IDLE_THREAD_MINUTES = int(os.environ.get("IDLE_THREAD_MINUTES", "0"))    # 0 = disabled
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USERNAME   = os.environ.get("GITHUB_USERNAME", "RajuRoopani")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AGENT_BASE_IMAGE  = os.environ.get("AGENT_BASE_IMAGE", "team-claw-agent-base:latest")
AUDIT_STREAM      = "team:audit"
ACTIVITY_STREAM = "team:activity"   # ephemeral working signals — not persisted to DB
AUDIT_GROUP     = "grp:orchestrator-audit"
AUDIT_CONSUMER  = "orchestrator-0"

STATIC_AGENT_ROLES = [
    "product_owner", "engineering_manager", "architect", "ux_engineer",
    "senior_dev_1", "senior_dev_2", "junior_dev_1", "junior_dev_2",
    "security_engineer",
]
# Keep ALL_AGENT_ROLES as a function so dynamic agents are always included
def ALL_AGENT_ROLES() -> list[str]:
    return STATIC_AGENT_ROLES + state.dynamic_agents


# ─────────────────────────────────────────────
# App state
# ─────────────────────────────────────────────

class AppState:
    redis: aioredis.Redis
    db: asyncpg.Pool
    audit_task: asyncio.Task | None = None
    agent_last_seen: dict[str, datetime] = {}
    budget_warnings_fired: set[str] = set()  # "{thread_id}:{threshold}"
    idle_alerts_fired: set[str] = set()      # thread_ids already alerted
    idle_task: asyncio.Task | None = None
    thread_last_activity: dict[str, dict] = {}  # thread_id → {role, ts}
    dynamic_agents: list[str] = []              # roles added at runtime


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    state.redis = aioredis.from_url(REDIS_URL, decode_responses=False)
    state.db = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)
    state.agent_last_seen = {}
    state.budget_warnings_fired = set()
    state.idle_alerts_fired = set()
    state.idle_task = None
    state.thread_last_activity = {}
    state.dynamic_agents = []
    await _setup_audit_consumer()
    await _ensure_phase4_tables()
    await _ensure_phase5_tables()
    await _ensure_phase8_tables()
    await _ensure_phase9_tables()
    await _ensure_phase10_tables()
    await _ensure_dynamic_agents_table()
    await _load_dynamic_agents()
    _init_workspace_git()
    state.audit_task = asyncio.create_task(_audit_loop(), name="audit-loop")
    state.idle_task = asyncio.create_task(_idle_monitor_loop(), name="idle-monitor")
    logger.info("Orchestrator online.")
    yield
    # Shutdown
    if state.audit_task:
        state.audit_task.cancel()
    if state.idle_task:
        state.idle_task.cancel()
    await state.redis.aclose()
    await state.db.close()


app = FastAPI(title="Team Claw Orchestrator", lifespan=lifespan)


# ─────────────────────────────────────────────
# Pydantic schemas
# ─────────────────────────────────────────────

class CreateAgentRequest(BaseModel):
    display_name: str                                    # "Security Engineer"
    description: str                                     # what the agent does
    model: str = "claude-sonnet-4-6"


class TaskRequest(BaseModel):
    title: str
    description: str
    priority: str = "normal"


class TaskResponse(BaseModel):
    thread_id: str
    message_id: str
    status: str = "submitted"
    github_repo: str = ""


class ThreadSummary(BaseModel):
    id: str
    title: str
    status: str
    message_count: int
    created_at: str
    github_repo: str = ""
    last_active_role: str = ""
    last_active_at: str = ""


class MessageOut(BaseModel):
    id: str
    thread_id: str
    from_role: str
    to_role: str
    type: str
    content: str
    priority: str
    created_at: str


# Phase 4 models
class MemoryItem(BaseModel):
    key: str
    value: str
    updated_at: str


class WikiArticle(BaseModel):
    title: str
    content: str
    author: str
    updated_at: str


class WikiWriteRequest(BaseModel):
    content: str
    author: str = "unknown"


class MetricsRecord(BaseModel):
    agent_role: str
    thread_id: str | None = None
    model: str
    input_tokens: int
    output_tokens: int


class AgentMetricsSummary(BaseModel):
    agent_role: str
    total_input_tokens: int
    total_output_tokens: int
    total_calls: int
    estimated_cost_usd: float


# Phase 5 models
class AgentStatus(BaseModel):
    role: str
    status: str           # online | stale | offline
    last_seen: str | None
    display_name: str = ""
    model: str = ""


class TaskCreate(BaseModel):
    thread_id: str
    title: str
    description: str = ""
    assignee: str = ""
    created_by: str = ""


class TaskUpdate(BaseModel):
    status: str          # pending | in_progress | review | done


class TaskOut(BaseModel):
    id: str
    thread_id: str
    title: str
    description: str
    assignee: str
    status: str
    created_by: str
    created_at: str
    updated_at: str


class CIResult(BaseModel):
    id: int
    task_id: str | None
    thread_id: str | None
    passed: int
    failed: int
    total: int
    exit_code: int
    output: str
    ran_at: str


# Phase 6 models
class ThreadSummaryDetail(BaseModel):
    id: str
    title: str
    status: str
    created_at: str
    tasks_total: int
    tasks_done: int
    tasks_in_progress: int
    ci_last_exit_code: int | None
    ci_last_passed: int | None
    ci_last_total: int | None


class CITrendPoint(BaseModel):
    exit_code: int
    passed: int
    total: int
    ran_at: str


# Phase 7 models
class ThreadBudget(BaseModel):
    thread_id: str
    tokens_used: int
    budget: int          # 0 = unlimited
    pct_used: float      # 0.0–100.0+
    status: str          # ok | warning (>80%) | exceeded (>100%) | unlimited


class StandupReport(BaseModel):
    generated_at: str
    period_hours: int
    active_threads: list[dict]
    tasks_completed: list[dict]
    tasks_in_progress: list[dict]
    ci_summary: dict
    messages_by_agent: dict
    token_cost: dict
    recent_blockers: list[dict]


# Phase 8 models
class ToolExecutionRecord(BaseModel):
    agent_role: str
    tool_name: str
    thread_id: str | None = None
    duration_ms: int = 0
    success: bool = True
    error: str = ""


class ToolExecutionOut(BaseModel):
    id: int
    agent_role: str
    tool_name: str
    thread_id: str | None
    duration_ms: int
    success: bool
    error: str
    executed_at: str


class ToolStats(BaseModel):
    tool_name: str
    total_calls: int
    success_rate: float
    avg_duration_ms: float
    p95_duration_ms: float


# Phase 9 models
class AskHumanRequest(BaseModel):
    question: str
    context: str = ""
    from_role: str


class HumanReplyRequest(BaseModel):
    message: str
    target_role: str   # which agent to reply to


class HumanQuestionOut(BaseModel):
    id: int
    thread_id: str
    from_role: str
    question: str
    context: str
    answered: bool
    answer: str
    created_at: str
    answered_at: str | None = None


# ─────────────────────────────────────────────
# API endpoints
# ─────────────────────────────────────────────

@app.post("/task", response_model=TaskResponse)
async def submit_task(req: TaskRequest) -> TaskResponse:
    """Submit a new task — creates a thread, creates GitHub repo, routes to PO."""
    thread_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    # Create GitHub repo for this task (best-effort, non-blocking on failure)
    github_repo = await _create_github_repo(_slugify_repo_name(req.title))

    # Persist thread with github_repo
    async with state.db.acquire() as conn:
        await conn.execute(
            "INSERT INTO threads(id, title, status, github_repo, created_at, updated_at) VALUES($1,$2,'active',$3,$4,$4)",
            thread_id, req.title, github_repo, now,
        )

    # Branch strategy instructions injected into every task message
    branch_instructions = ""
    if github_repo:
        branch_instructions = (
            f"\n\n---\n"
            f"**GitHub Repo**: {github_repo}\n"
            f"**Repo Name**: {_repo_name_from_url(github_repo)}\n\n"
            f"**Branch Strategy** (follow exactly to avoid conflicts):\n"
            f"1. Engineering Manager: set up project skeleton on branch `main`, commit, then push\n"
            f"2. Each developer: `git_checkout_branch` → branch named `<your_role>` (e.g. `junior_dev_1`)\n"
            f"3. Developers commit to their own branch, then push that branch\n"
            f"4. Senior Dev: `git_merge` each junior branch → their own branch, resolve any conflicts using `strategy=ours`\n"
            f"5. Engineering Manager: `git_merge` all senior branches → `main`, then call `git_push` to publish the final result\n"
            f"6. Include the GitHub repo URL in your final task_complete message so the human knows where to find the code."
        )

    # Build message payload
    payload = {
        "id": message_id,
        "thread_id": thread_id,
        "from_role": "orchestrator",
        "to_role": "product_owner",
        "type": "human_input",
        "content": f"**{req.title}**\n\n{req.description}{branch_instructions}",
        "priority": req.priority,
        "artifacts": "[]",
        "parent_message_id": "",
        "timestamp": now.isoformat(),
        "metadata": json.dumps({"source": "human", "github_repo": github_repo}),
    }

    await state.redis.xadd("agent:product_owner:inbox", _encode(payload))
    await state.redis.xadd(AUDIT_STREAM, _encode(payload))

    logger.info("Task submitted: thread=%s title=%r repo=%r", thread_id[:8], req.title, github_repo)
    return TaskResponse(thread_id=thread_id, message_id=message_id, github_repo=github_repo)


@app.get("/threads", response_model=list[ThreadSummary])
async def list_threads() -> list[ThreadSummary]:
    async with state.db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.id, t.title, t.status, t.github_repo, t.created_at,
                   COUNT(m.id) AS message_count
            FROM threads t
            LEFT JOIN messages m ON m.thread_id = t.id
            GROUP BY t.id
            ORDER BY t.created_at DESC
            LIMIT 50
            """
        )
    return [
        ThreadSummary(
            id=str(r["id"]),
            title=r["title"] or "",
            status=r["status"],
            message_count=r["message_count"],
            created_at=r["created_at"].isoformat(),
            github_repo=r["github_repo"] or "",
            last_active_role=state.thread_last_activity.get(str(r["id"]), {}).get("role", ""),
            last_active_at=state.thread_last_activity.get(str(r["id"]), {}).get("ts", ""),
        )
        for r in rows
    ]


@app.get("/threads/{thread_id}/github-repo")
async def get_thread_github_repo(thread_id: str) -> dict:
    async with state.db.acquire() as conn:
        row = await conn.fetchrow("SELECT github_repo FROM threads WHERE id=$1", thread_id)
    if not row:
        raise HTTPException(404, "Thread not found")
    return {"thread_id": thread_id, "github_repo": row["github_repo"] or ""}


@app.get("/threads/{thread_id}/messages", response_model=list[MessageOut])
async def get_thread_messages(thread_id: str) -> list[MessageOut]:
    async with state.db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, thread_id, from_role, to_role, type, content, priority, created_at
            FROM messages
            WHERE thread_id = $1
            ORDER BY created_at ASC
            """,
            thread_id,
        )
    if not rows:
        raise HTTPException(status_code=404, detail="Thread not found or empty")
    return [
        MessageOut(
            id=str(r["id"]),
            thread_id=str(r["thread_id"]),
            from_role=r["from_role"],
            to_role=r["to_role"],
            type=r["type"],
            content=r["content"],
            priority=r["priority"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@app.get("/messages/since", response_model=list[MessageOut])
async def messages_since(ts: float = 0) -> list[MessageOut]:
    """Return all messages created after the given Unix epoch millisecond timestamp.
    Used by the dashboard as a reliable polling fallback alongside SSE."""
    dt = datetime.utcfromtimestamp(ts / 1000).replace(tzinfo=timezone.utc) if ts else datetime(2000, 1, 1, tzinfo=timezone.utc)
    async with state.db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, thread_id, from_role, to_role, type, content, priority, created_at
            FROM messages
            WHERE created_at > $1
            ORDER BY created_at ASC
            LIMIT 500
            """,
            dt,
        )
    return [
        MessageOut(
            id=str(r["id"]),
            thread_id=str(r["thread_id"]),
            from_role=r["from_role"],
            to_role=r["to_role"],
            type=r["type"],
            content=r["content"],
            priority=r["priority"],
            created_at=r["created_at"].isoformat(),
        )
        for r in rows
    ]


@app.get("/threads/{thread_id}/stream")
async def stream_thread(thread_id: str):
    """Server-Sent Events stream of new messages for a thread."""
    async def event_gen() -> AsyncGenerator[str, None]:
        last_id = "$"
        while True:
            result = await state.redis.xread(
                {AUDIT_STREAM: last_id}, count=20, block=3000
            )
            if result:
                for _stream, entries in result:
                    for redis_id, fields in entries:
                        last_id = redis_id
                        decoded = _decode(fields)
                        if decoded.get("thread_id") == thread_id:
                            yield f"data: {json.dumps(decoded)}\n\n"
            else:
                yield ": ping\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────
# Phase 4: Agent memory endpoints
# ─────────────────────────────────────────────

@app.get("/memory/{role}", response_model=list[MemoryItem])
async def list_memories(role: str) -> list[MemoryItem]:
    async with state.db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key, value, updated_at FROM agent_memories WHERE agent_role=$1 ORDER BY key",
            role,
        )
    return [MemoryItem(key=r["key"], value=r["value"], updated_at=r["updated_at"].isoformat()) for r in rows]


@app.get("/memory/{role}/{key}", response_model=MemoryItem)
async def get_memory(role: str, key: str) -> MemoryItem:
    async with state.db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT key, value, updated_at FROM agent_memories WHERE agent_role=$1 AND key=$2",
            role, key,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Memory key not found")
    return MemoryItem(key=row["key"], value=row["value"], updated_at=row["updated_at"].isoformat())


@app.put("/memory/{role}/{key}")
async def set_memory(role: str, key: str, body: dict) -> dict:
    value = body.get("value", "")
    async with state.db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_memories(agent_role, key, value, updated_at)
            VALUES($1,$2,$3,NOW())
            ON CONFLICT (agent_role, key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
            """,
            role, key, value,
        )
    return {"status": "ok", "role": role, "key": key}


@app.delete("/memory/{role}/{key}")
async def delete_memory(role: str, key: str) -> dict:
    async with state.db.acquire() as conn:
        await conn.execute(
            "DELETE FROM agent_memories WHERE agent_role=$1 AND key=$2", role, key
        )
    return {"status": "deleted"}


# ─────────────────────────────────────────────
# Phase 4: Team wiki endpoints
# ─────────────────────────────────────────────

@app.get("/wiki", response_model=list[WikiArticle])
async def list_wiki(q: str = "") -> list[WikiArticle]:
    async with state.db.acquire() as conn:
        if q:
            rows = await conn.fetch(
                "SELECT title, content, author, updated_at FROM team_wiki "
                "WHERE title ILIKE $1 OR content ILIKE $1 ORDER BY updated_at DESC",
                f"%{q}%",
            )
        else:
            rows = await conn.fetch(
                "SELECT title, content, author, updated_at FROM team_wiki ORDER BY updated_at DESC"
            )
    return [
        WikiArticle(title=r["title"], content=r["content"], author=r["author"], updated_at=r["updated_at"].isoformat())
        for r in rows
    ]


@app.get("/wiki/{title:path}", response_model=WikiArticle)
async def get_wiki(title: str) -> WikiArticle:
    async with state.db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT title, content, author, updated_at FROM team_wiki WHERE title=$1", title
        )
    if not row:
        raise HTTPException(status_code=404, detail="Wiki article not found")
    return WikiArticle(title=row["title"], content=row["content"], author=row["author"], updated_at=row["updated_at"].isoformat())


@app.put("/wiki/{title:path}")
async def set_wiki(title: str, body: WikiWriteRequest) -> dict:
    async with state.db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO team_wiki(title, content, author, updated_at)
            VALUES($1,$2,$3,NOW())
            ON CONFLICT (title) DO UPDATE SET content=EXCLUDED.content, author=EXCLUDED.author, updated_at=NOW()
            """,
            title, body.content, body.author,
        )
    return {"status": "ok", "title": title}


# ─────────────────────────────────────────────
# Phase 4: Token telemetry endpoints
# ─────────────────────────────────────────────

# Cost per million tokens (USD) — approximate as of Feb 2026
_COST_TABLE = {
    "claude-opus-4-6":          (15.0, 75.0),
    "claude-sonnet-4-6":        (3.0,  15.0),
    "claude-haiku-4-5-20251001":(0.25,  1.25),
    "claude-haiku-4-5":         (0.25,  1.25),
}


def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_rate, out_rate = _COST_TABLE.get(model, (3.0, 15.0))
    return (input_tokens * in_rate + output_tokens * out_rate) / 1_000_000


async def _compute_thread_tokens(thread_id: str) -> int:
    async with state.db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS total FROM agent_metrics WHERE thread_id=$1",
            thread_id,
        )
    return int(row["total"]) if row else 0


@app.post("/metrics", status_code=201)
async def record_metrics(rec: MetricsRecord) -> dict:
    async with state.db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agent_metrics(agent_role, thread_id, model, input_tokens, output_tokens)
            VALUES($1,$2,$3,$4,$5)
            """,
            rec.agent_role, rec.thread_id, rec.model, rec.input_tokens, rec.output_tokens,
        )
    # Track last activity per thread (in-memory, for /threads response)
    if rec.thread_id:
        now_str = datetime.now(timezone.utc).isoformat()
        state.thread_last_activity[rec.thread_id] = {"role": rec.agent_role, "ts": now_str}
        # Publish ephemeral working signal to activity stream (not persisted to DB)
        activity_payload = {
            "type": "agent_working",
            "from_role": rec.agent_role,
            "thread_id": rec.thread_id,
            "ts": now_str,
        }
        await state.redis.xadd(ACTIVITY_STREAM, _encode(activity_payload), maxlen=500)
    # Budget monitoring
    if THREAD_BUDGET_TOKENS > 0 and rec.thread_id:
        used = await _compute_thread_tokens(rec.thread_id)
        now_str = datetime.now(timezone.utc).isoformat()
        if used > THREAD_BUDGET_TOKENS:
            key = f"{rec.thread_id}:exceeded"
            if key not in state.budget_warnings_fired:
                state.budget_warnings_fired.add(key)
                asyncio.create_task(_fire_webhook("budget.exceeded", {
                    "thread_id": rec.thread_id, "tokens_used": used, "budget": THREAD_BUDGET_TOKENS,
                }))
                payload = {
                    "id": str(uuid.uuid4()), "thread_id": rec.thread_id,
                    "from_role": "orchestrator", "to_role": "orchestrator",
                    "type": "budget_exceeded",
                    "content": f"⚠️ Thread {rec.thread_id[:8]} exceeded token budget ({used:,}/{THREAD_BUDGET_TOKENS:,})",
                    "priority": "high", "artifacts": "[]", "parent_message_id": "",
                    "timestamp": now_str, "metadata": "{}",
                }
                await state.redis.xadd(AUDIT_STREAM, _encode(payload))
        elif used > THREAD_BUDGET_TOKENS * 0.8:
            key = f"{rec.thread_id}:warning"
            if key not in state.budget_warnings_fired:
                state.budget_warnings_fired.add(key)
                payload = {
                    "id": str(uuid.uuid4()), "thread_id": rec.thread_id,
                    "from_role": "orchestrator", "to_role": "orchestrator",
                    "type": "budget_warning",
                    "content": f"🔶 Thread {rec.thread_id[:8]} at {used/THREAD_BUDGET_TOKENS*100:.0f}% of token budget ({used:,}/{THREAD_BUDGET_TOKENS:,})",
                    "priority": "normal", "artifacts": "[]", "parent_message_id": "",
                    "timestamp": now_str, "metadata": "{}",
                }
                await state.redis.xadd(AUDIT_STREAM, _encode(payload))
    return {"status": "recorded"}


@app.get("/metrics", response_model=list[AgentMetricsSummary])
async def get_metrics() -> list[AgentMetricsSummary]:
    async with state.db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT agent_role, model,
                   SUM(input_tokens)  AS total_input,
                   SUM(output_tokens) AS total_output,
                   COUNT(*)           AS total_calls
            FROM agent_metrics
            GROUP BY agent_role, model
            ORDER BY agent_role
            """
        )
    # Merge by role (an agent may have used multiple models if config changed)
    merged: dict[str, dict] = {}
    for r in rows:
        role = r["agent_role"]
        if role not in merged:
            merged[role] = {"total_input": 0, "total_output": 0, "calls": 0, "cost": 0.0}
        merged[role]["total_input"]  += r["total_input"]
        merged[role]["total_output"] += r["total_output"]
        merged[role]["calls"]        += r["total_calls"]
        merged[role]["cost"]         += _estimate_cost(r["model"], r["total_input"], r["total_output"])

    return [
        AgentMetricsSummary(
            agent_role=role,
            total_input_tokens=v["total_input"],
            total_output_tokens=v["total_output"],
            total_calls=v["calls"],
            estimated_cost_usd=round(v["cost"], 4),
        )
        for role, v in merged.items()
    ]


# ─────────────────────────────────────────────
# Phase 5: Agent heartbeat endpoints
# ─────────────────────────────────────────────

@app.post("/heartbeat/{role}", status_code=200)
async def heartbeat(role: str) -> dict:
    state.agent_last_seen[role] = datetime.now(timezone.utc)
    return {"status": "ok", "role": role}


@app.get("/agents", response_model=list[AgentStatus])
async def get_agents() -> list[AgentStatus]:
    now = datetime.now(timezone.utc)
    # Load dynamic agent metadata for display_name / model
    dyn_meta: dict[str, dict] = {}
    if state.dynamic_agents:
        async with state.db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT role, display_name, model FROM dynamic_agents WHERE role = ANY($1)",
                state.dynamic_agents,
            )
        dyn_meta = {r["role"]: dict(r) for r in rows}

    result = []
    for role in ALL_AGENT_ROLES():
        last = state.agent_last_seen.get(role)
        if last is None:
            status = "offline"
        elif (now - last).total_seconds() < 60:
            status = "online"
        elif (now - last).total_seconds() < 300:
            status = "stale"
        else:
            status = "offline"
        meta = dyn_meta.get(role, {})
        result.append(AgentStatus(
            role=role,
            status=status,
            last_seen=last.isoformat() if last else None,
            display_name=meta.get("display_name", ""),
            model=meta.get("model", ""),
        ))
    return result


# ─────────────────────────────────────────────
# Phase 5: Task board endpoints
# ─────────────────────────────────────────────

@app.get("/tasks", response_model=list[TaskOut])
async def list_tasks(thread_id: str | None = Query(default=None)) -> list[TaskOut]:
    async with state.db.acquire() as conn:
        if thread_id:
            rows = await conn.fetch(
                "SELECT * FROM tasks WHERE thread_id=$1 ORDER BY created_at ASC", thread_id
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT 100"
            )
    return [_task_row_to_out(r) for r in rows]


@app.post("/tasks", response_model=TaskOut, status_code=201)
async def create_task_endpoint(body: TaskCreate) -> TaskOut:
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    async with state.db.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tasks(id, thread_id, title, description, assignee, status, created_by, created_at, updated_at)
            VALUES($1,$2,$3,$4,$5,'pending',$6,$7,$7)
            RETURNING *
            """,
            task_id, body.thread_id, body.title, body.description,
            body.assignee, body.created_by, now,
        )
    return _task_row_to_out(row)


@app.patch("/tasks/{task_id}", response_model=TaskOut)
async def update_task_endpoint(
    task_id: str, body: TaskUpdate, force: bool = Query(default=False)
) -> TaskOut:
    # CI quality gate: block marking done if the last CI run for this thread failed
    if body.status == "done" and not force:
        async with state.db.acquire() as conn:
            task_row = await conn.fetchrow("SELECT thread_id FROM tasks WHERE id=$1", task_id)
        if task_row:
            gate_open, reason = await _check_ci_gate(str(task_row["thread_id"]))
            if not gate_open:
                raise HTTPException(
                    status_code=422,
                    detail=f"CI quality gate: {reason}. Add ?force=true to override.",
                )

    now = datetime.now(timezone.utc)
    async with state.db.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE tasks SET status=$1, updated_at=$2 WHERE id=$3 RETURNING *",
            body.status, now, task_id,
        )
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    out = _task_row_to_out(row)
    if body.status == "done":
        asyncio.create_task(_run_ci_for_task(task_id=task_id, thread_id=out.thread_id))
        asyncio.create_task(_auto_complete_thread(out.thread_id))
        asyncio.create_task(_send_push_reminder(task_id=task_id, thread_id=out.thread_id, assignee=out.assignee))
    return out


def _task_row_to_out(r) -> TaskOut:
    return TaskOut(
        id=str(r["id"]),
        thread_id=str(r["thread_id"]),
        title=r["title"] or "",
        description=r["description"] or "",
        assignee=r["assignee"] or "",
        status=r["status"] or "pending",
        created_by=r["created_by"] or "",
        created_at=r["created_at"].isoformat(),
        updated_at=r["updated_at"].isoformat(),
    )


# ─────────────────────────────────────────────
# Phase 5: CI pipeline endpoints
# ─────────────────────────────────────────────

@app.post("/run-tests", response_model=CIResult, status_code=201)
async def run_tests_endpoint(task_id: str | None = None, thread_id: str | None = None) -> CIResult:
    return await _run_ci_for_task(task_id=task_id, thread_id=thread_id)


@app.get("/ci-results", response_model=list[CIResult])
async def list_ci_results(thread_id: str | None = Query(default=None)) -> list[CIResult]:
    async with state.db.acquire() as conn:
        if thread_id:
            rows = await conn.fetch(
                "SELECT * FROM ci_results WHERE thread_id=$1 ORDER BY ran_at DESC LIMIT 20",
                thread_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM ci_results ORDER BY ran_at DESC LIMIT 20"
            )
    return [_ci_row_to_out(r) for r in rows]


@app.get("/ci-results/latest", response_model=CIResult | None)
async def latest_ci_result() -> CIResult | None:
    async with state.db.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM ci_results ORDER BY ran_at DESC LIMIT 1")
    return _ci_row_to_out(row) if row else None


def _ci_row_to_out(r) -> CIResult:
    return CIResult(
        id=r["id"],
        task_id=r["task_id"],
        thread_id=r["thread_id"],
        passed=r["passed"],
        failed=r["failed"],
        total=r["total"],
        exit_code=r["exit_code"],
        output=r["output"] or "",
        ran_at=r["ran_at"].isoformat(),
    )


async def _run_ci_for_task(*, task_id: str | None, thread_id: str | None) -> CIResult:
    """Run the full test suite in the sandbox and persist the result."""
    logger.info("CI triggered: task=%s thread=%s", task_id, thread_id)
    passed = failed = total = 0
    output = ""
    exit_code = -1

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{SANDBOX_URL}/execute",
                json={"language": "pytest", "file_path": "tests/", "working_directory": ""},
            )
            resp.raise_for_status()
            data = resp.json()
            import re
            exit_code = data.get("exit_code", -1)
            full_stdout = data.get("stdout", "")
            full_stderr = data.get("stderr", "")
            # Parse counts from the full output before truncating
            combined = full_stdout + full_stderr
            m = re.search(r"(\d+) passed", combined)
            if m:
                passed = int(m.group(1))
            m = re.search(r"(\d+) failed", combined)
            if m:
                failed = int(m.group(1))
            total = passed + failed
            # Keep tail of output (summary line is at the end)
            output = (full_stdout[-6000:] + full_stderr[-2000:]).strip()
    except Exception as exc:
        logger.warning("CI run failed: %s", exc)
        output = str(exc)
        exit_code = -1

    now = datetime.now(timezone.utc)
    async with state.db.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO ci_results(task_id, thread_id, passed, failed, total, exit_code, output, ran_at)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8)
            RETURNING *
            """,
            task_id, thread_id, passed, failed, total, exit_code, output, now,
        )

    result = _ci_row_to_out(row)
    badge = "✅" if exit_code == 0 else "❌"
    # Fire webhook for external integrations
    asyncio.create_task(_fire_webhook(
        "ci.pass" if exit_code == 0 else "ci.fail",
        {"task_id": task_id, "thread_id": thread_id,
         "passed": passed, "failed": failed, "total": total, "exit_code": exit_code},
    ))
    # Publish CI result to audit stream so dashboard picks it up live
    ci_payload = {
        "id": str(uuid.uuid4()),
        "thread_id": thread_id or "",
        "from_role": "ci",
        "to_role": "orchestrator",
        "type": "ci_result",
        "content": f"{badge} CI: {passed}/{total} tests passed — task {(task_id or '')[:8]}",
        "priority": "normal",
        "artifacts": "[]",
        "parent_message_id": "",
        "timestamp": now.isoformat(),
        "metadata": json.dumps({"task_id": task_id, "exit_code": exit_code}),
    }
    await state.redis.xadd(AUDIT_STREAM, {k: str(v) for k, v in ci_payload.items()})
    logger.info("CI complete: %s/%s passed (exit=%s)", passed, total, exit_code)
    # Re-check thread auto-completion now that CI result is stored
    if thread_id:
        asyncio.create_task(_auto_complete_thread(thread_id))
    return result


# ─────────────────────────────────────────────
# Phase 6: Webhook, CI gate, thread auto-completion helpers
# ─────────────────────────────────────────────

async def _fire_webhook(event: str, data: dict) -> None:
    """POST a structured event to WEBHOOK_URL (best-effort, non-blocking)."""
    if not WEBHOOK_URL:
        return
    payload = {
        "event":     event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data":      data,
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(WEBHOOK_URL, json=payload)
        logger.info("Webhook fired: event=%s", event)
    except Exception as exc:
        logger.debug("Webhook failed (non-fatal): %s", exc)


async def _check_ci_gate(thread_id: str) -> tuple[bool, str]:
    """Return (gate_open, reason). Gate is open unless the last CI run for this thread failed."""
    async with state.db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT exit_code, passed, failed FROM ci_results WHERE thread_id=$1 ORDER BY ran_at DESC LIMIT 1",
            thread_id,
        )
    if row is None:
        return True, "no CI runs yet"
    if row["exit_code"] == 0:
        return True, f"{row['passed']} tests passing"
    return False, f"last CI run failed ({row['failed']} failure(s)) — fix tests before closing"


_PUSH_REMINDER_SKIP_ROLES = frozenset({"product_owner", "engineering_manager", "orchestrator", ""})

async def _send_push_reminder(task_id: str, thread_id: str, assignee: str) -> None:
    """After a dev marks a task done, remind them to push to GitHub if they haven't yet."""
    if not assignee or assignee in _PUSH_REMINDER_SKIP_ROLES:
        return
    try:
        async with state.db.acquire() as conn:
            thread_row = await conn.fetchrow(
                "SELECT github_repo FROM threads WHERE id=$1", thread_id
            )
        if not thread_row or not thread_row["github_repo"]:
            return
        github_repo = thread_row["github_repo"]
        repo_name = _repo_name_from_url(github_repo)

        msg_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        payload = {
            "id": msg_id,
            "thread_id": thread_id,
            "from_role": "orchestrator",
            "to_role": assignee,
            "type": "task_assignment",
            "content": (
                f"PUSH REMINDER — Your task ({task_id[:8]}) has been marked done.\n\n"
                f"If you have not already pushed your code to GitHub, do it NOW:\n"
                f"1. `git_commit` any uncommitted changes in /workspace\n"
                f"2. `git_push(repo_name='{repo_name}', subdirectory='<your_app_folder>')` "
                f"   to publish to {github_repo}\n\n"
                f"This is REQUIRED before EM can mark the thread complete. "
                f"If you already pushed, reply with `task_complete` confirming the push and you can ignore the rest."
            ),
            "priority": "high",
            "artifacts": "[]",
            "parent_message_id": "",
            "timestamp": now.isoformat(),
            "metadata": json.dumps({"source": "push_reminder", "task_id": task_id}),
        }
        inbox_key = f"agent:{assignee}:inbox"
        await state.redis.xadd(inbox_key, _encode(payload))
        logger.info("Sent push reminder to %s for task %s (repo: %s)", assignee, task_id[:8], repo_name)
    except Exception as exc:
        logger.warning("push_reminder failed for task %s: %s", task_id[:8], exc)


async def _auto_complete_thread(thread_id: str) -> bool:
    """Mark thread complete if every task is done and last CI passed. Returns True if completed."""
    async with state.db.acquire() as conn:
        counts = await conn.fetchrow(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE status = 'done') AS done_count
               FROM tasks WHERE thread_id = $1""",
            thread_id,
        )
        if not counts or counts["total"] == 0 or counts["total"] != counts["done_count"]:
            return False
        ci_row = await conn.fetchrow(
            "SELECT exit_code, passed, total, failed FROM ci_results WHERE thread_id=$1 ORDER BY ran_at DESC LIMIT 1",
            thread_id,
        )
        if ci_row is None:
            return False
        # CI passes if: exit_code==0, OR if tests ran with 0 failures and >0 passes
        # (exit_code=1 can occur from collection errors on unrelated test files)
        ci_passed = (
            ci_row["exit_code"] == 0
            or (ci_row["passed"] > 0 and (ci_row["failed"] or 0) == 0)
        )
        if not ci_passed:
            return False
        updated = await conn.fetchrow(
            "UPDATE threads SET status='complete', updated_at=NOW() WHERE id=$1 AND status != 'complete' RETURNING id, github_repo",
            thread_id,
        )
    if updated is None:
        return False  # already complete

    github_repo = updated["github_repo"] or ""

    # Safety net: if agents didn't push, orchestrator does it now via EM's inbox
    if github_repo:
        repo_name = _repo_name_from_url(github_repo)
        push_msg_id = str(uuid.uuid4())
        push_now = datetime.now(timezone.utc)
        push_payload = {
            "id": push_msg_id, "thread_id": thread_id,
            "from_role": "orchestrator", "to_role": "engineering_manager",
            "type": "task_assignment",
            "content": (
                f"FINAL STEP — push code to GitHub now.\n\n"
                f"All tasks are done and CI is green. Run `git_push` with repo_name=`{repo_name}` "
                f"to publish the final code to {github_repo}.\n"
                f"Then send a status_update back to orchestrator confirming the push."
            ),
            "priority": "high", "artifacts": "[]", "parent_message_id": "",
            "timestamp": push_now.isoformat(),
            "metadata": json.dumps({"source": "auto_complete", "repo_name": repo_name}),
        }
        await state.redis.xadd("agent:engineering_manager:inbox", _encode(push_payload))
        logger.info("Thread %s: sent git_push instruction to EM for repo %r", thread_id[:8], repo_name)

    repo_line   = f"\n🔗 GitHub: {github_repo}" if github_repo else ""

    now = datetime.now(timezone.utc)
    event_payload = {
        "id":               str(uuid.uuid4()),
        "thread_id":        thread_id,
        "from_role":        "orchestrator",
        "to_role":          "orchestrator",
        "type":             "thread_complete",
        "content":          f"✅ Thread {thread_id[:8]} complete — all tasks done, CI green{repo_line}",
        "priority":         "high",
        "artifacts":        "[]",
        "parent_message_id": "",
        "timestamp":        now.isoformat(),
        "metadata":         json.dumps({"passed": ci_row["passed"], "total": ci_row["total"], "github_repo": github_repo}),
    }
    await state.redis.xadd(AUDIT_STREAM, _encode(event_payload))
    asyncio.create_task(_fire_webhook("thread.complete", {
        "thread_id":   thread_id,
        "passed":      ci_row["passed"],
        "total":       ci_row["total"],
        "github_repo": github_repo,
    }))
    logger.info("Thread %s auto-completed (%d/%d tests). repo=%r", thread_id[:8], ci_row["passed"], ci_row["total"], github_repo)
    return True


# ─────────────────────────────────────────────
# Phase 6: Endpoints
# ─────────────────────────────────────────────

@app.get("/threads/{thread_id}/summary", response_model=ThreadSummaryDetail)
async def get_thread_summary(thread_id: str) -> ThreadSummaryDetail:
    async with state.db.acquire() as conn:
        thread = await conn.fetchrow(
            "SELECT id, title, status, created_at FROM threads WHERE id=$1", thread_id
        )
        if not thread:
            raise HTTPException(status_code=404, detail="Thread not found")
        counts = await conn.fetchrow(
            """SELECT COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE status = 'done')        AS done_count,
                      COUNT(*) FILTER (WHERE status = 'in_progress') AS in_progress_count
               FROM tasks WHERE thread_id = $1""",
            thread_id,
        )
        ci_last = await conn.fetchrow(
            "SELECT exit_code, passed, total FROM ci_results WHERE thread_id=$1 ORDER BY ran_at DESC LIMIT 1",
            thread_id,
        )
    return ThreadSummaryDetail(
        id=str(thread["id"]),
        title=thread["title"] or "",
        status=thread["status"],
        created_at=thread["created_at"].isoformat(),
        tasks_total=counts["total"] or 0,
        tasks_done=counts["done_count"] or 0,
        tasks_in_progress=counts["in_progress_count"] or 0,
        ci_last_exit_code=ci_last["exit_code"] if ci_last else None,
        ci_last_passed=ci_last["passed"] if ci_last else None,
        ci_last_total=ci_last["total"] if ci_last else None,
    )


@app.post("/webhooks/test")
async def test_webhook() -> dict:
    """Fire a test.ping event to the configured WEBHOOK_URL."""
    if not WEBHOOK_URL:
        return {"status": "skipped", "reason": "WEBHOOK_URL not configured"}
    await _fire_webhook("test.ping", {"message": "Team Claw webhook test"})
    return {"status": "fired", "url": WEBHOOK_URL}


@app.get("/threads/{thread_id}/budget", response_model=ThreadBudget)
async def get_thread_budget(thread_id: str) -> ThreadBudget:
    async with state.db.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM threads WHERE id=$1", thread_id)
    if not exists:
        raise HTTPException(404, "Thread not found")
    used = await _compute_thread_tokens(thread_id)
    budget = THREAD_BUDGET_TOKENS
    if budget == 0:
        return ThreadBudget(thread_id=thread_id, tokens_used=used, budget=0, pct_used=0.0, status="unlimited")
    pct = used / budget * 100
    status = "exceeded" if pct >= 100 else "warning" if pct >= 80 else "ok"
    return ThreadBudget(
        thread_id=thread_id, tokens_used=used, budget=budget,
        pct_used=round(pct, 1), status=status,
    )


@app.get("/standup", response_model=StandupReport)
async def get_standup(hours: int = Query(default=24)) -> StandupReport:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with state.db.acquire() as conn:
        active_threads = await conn.fetch(
            "SELECT t.id, t.title, t.status, COUNT(m.id) AS msg_count "
            "FROM threads t JOIN messages m ON m.thread_id=t.id "
            "WHERE m.created_at >= $1 GROUP BY t.id ORDER BY msg_count DESC LIMIT 10",
            cutoff,
        )
        tasks_done = await conn.fetch(
            "SELECT id, title, assignee, updated_at FROM tasks WHERE status='done' AND updated_at >= $1 ORDER BY updated_at DESC LIMIT 20",
            cutoff,
        )
        tasks_wip = await conn.fetch(
            "SELECT id, title, assignee FROM tasks WHERE status='in_progress' ORDER BY updated_at DESC LIMIT 10"
        )
        ci = await conn.fetchrow(
            "SELECT COUNT(*) FILTER (WHERE exit_code=0) AS passed, "
            "COUNT(*) FILTER (WHERE exit_code!=0) AS failed, COUNT(*) AS total "
            "FROM ci_results WHERE ran_at >= $1",
            cutoff,
        )
        msgs_by_agent = await conn.fetch(
            "SELECT from_role, COUNT(*) AS cnt FROM messages WHERE created_at >= $1 "
            "AND from_role != 'orchestrator' GROUP BY from_role ORDER BY cnt DESC",
            cutoff,
        )
        metrics = await conn.fetch(
            "SELECT model, SUM(input_tokens) AS inp, SUM(output_tokens) AS out "
            "FROM agent_metrics WHERE recorded_at >= $1 GROUP BY model",
            cutoff,
        )
        blockers = await conn.fetch(
            "SELECT id, content, from_role, thread_id, created_at FROM messages "
            "WHERE type='blocker' AND created_at >= $1 ORDER BY created_at DESC LIMIT 5",
            cutoff,
        )
    total_in = total_out = 0
    total_cost = 0.0
    for m in metrics:
        total_in  += m["inp"]
        total_out += m["out"]
        total_cost += _estimate_cost(m["model"], m["inp"], m["out"])
    return StandupReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        period_hours=hours,
        active_threads=[
            {"id": str(r["id"]), "title": r["title"], "status": r["status"], "messages": r["msg_count"]}
            for r in active_threads
        ],
        tasks_completed=[
            {"id": str(r["id"]), "title": r["title"], "assignee": r["assignee"],
             "completed_at": r["updated_at"].isoformat()}
            for r in tasks_done
        ],
        tasks_in_progress=[
            {"id": str(r["id"]), "title": r["title"], "assignee": r["assignee"]}
            for r in tasks_wip
        ],
        ci_summary={"passed": ci["passed"], "failed": ci["failed"], "total": ci["total"]},
        messages_by_agent={r["from_role"]: r["cnt"] for r in msgs_by_agent},
        token_cost={
            "total_input": total_in, "total_output": total_out,
            "estimated_cost_usd": round(total_cost, 4),
        },
        recent_blockers=[
            {"id": str(r["id"]), "from_role": r["from_role"], "thread_id": str(r["thread_id"]),
             "excerpt": r["content"][:120]}
            for r in blockers
        ],
    )


@app.post("/standup/publish")
async def publish_standup() -> dict:
    report = await get_standup(hours=24)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = f"Daily Standup — {date_str}"
    lines = [
        f"# {title}",
        f"_Generated at {report.generated_at} (last {report.period_hours}h)_\n",
        "## Active Threads",
    ]
    for t in report.active_threads:
        lines.append(f"- **{t['title']}** ({t['status']}, {t['messages']} msgs)")
    lines.append("\n## Tasks Completed")
    for t in report.tasks_completed:
        lines.append(f"- ✅ {t['title']} — _{t['assignee']}_")
    if not report.tasks_completed:
        lines.append("- _(none)_")
    lines.append("\n## In Progress")
    for t in report.tasks_in_progress:
        lines.append(f"- 🔄 {t['title']} — _{t['assignee']}_")
    if not report.tasks_in_progress:
        lines.append("- _(none)_")
    ci = report.ci_summary
    lines.append(f"\n## CI Summary\n{ci['passed']} passed / {ci['failed']} failed / {ci['total']} total")
    lines.append("\n## Message Activity")
    for role, cnt in report.messages_by_agent.items():
        lines.append(f"- {role}: {cnt} messages")
    cost = report.token_cost
    lines.append(
        f"\n## Token Cost\n"
        f"Input: {cost['total_input']:,} | Output: {cost['total_output']:,} | "
        f"Est. cost: **${cost['estimated_cost_usd']:.4f}**"
    )
    if report.recent_blockers:
        lines.append("\n## Recent Blockers")
        for b in report.recent_blockers:
            lines.append(f"- [{b['from_role']}] {b['excerpt']}")
    content = "\n".join(lines)
    async with state.db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO team_wiki(title, content, author, updated_at)
            VALUES($1,$2,'orchestrator',NOW())
            ON CONFLICT (title) DO UPDATE SET content=EXCLUDED.content, updated_at=NOW()
            """,
            title, content,
        )
    return {"status": "published", "title": title}


@app.get("/ci-results/trend", response_model=list[CITrendPoint])
async def ci_trend() -> list[CITrendPoint]:
    """Return last 20 CI runs for sparkline visualization."""
    async with state.db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT exit_code, passed, total, ran_at FROM ci_results ORDER BY ran_at DESC LIMIT 20"
        )
    return [
        CITrendPoint(
            exit_code=r["exit_code"],
            passed=r["passed"],
            total=r["total"],
            ran_at=r["ran_at"].isoformat(),
        )
        for r in reversed(rows)   # chronological order for the chart
    ]


@app.get("/stream/all")
async def stream_all():
    """Server-Sent Events stream of audit messages + ephemeral agent_working signals."""
    async def event_gen() -> AsyncGenerator[str, None]:
        last_audit_id    = "$"
        last_activity_id = "$"
        while True:
            result = await state.redis.xread(
                {AUDIT_STREAM: last_audit_id, ACTIVITY_STREAM: last_activity_id},
                count=50, block=3000,
            )
            if result:
                for stream_name, entries in result:
                    for redis_id, fields in entries:
                        if stream_name == AUDIT_STREAM.encode():
                            last_audit_id = redis_id
                        else:
                            last_activity_id = redis_id
                        decoded = _decode(fields)
                        yield f"data: {json.dumps(decoded)}\n\n"
            else:
                yield ": ping\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/", response_class=HTMLResponse)
@app.get("/home", response_class=HTMLResponse)
async def homepage():
    """Serve the Team Claw homepage."""
    f = pathlib.Path(__file__).parent / "home.html"
    if f.exists():
        return FileResponse(str(f), media_type="text/html")
    return HTMLResponse("<h1>Homepage not found</h1>", status_code=404)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serve the live dashboard."""
    dash_file = pathlib.Path(__file__).parent / "dashboard.html"
    if dash_file.exists():
        return FileResponse(str(dash_file), media_type="text/html")
    return HTMLResponse("<h1>Dashboard not found</h1>", status_code=404)


@app.get("/pitch", response_class=HTMLResponse)
async def pitch_deck():
    """Serve the Team Claw pitch deck."""
    pitch_file = pathlib.Path(__file__).parent / "pitch-deck.html"
    if pitch_file.exists():
        return FileResponse(str(pitch_file), media_type="text/html")
    return HTMLResponse("<h1>Pitch deck not found</h1>", status_code=404)


@app.get("/report", response_class=HTMLResponse)
async def report_page():
    """Serve the Executive Dashboard."""
    f = pathlib.Path(__file__).parent / "report.html"
    if f.exists():
        return FileResponse(str(f), media_type="text/html")
    return HTMLResponse("<h1>Report page not found</h1>", status_code=404)


@app.get("/report/summary")
async def report_summary() -> dict:
    """Single aggregation call for the executive dashboard."""
    now = datetime.now(timezone.utc)
    window_24h = now - timedelta(hours=24)
    seven_days_ago = now - timedelta(days=7)

    async with state.db.acquire() as conn:
        # KPIs
        active_count = await conn.fetchval(
            "SELECT COUNT(*) FROM threads WHERE status='active'"
        )
        tasks_done_today = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status='done' AND updated_at >= $1", window_24h
        )
        tasks_in_progress = await conn.fetchval(
            "SELECT COUNT(*) FROM tasks WHERE status='in_progress'"
        )
        ci_today = await conn.fetchrow(
            "SELECT COALESCE(SUM(passed),0) AS tests_passed, COUNT(*) AS runs "
            "FROM ci_results WHERE ran_at >= $1 AND exit_code=0", window_24h
        )
        # Total all-time cost across all models
        total_cost_row = await conn.fetch(
            "SELECT model, SUM(input_tokens) AS inp, SUM(output_tokens) AS out "
            "FROM agent_metrics GROUP BY model"
        )
        avg_ship_row = await conn.fetchrow(
            "SELECT AVG(EXTRACT(EPOCH FROM (updated_at - created_at))/60) AS avg_min "
            "FROM threads WHERE status='complete'"
        )

        # Active projects
        active_threads = await conn.fetch(
            "SELECT id, title, status, created_at, github_repo FROM threads "
            "WHERE status='active' ORDER BY created_at DESC LIMIT 20"
        )
        active_thread_ids = [str(r["id"]) for r in active_threads]

        # Task counts per active thread
        task_counts: dict = {}
        if active_thread_ids:
            tc_rows = await conn.fetch(
                "SELECT thread_id, "
                "  COUNT(*) AS total, "
                "  COUNT(*) FILTER (WHERE status='done') AS done "
                "FROM tasks WHERE thread_id = ANY($1) GROUP BY thread_id",
                active_thread_ids,
            )
            for r in tc_rows:
                task_counts[str(r["thread_id"])] = {"total": r["total"], "done": r["done"]}

        # Last CI per active thread
        ci_by_thread: dict = {}
        if active_thread_ids:
            ci_rows = await conn.fetch(
                "SELECT DISTINCT ON (thread_id) thread_id, exit_code, passed, total, ran_at "
                "FROM ci_results WHERE thread_id = ANY($1) ORDER BY thread_id, ran_at DESC",
                active_thread_ids,
            )
            for r in ci_rows:
                ci_by_thread[str(r["thread_id"])] = {
                    "exit_code": r["exit_code"],
                    "passed": r["passed"],
                    "total": r["total"],
                    "ran_at": r["ran_at"].isoformat(),
                }

        # Completed projects (last 20)
        completed_threads = await conn.fetch(
            "SELECT id, title, created_at, updated_at, github_repo FROM threads "
            "WHERE status='complete' ORDER BY updated_at DESC LIMIT 20"
        )
        completed_ids = [str(r["id"]) for r in completed_threads]
        completed_ci: dict = {}
        completed_cost: dict = {}
        if completed_ids:
            comp_ci = await conn.fetch(
                "SELECT DISTINCT ON (thread_id) thread_id, exit_code, passed, total "
                "FROM ci_results WHERE thread_id = ANY($1) ORDER BY thread_id, ran_at DESC",
                completed_ids,
            )
            for r in comp_ci:
                completed_ci[str(r["thread_id"])] = {
                    "exit_code": r["exit_code"],
                    "passed": r["passed"],
                    "total": r["total"],
                }
            cost_rows = await conn.fetch(
                "SELECT thread_id, model, SUM(input_tokens) AS inp, SUM(output_tokens) AS out "
                "FROM agent_metrics WHERE thread_id = ANY($1) GROUP BY thread_id, model",
                completed_ids,
            )
            for r in cost_rows:
                tid = str(r["thread_id"])
                completed_cost[tid] = completed_cost.get(tid, 0.0) + _estimate_cost(
                    r["model"], r["inp"], r["out"]
                )

        # Agent status with current thread
        msgs_today: dict = {}
        mt_rows = await conn.fetch(
            "SELECT from_role, COUNT(*) AS cnt FROM messages WHERE created_at >= $1 "
            "AND from_role != 'orchestrator' GROUP BY from_role", window_24h
        )
        for r in mt_rows:
            msgs_today[r["from_role"]] = r["cnt"]

        # Velocity: tasks done per day last 7 days
        velocity_rows = await conn.fetch(
            "SELECT DATE(updated_at AT TIME ZONE 'UTC') AS day, COUNT(*) AS cnt "
            "FROM tasks WHERE status='done' AND updated_at >= $1 "
            "GROUP BY day ORDER BY day ASC", seven_days_ago
        )

        # Cost by role
        cost_role_rows = await conn.fetch(
            "SELECT agent_role, model, SUM(input_tokens) AS inp, SUM(output_tokens) AS out "
            "FROM agent_metrics GROUP BY agent_role, model"
        )

        # Recent activity (last 30 audit messages as human-readable)
        activity_rows = await conn.fetch(
            "SELECT type, from_role, to_role, content, thread_id, created_at "
            "FROM messages ORDER BY created_at DESC LIMIT 30"
        )

    # Compute total all-time cost
    cost_today = 0.0
    for r in total_cost_row:
        cost_today += _estimate_cost(r["model"], r["inp"], r["out"])

    # Agent online status
    agents_online = sum(
        1 for role in ALL_AGENT_ROLES()
        if role in state.agent_last_seen
        and (now - state.agent_last_seen[role]).total_seconds() < 60
    )

    # Build active projects list
    active_projects = []
    for t in active_threads:
        tid = str(t["id"])
        tc = task_counts.get(tid, {"total": 0, "done": 0})
        ci = ci_by_thread.get(tid)
        last_act = state.thread_last_activity.get(tid, {})
        elapsed_min = int((now - t["created_at"].replace(tzinfo=timezone.utc)).total_seconds() / 60)
        active_projects.append({
            "id": tid,
            "title": t["title"] or "",
            "elapsed_min": elapsed_min,
            "tasks_total": tc["total"],
            "tasks_done": tc["done"],
            "ci": ci,
            "github_repo": t["github_repo"] or "",
            "active_agent": last_act.get("role", ""),
        })

    # Build completed projects list
    completed_projects = []
    for t in completed_threads:
        tid = str(t["id"])
        created = t["created_at"].replace(tzinfo=timezone.utc)
        updated = t["updated_at"].replace(tzinfo=timezone.utc)
        duration_min = int((updated - created).total_seconds() / 60)
        completed_projects.append({
            "id": tid,
            "title": t["title"] or "",
            "completed_at": updated.isoformat(),
            "duration_min": duration_min,
            "github_repo": t["github_repo"] or "",
            "ci": completed_ci.get(tid),
            "cost_usd": round(completed_cost.get(tid, 0.0), 4),
        })

    # Agent status list
    dyn_meta: dict = {}
    if state.dynamic_agents:
        async with state.db.acquire() as conn:
            dm_rows = await conn.fetch(
                "SELECT role, display_name, model FROM dynamic_agents WHERE role = ANY($1)",
                state.dynamic_agents,
            )
        dyn_meta = {r["role"]: dict(r) for r in dm_rows}

    agent_status = []
    for role in ALL_AGENT_ROLES():
        last = state.agent_last_seen.get(role)
        if last is None:
            status = "offline"
        elif (now - last).total_seconds() < 60:
            status = "online"
        elif (now - last).total_seconds() < 300:
            status = "stale"
        else:
            status = "offline"
        meta = dyn_meta.get(role, {})
        current_thread_id = ""
        for tid, act in state.thread_last_activity.items():
            if act.get("role") == role:
                current_thread_id = tid
                break
        agent_status.append({
            "role": role,
            "status": status,
            "last_seen": last.isoformat() if last else None,
            "display_name": meta.get("display_name", role.replace("_", " ").title()),
            "model": meta.get("model", ""),
            "messages_today": msgs_today.get(role, 0),
            "current_thread": current_thread_id,
        })

    # Velocity by day (last 7 days)
    velocity_map: dict = {}
    for i in range(7):
        day = (now - timedelta(days=6 - i)).date()
        velocity_map[str(day)] = 0
    for r in velocity_rows:
        velocity_map[str(r["day"])] = r["cnt"]
    velocity_by_day = [{"day": k, "count": v} for k, v in sorted(velocity_map.items())]

    # Cost by role
    role_cost_map: dict = {}
    for r in cost_role_rows:
        role = r["agent_role"]
        role_cost_map[role] = role_cost_map.get(role, 0.0) + _estimate_cost(
            r["model"], r["inp"], r["out"]
        )
    cost_by_role = sorted(
        [{"role": k, "cost_usd": round(v, 4)} for k, v in role_cost_map.items()],
        key=lambda x: x["cost_usd"],
        reverse=True,
    )

    # Recent activity as human-readable strings
    def _humanize(row) -> str:
        t = row["type"]
        fr = row["from_role"] or ""
        content = (row["content"] or "")[:80]
        name = fr.replace("_", " ").title()
        if t == "task_complete":
            return f"✅ {name} finished a task"
        elif t == "ci_result":
            return f"🧪 {content}"
        elif t == "thread_complete":
            return f"🚀 Shipped: {content[:60]}"
        elif t == "blocker":
            return f"⚠️ {name} is blocked"
        elif t == "human_question":
            return f"💬 {name} needs your input"
        elif t == "agent_created":
            return f"🤖 New agent joined"
        elif t == "task_assignment":
            return f"📋 {name} received a task"
        elif t == "status_update":
            return f"📊 {name}: {content[:60]}"
        elif t == "budget_warning":
            return f"🔶 {content[:60]}"
        elif t == "budget_exceeded":
            return f"🔴 {content[:60]}"
        else:
            return f"💬 {name}: {content[:60]}"

    recent_activity = []
    for row in activity_rows:
        recent_activity.append({
            "text": _humanize(row),
            "type": row["type"],
            "ts": row["created_at"].isoformat(),
            "thread_id": str(row["thread_id"]) if row["thread_id"] else "",
        })

    avg_ship = avg_ship_row["avg_min"] if avg_ship_row and avg_ship_row["avg_min"] else 0

    return {
        "kpis": {
            "active_threads": active_count or 0,
            "agents_online": agents_online,
            "tasks_done_today": tasks_done_today or 0,
            "tasks_in_progress": tasks_in_progress or 0,
            "tests_passed_today": int(ci_today["tests_passed"]) if ci_today else 0,
            "cost_today_usd": round(cost_today, 4),
            "avg_ship_time_minutes": round(float(avg_ship), 1),
        },
        "active_projects": active_projects,
        "completed_projects": completed_projects,
        "agent_status": agent_status,
        "velocity": {"by_day": velocity_by_day},
        "cost_by_role": cost_by_role,
        "recent_activity": recent_activity,
    }


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "orchestrator"}


# ─────────────────────────────────────────────
# Phase 8: Tool execution telemetry endpoints
# ─────────────────────────────────────────────

def _tool_row_to_out(r) -> ToolExecutionOut:
    return ToolExecutionOut(
        id=r["id"],
        agent_role=r["agent_role"],
        tool_name=r["tool_name"],
        thread_id=r["thread_id"],
        duration_ms=r["duration_ms"],
        success=r["success"],
        error=r["error"] or "",
        executed_at=r["executed_at"].isoformat(),
    )


@app.post("/tool-executions", status_code=201)
async def record_tool_execution(rec: ToolExecutionRecord) -> dict:
    async with state.db.acquire() as conn:
        await conn.execute(
            "INSERT INTO tool_executions(agent_role, tool_name, thread_id, duration_ms, success, error) "
            "VALUES($1,$2,$3,$4,$5,$6)",
            rec.agent_role, rec.tool_name, rec.thread_id,
            rec.duration_ms, rec.success, rec.error or "",
        )
    return {"status": "recorded"}


@app.get("/tool-history", response_model=list[ToolExecutionOut])
async def get_tool_history(
    agent: str | None = Query(default=None),
    tool: str | None = Query(default=None),
    thread_id: str | None = Query(default=None),
    limit: int = Query(default=50, le=500),
) -> list[ToolExecutionOut]:
    conditions: list[str] = []
    params: list = []
    if agent:
        conditions.append(f"agent_role=${len(params)+1}")
        params.append(agent)
    if tool:
        conditions.append(f"tool_name=${len(params)+1}")
        params.append(tool)
    if thread_id:
        conditions.append(f"thread_id=${len(params)+1}")
        params.append(thread_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    async with state.db.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM tool_executions {where} ORDER BY executed_at DESC LIMIT ${len(params)}",
            *params,
        )
    return [_tool_row_to_out(r) for r in rows]


@app.get("/tool-history/stats", response_model=list[ToolStats])
async def get_tool_stats() -> list[ToolStats]:
    async with state.db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT tool_name,
                   COUNT(*)                                              AS total_calls,
                   AVG(CASE WHEN success THEN 1.0 ELSE 0.0 END)        AS success_rate,
                   AVG(duration_ms)                                      AS avg_duration,
                   PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_duration
            FROM tool_executions
            GROUP BY tool_name
            ORDER BY total_calls DESC
        """)
    return [
        ToolStats(
            tool_name=r["tool_name"],
            total_calls=r["total_calls"],
            success_rate=round(float(r["success_rate"]), 3),
            avg_duration_ms=round(float(r["avg_duration"]), 1),
            p95_duration_ms=round(float(r["p95_duration"]), 1),
        )
        for r in rows
    ]


# ─────────────────────────────────────────────
# Phase 8: Thread close endpoint
# ─────────────────────────────────────────────

@app.post("/threads/{thread_id}/close")
async def close_thread(thread_id: str, force: bool = Query(default=False)) -> dict:
    async with state.db.acquire() as conn:
        # Guard: block close if tasks are still incomplete (unless force=true)
        if not force:
            counts = await conn.fetchrow(
                """SELECT COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE status != 'done') AS incomplete
                   FROM tasks WHERE thread_id = $1""",
                thread_id,
            )
            if counts and counts["total"] > 0 and counts["incomplete"] > 0:
                raise HTTPException(
                    409,
                    f"Cannot close: {counts['incomplete']} task(s) not yet done. "
                    f"Complete all tasks first, or add ?force=true to override.",
                )
        row = await conn.fetchrow(
            "UPDATE threads SET status='closed', updated_at=NOW() "
            "WHERE id=$1 AND status IN ('active', 'waiting') RETURNING id",
            thread_id,
        )
    if not row:
        raise HTTPException(404, "Thread not found or not active/waiting")
    now = datetime.now(timezone.utc)
    payload = {
        "id": str(uuid.uuid4()), "thread_id": thread_id,
        "from_role": "orchestrator", "to_role": "orchestrator",
        "type": "thread_closed",
        "content": f"🔒 Thread {thread_id[:8]} manually closed",
        "priority": "normal", "artifacts": "[]", "parent_message_id": "",
        "timestamp": now.isoformat(), "metadata": "{}",
    }
    await state.redis.xadd(AUDIT_STREAM, _encode(payload))
    asyncio.create_task(_fire_webhook("thread.closed", {"thread_id": thread_id}))
    return {"status": "closed", "thread_id": thread_id}


@app.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str) -> dict:
    """Permanently delete a thread and all its messages, tasks, CI results, and human questions."""
    async with state.db.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM threads WHERE id=$1", thread_id)
        if not row:
            raise HTTPException(404, "Thread not found")
        await conn.execute("DELETE FROM human_questions WHERE thread_id=$1", thread_id)
        await conn.execute("DELETE FROM ci_results WHERE thread_id=$1", thread_id)
        await conn.execute("DELETE FROM tasks WHERE thread_id=$1", thread_id)
        await conn.execute("DELETE FROM messages WHERE thread_id=$1", thread_id)
        await conn.execute("DELETE FROM threads WHERE id=$1", thread_id)
    # Remove from Redis audit stream entries is best-effort (streams don't support selective delete)
    asyncio.create_task(_fire_webhook("thread.deleted", {"thread_id": thread_id}))
    return {"status": "deleted", "thread_id": thread_id}


# ─────────────────────────────────────────────
# Phase 9: Human-in-the-Loop endpoints
# ─────────────────────────────────────────────

@app.post("/threads/{thread_id}/ask-human", status_code=201)
async def ask_human_endpoint(thread_id: str, req: AskHumanRequest) -> dict:
    """Called by an agent tool to submit a question for the human; sets thread to 'waiting'."""
    async with state.db.acquire() as conn:
        if not await conn.fetchrow("SELECT id FROM threads WHERE id=$1", thread_id):
            raise HTTPException(404, "Thread not found")
        row = await conn.fetchrow(
            "INSERT INTO human_questions(thread_id, from_role, question, context) "
            "VALUES($1,$2,$3,$4) RETURNING id",
            thread_id, req.from_role, req.question, req.context,
        )
        question_id = row["id"]
        await conn.execute(
            "UPDATE threads SET status='waiting', updated_at=NOW() WHERE id=$1", thread_id
        )
    now = datetime.now(timezone.utc)
    payload = {
        "id": str(uuid.uuid4()), "thread_id": thread_id,
        "from_role": req.from_role, "to_role": "human",
        "type": "human_question",
        "content": req.question,
        "priority": "high", "artifacts": "[]", "parent_message_id": "",
        "timestamp": now.isoformat(),
        "metadata": json.dumps({"question_id": question_id, "context": req.context}),
    }
    await state.redis.xadd(AUDIT_STREAM, _encode(payload))
    asyncio.create_task(_fire_webhook("thread.waiting", {"thread_id": thread_id, "question": req.question}))
    return {"question_id": question_id, "status": "waiting"}


@app.post("/threads/{thread_id}/human-reply")
async def human_reply(thread_id: str, req: HumanReplyRequest) -> dict:
    """Human answers a pending question; resumes the thread."""
    async with state.db.acquire() as conn:
        if not await conn.fetchrow("SELECT id FROM threads WHERE id=$1", thread_id):
            raise HTTPException(404, "Thread not found")
        await conn.execute(
            """UPDATE human_questions SET answered=TRUE, answer=$1, answered_at=NOW()
               WHERE thread_id=$2 AND NOT answered""",
            req.message, thread_id,
        )
        await conn.execute(
            "UPDATE threads SET status='active', updated_at=NOW() WHERE id=$1", thread_id
        )
    now = datetime.now(timezone.utc)
    message_id = str(uuid.uuid4())
    payload = {
        "id": message_id, "thread_id": thread_id,
        "from_role": "human", "to_role": req.target_role,
        "type": "human_input",
        "content": req.message,
        "priority": "high", "artifacts": "[]", "parent_message_id": "",
        "timestamp": now.isoformat(),
        "metadata": json.dumps({"source": "human_reply"}),
    }
    await state.redis.xadd(f"agent:{req.target_role}:inbox", _encode(payload))
    await state.redis.xadd(AUDIT_STREAM, _encode(payload))
    asyncio.create_task(_fire_webhook("thread.resumed", {"thread_id": thread_id, "target_role": req.target_role}))
    return {"status": "reply_sent", "message_id": message_id, "target_role": req.target_role}


@app.get("/pending-questions", response_model=list[HumanQuestionOut])
async def get_pending_questions(thread_id: str | None = Query(default=None)) -> list[HumanQuestionOut]:
    """Return all unanswered human questions, optionally filtered by thread."""
    async with state.db.acquire() as conn:
        if thread_id:
            rows = await conn.fetch(
                "SELECT * FROM human_questions WHERE thread_id=$1 AND NOT answered ORDER BY created_at DESC",
                thread_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM human_questions WHERE NOT answered ORDER BY created_at DESC"
            )
    return [_hq_row_to_out(r) for r in rows]


@app.get("/threads/{thread_id}/pending-questions", response_model=list[HumanQuestionOut])
async def get_thread_pending_questions(thread_id: str) -> list[HumanQuestionOut]:
    """Return unanswered human questions for a specific thread."""
    async with state.db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM human_questions WHERE thread_id=$1 AND NOT answered ORDER BY created_at DESC",
            thread_id,
        )
    return [_hq_row_to_out(r) for r in rows]


# ─────────────────────────────────────────────
# Phase 8 table migration (idempotent)
# ─────────────────────────────────────────────

async def _ensure_phase8_tables() -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS tool_executions (
        id          BIGSERIAL   PRIMARY KEY,
        agent_role  TEXT        NOT NULL,
        tool_name   TEXT        NOT NULL,
        thread_id   TEXT,
        duration_ms INTEGER     NOT NULL DEFAULT 0,
        success     BOOLEAN     NOT NULL DEFAULT TRUE,
        error       TEXT        DEFAULT '',
        executed_at TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_tool_exec_agent ON tool_executions(agent_role);
    CREATE INDEX IF NOT EXISTS idx_tool_exec_tool  ON tool_executions(tool_name);
    CREATE INDEX IF NOT EXISTS idx_tool_exec_time  ON tool_executions(executed_at DESC);
    """
    async with state.db.acquire() as conn:
        await conn.execute(ddl)
    logger.info("Phase 8 tables ensured.")


# ─────────────────────────────────────────────
# Phase 9 table migration (idempotent)
# ─────────────────────────────────────────────

async def _ensure_phase9_tables() -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS human_questions (
        id          BIGSERIAL   PRIMARY KEY,
        thread_id   TEXT        NOT NULL,
        from_role   TEXT        NOT NULL,
        question    TEXT        NOT NULL,
        context     TEXT        DEFAULT '',
        answered    BOOLEAN     NOT NULL DEFAULT FALSE,
        answer      TEXT        DEFAULT '',
        answered_at TIMESTAMPTZ,
        created_at  TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_hq_thread     ON human_questions(thread_id);
    CREATE INDEX IF NOT EXISTS idx_hq_unanswered ON human_questions(answered) WHERE NOT answered;
    """
    async with state.db.acquire() as conn:
        await conn.execute(ddl)
    logger.info("Phase 9 tables ensured.")


async def _ensure_phase10_tables() -> None:
    """Phase 10: add github_repo column to threads (idempotent)."""
    ddl = "ALTER TABLE threads ADD COLUMN IF NOT EXISTS github_repo TEXT DEFAULT '';"
    async with state.db.acquire() as conn:
        await conn.execute(ddl)
    logger.info("Phase 10 migration: github_repo column ensured.")


async def _ensure_dynamic_agents_table() -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS dynamic_agents (
        role         TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        description  TEXT DEFAULT '',
        model        TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
        container_id TEXT DEFAULT '',
        created_at   TIMESTAMPTZ DEFAULT NOW()
    );
    """
    async with state.db.acquire() as conn:
        await conn.execute(ddl)
    logger.info("Dynamic agents table ensured.")


async def _load_dynamic_agents() -> None:
    async with state.db.acquire() as conn:
        rows = await conn.fetch("SELECT role FROM dynamic_agents ORDER BY created_at")
    state.dynamic_agents = [r["role"] for r in rows]
    if state.dynamic_agents:
        logger.info("Loaded dynamic agents: %s", state.dynamic_agents)


# ─────────────────────────────────────────────
# Dynamic agent creation helpers
# ─────────────────────────────────────────────

def _slugify_role(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug


_UX_SYSTEM_PROMPT_EXAMPLE = """# UX Engineer — Team Claw
You are the UX Engineer for Team Claw. You translate product requirements into
developer-ready design artifacts (wireframes, user flows, component specs).
You write design docs to /workspace/designs/{feature}-ux.md before any UI code
is written. You use send_message for all communication. You save lessons to
memory after every task."""

_UX_CONFIG_EXAMPLE = """ALLOWED_TOOLS = [
    "send_message", "write_file", "read_file", "list_files", "edit_file",
    "search_code", "find_files", "wiki_write", "wiki_read",
    "write_memory", "read_memory", "list_memories",
    "create_task", "update_task_status", "check_budget", "ask_human",
]
AVAILABLE_ROLES = [
    "orchestrator", "engineering_manager", "product_owner",
    "architect", "senior_dev_1", "senior_dev_2",
]"""

ALL_AVAILABLE_TOOLS = [
    "send_message", "write_file", "read_file", "list_files", "edit_file",
    "execute_code", "search_code", "find_files",
    "git_status", "git_commit", "git_push", "git_diff",
    "git_checkout_branch", "git_merge",
    "wiki_write", "wiki_read", "wiki_search",
    "write_memory", "read_memory", "list_memories",
    "create_task", "update_task_status", "check_budget", "ask_human",
]


async def _generate_agent_files(role: str, display_name: str, description: str, model: str) -> tuple[str, str]:
    """Use Claude to generate system_prompt.md and config.py for a new agent."""
    client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    all_roles = ALL_AGENT_ROLES() + [role]

    prompt = f"""You are creating configuration files for a new AI agent joining the Team Claw autonomous software development team.

Team Claw is a multi-agent system where 8+ Claude agents each play a software engineering role. They communicate via Redis message bus using structured JSON messages. Each agent has:
1. system_prompt.md — defines identity, responsibilities, workflow, memory patterns
2. config.py — ALLOWED_TOOLS list and AVAILABLE_ROLES list

New agent to create:
- Display Name: {display_name}
- Role slug: {role}
- Description: {description}
- Model that will run this agent: {model}

Available tools they can be assigned from: {json.dumps(ALL_AVAILABLE_TOOLS)}
Available roles they can communicate with: {json.dumps(all_roles)}

Example of a well-designed system_prompt.md (UX Engineer):
{_UX_SYSTEM_PROMPT_EXAMPLE}

Example config.py (UX Engineer):
{_UX_CONFIG_EXAMPLE}

Key conventions:
- All communication MUST use send_message tool
- Agents save lessons to memory after every task (write_memory/read_memory/list_memories)
- Tool execute_code and git_* tools only for dev/engineering roles that actually write/run code
- Send task_complete to engineering_manager when work is done
- Keep messages brief; put detail in files (/workspace/)
- Memory key format: "pattern:<type>:<name>", "lesson:<topic>:<name>", "mistake:<type>:<name>"

Generate a high-quality system_prompt.md and config.py for the {display_name} role."""

    tools = [{
        "name": "generate_agent_config",
        "description": "Output the two agent configuration files",
        "input_schema": {
            "type": "object",
            "properties": {
                "system_prompt_md": {
                    "type": "string",
                    "description": "Full content of system_prompt.md — identity, responsibilities, workflow, memory patterns"
                },
                "config_py": {
                    "type": "string",
                    "description": "Full content of config.py — exactly ALLOWED_TOOLS list and AVAILABLE_ROLES list as Python"
                },
            },
            "required": ["system_prompt_md", "config_py"],
        },
    }]

    resp = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        tools=tools,
        tool_choice={"type": "tool", "name": "generate_agent_config"},
        messages=[{"role": "user", "content": prompt}],
    )

    for block in resp.content:
        if block.type == "tool_use" and block.name == "generate_agent_config":
            return block.input["system_prompt_md"], block.input["config_py"]

    raise RuntimeError("Claude did not return agent config files")


def _start_agent_container_sync(role: str, display_name: str, system_prompt: str, config_py: str, model: str) -> str:
    """Spin up a new agent container using the Docker socket. Returns container ID."""
    client = docker_sdk.from_env()

    # Detect network and workspace volume from the orchestrator container itself
    own_id = pathlib.Path("/etc/hostname").read_text().strip()
    try:
        own_container = client.containers.get(own_id)
        networks = list(own_container.attrs["NetworkSettings"]["Networks"].keys())
        network_name = networks[0] if networks else "team-claw_team-claw"
        mounts = own_container.attrs.get("Mounts", [])
        workspace_vol = next(
            (m["Name"] for m in mounts if m.get("Destination") == "/workspace" and m.get("Type") == "volume"),
            "team-claw_workspace",
        )
    except Exception:
        network_name = "team-claw_team-claw"
        workspace_vol = "team-claw_workspace"

    container_name = f"team-claw-{role.replace('_', '-')}-1"

    # Remove stale container with same name if it exists
    try:
        old = client.containers.get(container_name)
        old.remove(force=True)
    except docker_sdk.errors.NotFound:
        pass

    env = {
        "ROLE": role,
        "MODEL": model,
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "REDIS_URL": "redis://redis:6379",
        "ORCHESTRATOR_URL": "http://orchestrator:8080",
        "SANDBOX_URL": "http://sandbox:8081",
        "GITHUB_TOKEN": GITHUB_TOKEN,
        "GITHUB_USERNAME": GITHUB_USERNAME,
    }

    container = client.containers.create(
        image=AGENT_BASE_IMAGE,
        name=container_name,
        environment=env,
        network=network_name,
        volumes={workspace_vol: {"bind": "/workspace", "mode": "rw"}},
        restart_policy={"Name": "unless-stopped"},
        detach=True,
        labels={"team.claw.dynamic": "true", "team.claw.role": role, "team.claw.display": display_name},
    )

    # Inject system_prompt.md and config.py into /agent/ inside the (stopped) container
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        for filename, content in [("system_prompt.md", system_prompt), ("config.py", config_py)]:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=filename)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    tar_buf.seek(0)
    container.put_archive("/", tar_buf)   # Docker creates /agent/ if missing

    # Inject into /agent explicitly by creating the dir first via a second archive
    tar_buf2 = io.BytesIO()
    with tarfile.open(fileobj=tar_buf2, mode="w") as tar:
        dir_info = tarfile.TarInfo(name="agent")
        dir_info.type = tarfile.DIRTYPE
        dir_info.mode = 0o755
        tar.addfile(dir_info)
        for filename, content in [("agent/system_prompt.md", system_prompt), ("agent/config.py", config_py)]:
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=filename)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    tar_buf2.seek(0)
    container.put_archive("/", tar_buf2)

    container.start()
    return container.id


# ─────────────────────────────────────────────
# POST /agents/create  — streaming SSE response
# ─────────────────────────────────────────────

@app.post("/agents/create")
async def create_agent(body: CreateAgentRequest) -> StreamingResponse:
    role = _slugify_role(body.display_name)

    if role in ALL_AGENT_ROLES():
        raise HTTPException(409, f"Agent role '{role}' already exists")
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not set in orchestrator environment")

    async def _stream():
        def _sse(data: dict) -> str:
            return f"data: {json.dumps(data)}\n\n"

        try:
            yield _sse({"step": "generating_prompt", "message": f"Generating system prompt for {body.display_name}…"})

            system_prompt, config_py = await _generate_agent_files(
                role, body.display_name, body.description, body.model
            )

            yield _sse({"step": "generating_config", "message": "Agent config generated ✓"})
            yield _sse({"step": "starting_container", "message": "Starting Docker container…"})

            container_id = await asyncio.get_event_loop().run_in_executor(
                None,
                _start_agent_container_sync,
                role, body.display_name, system_prompt, config_py, body.model,
            )

            # Persist to DB
            async with state.db.acquire() as conn:
                await conn.execute(
                    """INSERT INTO dynamic_agents (role, display_name, description, model, container_id)
                       VALUES ($1, $2, $3, $4, $5)
                       ON CONFLICT (role) DO UPDATE
                       SET display_name=$2, description=$3, model=$4, container_id=$5""",
                    role, body.display_name, body.description, body.model, container_id[:12],
                )
            state.dynamic_agents.append(role)

            # Broadcast agent_created SSE to all connected dashboard clients
            event_payload = json.dumps({
                "type": "agent_created",
                "role": role,
                "display_name": body.display_name,
                "model": body.model,
            })
            await state.redis.xadd(
                AUDIT_STREAM,
                {"payload": event_payload},
                maxlen=10000,
            )

            yield _sse({
                "step": "done",
                "role": role,
                "display_name": body.display_name,
                "model": body.model,
                "container_id": container_id[:12],
                "message": f"🤖 {body.display_name} joined the team!",
            })

        except Exception as exc:
            logger.exception("Failed to create agent %s", role)
            yield _sse({"step": "error", "message": str(exc)})

    return StreamingResponse(_stream(), media_type="text/event-stream")


# ─────────────────────────────────────────────
# GitHub helpers
# ─────────────────────────────────────────────

def _slugify_repo_name(title: str) -> str:
    """Convert a task title to a valid GitHub repo name."""
    import re
    slug = re.sub(r"[^a-zA-Z0-9._-]", "-", title).lower()
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    return slug[:100] or "team-claw-task"


def _repo_name_from_url(url: str) -> str:
    """Extract repo name from a github.com URL."""
    return url.rstrip("/").split("/")[-1] if url else ""


async def _create_github_repo(repo_name: str) -> str:
    """Create a GitHub repo and return its HTML URL. Returns '' if token not set or on error."""
    if not GITHUB_TOKEN:
        logger.warning("GITHUB_TOKEN not set — skipping repo creation for %r", repo_name)
        return ""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://api.github.com/user/repos",
                json={
                    "name": repo_name,
                    "private": False,
                    "auto_init": True,
                    "description": "Built by Team Claw AI agents",
                },
                headers={
                    "Authorization": f"token {GITHUB_TOKEN}",
                    "Accept":        "application/vnd.github.v3+json",
                    "User-Agent":    "TeamClaw/1.0",
                },
            )
            if resp.status_code == 201:
                return resp.json().get("html_url", "")
            if resp.status_code == 422:   # already exists
                return f"https://github.com/{GITHUB_USERNAME}/{repo_name}"
            logger.warning("GitHub repo creation returned %d for %r", resp.status_code, repo_name)
            return ""
    except Exception as exc:
        logger.warning("GitHub repo creation failed for %r: %s", repo_name, exc)
        return ""


def _hq_row_to_out(row) -> HumanQuestionOut:
    return HumanQuestionOut(
        id=row["id"], thread_id=row["thread_id"], from_role=row["from_role"],
        question=row["question"], context=row["context"] or "",
        answered=row["answered"], answer=row["answer"] or "",
        created_at=row["created_at"].isoformat(),
        answered_at=row["answered_at"].isoformat() if row["answered_at"] else None,
    )


# ─────────────────────────────────────────────
# Phase 8: Idle monitor background task
# ─────────────────────────────────────────────

async def _idle_monitor_loop() -> None:
    """Fire thread_idle events for active threads silent for IDLE_THREAD_MINUTES."""
    if IDLE_THREAD_MINUTES <= 0:
        return
    logger.info("Idle monitor started (threshold=%d min).", IDLE_THREAD_MINUTES)
    while True:
        await asyncio.sleep(60)
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=IDLE_THREAD_MINUTES)
            async with state.db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT t.id, t.title, MAX(m.created_at) AS last_msg
                    FROM threads t
                    LEFT JOIN messages m ON m.thread_id = t.id
                    WHERE t.status = 'active'
                    GROUP BY t.id, t.title
                    HAVING MAX(m.created_at) < $1 OR MAX(m.created_at) IS NULL
                    """,
                    cutoff,
                )
            now = datetime.now(timezone.utc)
            for row in rows:
                tid = str(row["id"])
                if tid in state.idle_alerts_fired:
                    continue
                state.idle_alerts_fired.add(tid)
                last_msg = row["last_msg"]
                idle_mins = int((now - last_msg).total_seconds() / 60) if last_msg else 999
                payload = {
                    "id": str(uuid.uuid4()), "thread_id": tid,
                    "from_role": "orchestrator", "to_role": "orchestrator",
                    "type": "thread_idle",
                    "content": f"⏰ Thread {tid[:8]} idle for {idle_mins}m — \"{row['title'] or tid[:8]}\"",
                    "priority": "normal", "artifacts": "[]", "parent_message_id": "",
                    "timestamp": now.isoformat(), "metadata": "{}",
                }
                await state.redis.xadd(AUDIT_STREAM, _encode(payload))
                asyncio.create_task(_fire_webhook("thread.idle", {
                    "thread_id": tid, "idle_minutes": idle_mins,
                }))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Idle monitor error: %s", exc)


# ─────────────────────────────────────────────
# Phase 4 table migration (idempotent)
# ─────────────────────────────────────────────

async def _ensure_phase4_tables() -> None:
    """Create Phase 4 tables if they don't exist yet (safe to re-run)."""
    ddl = """
    CREATE TABLE IF NOT EXISTS agent_memories (
        agent_role  TEXT        NOT NULL,
        key         TEXT        NOT NULL,
        value       TEXT        NOT NULL,
        updated_at  TIMESTAMPTZ DEFAULT NOW(),
        PRIMARY KEY (agent_role, key)
    );
    CREATE TABLE IF NOT EXISTS team_wiki (
        title       TEXT        PRIMARY KEY,
        content     TEXT        NOT NULL,
        author      TEXT        NOT NULL DEFAULT 'unknown',
        updated_at  TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE TABLE IF NOT EXISTS agent_metrics (
        id              BIGSERIAL   PRIMARY KEY,
        agent_role      TEXT        NOT NULL,
        thread_id       TEXT,
        model           TEXT        NOT NULL,
        input_tokens    INTEGER     NOT NULL DEFAULT 0,
        output_tokens   INTEGER     NOT NULL DEFAULT 0,
        recorded_at     TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_agent_metrics_role ON agent_metrics(agent_role);
    CREATE INDEX IF NOT EXISTS idx_agent_metrics_time ON agent_metrics(recorded_at DESC);
    """
    async with state.db.acquire() as conn:
        await conn.execute(ddl)
    logger.info("Phase 4 tables ensured.")


# ─────────────────────────────────────────────
# Phase 5 table migration (idempotent)
# ─────────────────────────────────────────────

async def _ensure_phase5_tables() -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS ci_results (
        id          BIGSERIAL   PRIMARY KEY,
        task_id     TEXT,
        thread_id   TEXT,
        passed      INTEGER     NOT NULL DEFAULT 0,
        failed      INTEGER     NOT NULL DEFAULT 0,
        total       INTEGER     NOT NULL DEFAULT 0,
        exit_code   INTEGER     NOT NULL,
        output      TEXT,
        ran_at      TIMESTAMPTZ DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_ci_results_task   ON ci_results(task_id);
    CREATE INDEX IF NOT EXISTS idx_ci_results_thread ON ci_results(thread_id);
    """
    async with state.db.acquire() as conn:
        await conn.execute(ddl)
    logger.info("Phase 5 tables ensured.")


# ─────────────────────────────────────────────
# Workspace git initialisation
# ─────────────────────────────────────────────

def _init_workspace_git() -> None:
    """Ensure /workspace is a git repo (idempotent)."""
    workspace = pathlib.Path("/workspace")
    if not workspace.exists():
        workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / ".git").exists():
        logger.info("Workspace git repo already initialised.")
        return
    try:
        subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "team-claw@ai"],
            cwd=str(workspace), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Team Claw"],
            cwd=str(workspace), check=True, capture_output=True,
        )
        # Disable CRLF conversion so files committed from Windows hosts
        # don't corrupt line endings inside the Linux container.
        subprocess.run(
            ["git", "config", "core.autocrlf", "false"],
            cwd=str(workspace), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "core.safecrlf", "warn"],
            cwd=str(workspace), check=True, capture_output=True,
        )
        gitignore = workspace / ".gitignore"
        gitignore.write_text("__pycache__/\n*.pyc\n.pytest_cache/\n*.egg-info/\n")
        subprocess.run(
            ["git", "add", ".gitignore"],
            cwd=str(workspace), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "chore: init workspace"],
            cwd=str(workspace), check=True, capture_output=True,
        )
        logger.info("Workspace git repo initialised at %s", workspace)
    except subprocess.CalledProcessError as exc:
        logger.warning("Git init failed (non-fatal): %s", exc)


# ─────────────────────────────────────────────
# Audit loop — reads team:audit, persists to DB
# ─────────────────────────────────────────────

async def _setup_audit_consumer() -> None:
    try:
        await state.redis.xgroup_create(
            AUDIT_STREAM, AUDIT_GROUP, id="0", mkstream=True
        )
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def _audit_loop() -> None:
    """Continuously drain team:audit stream and write to Postgres."""
    logger.info("Audit loop started.")
    while True:
        try:
            result = await state.redis.xreadgroup(
                AUDIT_GROUP,
                AUDIT_CONSUMER,
                {AUDIT_STREAM: ">"},
                count=50,
                block=2000,
            )
            if not result:
                continue
            for _stream, entries in result:
                for redis_id, fields in entries:
                    decoded = _decode(fields)
                    await _persist_message(decoded)
                    await state.redis.xack(AUDIT_STREAM, AUDIT_GROUP, redis_id)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception("Audit loop error: %s", exc)
            await asyncio.sleep(1)


async def _persist_message(data: dict) -> None:
    thread_id = data.get("thread_id")
    if not thread_id:
        return
    try:
        async with state.db.acquire() as conn:
            # Ensure thread exists (may be created by agents mid-conversation)
            await conn.execute(
                "INSERT INTO threads(id, title, status) VALUES($1,$2,'active') ON CONFLICT DO NOTHING",
                thread_id, f"Thread {thread_id[:8]}",
            )
            await conn.execute(
                """
                INSERT INTO messages(id, thread_id, from_role, to_role, type, content, priority,
                                     artifacts, parent_message_id, metadata)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                ON CONFLICT DO NOTHING
                """,
                data.get("id", str(uuid.uuid4())),
                thread_id,
                data.get("from_role", "unknown"),
                data.get("to_role", "unknown"),
                data.get("type", "unknown"),
                data.get("content", ""),
                data.get("priority", "normal"),
                data.get("artifacts", "[]"),
                data.get("parent_message_id") or None,
                data.get("metadata", "{}"),
            )
    except Exception as exc:
        logger.warning("Failed to persist message %s: %s", data.get("id"), exc)


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _encode(d: dict) -> dict:
    """Ensure all values are bytes/str for Redis."""
    return {k: str(v) for k, v in d.items()}


def _decode(fields: dict) -> dict:
    return {
        k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
        for k, v in fields.items()
    }

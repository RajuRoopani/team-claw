# Team Claw — Claude Working Guide

> Session-distilled patterns for working on this codebase efficiently. Read this before touching anything.

---

## Project Snapshot

| Thing | Value |
|-------|-------|
| DB name | `team_claw` |
| DB user | `teamclaw` |
| Orchestrator port | `8080` |
| Devtunnel | `https://rhjrjl9w-8080.usw2.devtunnels.ms` (tunnel: `happy-hill-w28lhr3.usw2`) |
| Total agents | 9 static + dynamic at runtime |
| Model tiers | Opus → PO, EM · Sonnet → Arch, UX, Sr Devs, Security · Haiku → Jr Devs |

---

## Critical: Deployment Workflow

**Never assume a local file edit is live in the container.** The orchestrator serves HTML and Python from `/app/` inside the container — built at image time, not mounted.

### Python change (no rebuild needed)
```bash
docker compose cp orchestrator/main.py orchestrator:/app/main.py
docker compose restart orchestrator
```

### HTML change (no rebuild needed)
```bash
docker compose cp orchestrator/home.html orchestrator:/app/home.html
docker compose cp orchestrator/dashboard.html orchestrator:/app/dashboard.html
docker compose cp orchestrator/report.html orchestrator:/app/report.html
```

### New agent container
```bash
docker compose up -d {service-name}
```

### Verify after deploy
```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/health
docker compose logs orchestrator --tail=20 | grep -v heartbeat
```

> **Git commit ≠ deployed.** Always `docker compose cp` after editing. The "page not loading" class of bugs is almost always a stale container file.

---

## Context Window — Cost Optimisation (Claude Code level)

This section is about how *I* (Claude Code editing this repo) should manage context efficiently.

### Files that are dangerously large — never read whole

| File | Size | Strategy |
|------|------|----------|
| `orchestrator/home.html` | ~80KB / 25K+ tokens | Grep for exact pattern, then Read with `offset+limit` |
| `orchestrator/dashboard.html` | large | Grep first, then targeted Read |
| `orchestrator/main.py` | ~2100 lines | Read in 200-line chunks with `offset`; Grep for function names first |
| `orchestrator/report.html` | ~600 lines | Safe to read whole |
| `agents/base/tools/__init__.py` | ~900 lines | Grep for tool name, read ±20 lines |

### Always Grep before Read

```
Grep pattern="tc-jr2|team-grid" path=home.html output_mode=content context=3
```

Find the exact line range, then `Read offset=N limit=30`. Never start by reading 2000 lines blindly.

### Parallel tool calls

When editing multiple files in one change, launch ALL Read/Grep calls in a single message — never sequential unless one result gates the next. Example: reading `main.py` + `home.html` + `dashboard.html` before adding a new agent → 3 Grep calls in parallel.

### Use Explore agent for open-ended discovery

When you need more than 3 searches to understand something, delegate to the Explore agent. It runs in a subagent context and protects the main context window from large file dumps that you'd never actually use.

### Chunked editing for large files

For `main.py` edits:
1. `Grep` for the function/section name → get the line number
2. `Read offset=<line-50> limit=100` → read only the relevant block
3. `Edit` with old_string/new_string using that targeted context

Never read the whole 2100-line file just to make a 3-line change.

---

## Agent Context Window — How Agents Manage Their Own Context

This is how the **running agents inside Docker** manage their own context windows during task execution.

### Startup injection — memories are free context

Every agent runs `_load_agent_memories()` on startup (in `agents/base/agent.py`). This:

1. Fetches `GET /memory/{role}` from the orchestrator
2. Appends all stored key-value memories to the **system prompt** as a new section:

```
## Your Persistent Memories (from previous sessions)
- **pattern:python:async_http**: Use httpx.AsyncClient(timeout=5) for outbound HTTP — requests blocks event loop
- **lesson:debug:pytest**: Scope test paths explicitly; conftest.py conflicts cause collection errors
- **delegation:pattern:rest_api**: Assign models+DB to sr1, routes+tests to sr2 — avoids merge conflicts
```

**This means:** An agent that has done 20 tasks arrives at task 21 with all its learned patterns pre-loaded in context — without spending any tokens on retrieval. The cost is paid once at container start, not per-turn.

### Context compression during long tasks

The agent loop (`agentic_loop` in `agent.py`) manages the Claude API message list:

| Messages | Treatment |
|----------|-----------|
| Last 5 | Verbatim — exact content sent to Claude |
| 6–20 | Summarized by a Haiku call (cheap) → single compressed message |
| 20+ | Stored in Postgres, not in API payload at all |

**Implication:** An agent can run for many turns without hitting context limits. The Haiku summarization costs ~$0.001 and prevents the expensive Opus/Sonnet models from getting bloated prompts.

### Thread budget

Max 20 messages per thread. EM is alerted at 80% (16 messages). This limits not just cost but the depth of back-and-forth chaining — agents are expected to produce results, not hold extended conversations.

### Max iterations guard

Each agent runs a max of 30 iterations per message. At iterations 27–29, a "final-stretch warning" nudge is injected. If iteration 30 is reached without completing, a rescue message is sent to the orchestrator → EM inbox. This prevents runaway agents and caps per-task cost.

---

## Agent Memory System — Full Lifecycle

Memory is the mechanism that makes agents smarter over time. Understanding this cycle is essential.

### Storage

```
Postgres table: agent_memories
  agent_role  TEXT
  key         TEXT
  value       TEXT
  updated_at  TIMESTAMPTZ
  PRIMARY KEY (agent_role, key)

REST API (in orchestrator/main.py):
  GET    /memory/{role}         → list all memories for role
  GET    /memory/{role}/{key}   → read one memory
  PUT    /memory/{role}/{key}   → write/update (UPSERT)
  DELETE /memory/{role}/{key}   → forget
```

Memories are **per-role** (not per-agent-instance). If a container is replaced or restarted, the next container for that role picks up the same memories. This is how institutional knowledge survives container churn.

### Tools available to agents

| Tool | When to use |
|------|-------------|
| `list_memories` | **At the start of every task** — recall relevant past patterns before planning |
| `write_memory` | **Before reporting task_complete** — save what worked or what failed |
| `read_memory` | Mid-task retrieval of a specific key when you know what you're looking for |

### The retrospection loop

This is the core pattern that makes agents accumulate intelligence over time:

```
1. Task arrives in inbox
2. Agent calls list_memories → reads all past learnings
3. Agent plans using: system prompt + memories + task description
4. Agent executes (up to 30 iterations)
5. Agent calls write_memory (1-2 memories) → saves lesson learned
6. Agent sends task_complete to EM
7. Container restart → _load_agent_memories() re-injects into system prompt
8. Next task arrives with richer context
```

Each task adds 1–2 new memories. After 10 tasks, an agent has 10–20 memories shaping every subsequent plan.

### Memory key naming conventions by role

**Engineering Manager** — thinks about people and process:
```
delegation:pattern:<type>     → task decomposition patterns that worked or failed
team:performance:<role>       → observations about a team member's strengths/failure modes
blocker:pattern:<type>        → recurring blocker classes and how they were resolved
workflow:lesson:<topic>       → process improvements for how threads are run
```

**Senior / Junior Devs** — think about code and technology:
```
pattern:<stack>:<type>        → e.g. pattern:python:async_http, pattern:typescript:pagination
lesson:debug:<type>           → bugs encountered and how to detect/fix
tech:choice:<library>         → a library/approach decision with the reason
review:feedback:<topic>       → code review rules that should be standing policy
```

**Architect** — thinks about design decisions:
```
design:decision:<domain>      → architectural choice and rationale
design:antipattern:<type>     → patterns that caused problems
design:constraint:<project>   → per-project constraints to carry forward
```

**Product Owner** — thinks about requirements:
```
product:pattern:<domain>      → recurring requirement shapes
product:stakeholder:<name>    → stakeholder preferences and communication style
```

### How to inspect agent memories (debugging)

```bash
# See all memories for a role
curl http://localhost:8080/memory/engineering_manager | python3 -m json.tool

# See all memories for senior_dev_1
curl http://localhost:8080/memory/senior_dev_1 | python3 -m json.tool

# Delete a stale/wrong memory
curl -X DELETE http://localhost:8080/memory/engineering_manager/blocker:pattern:old_thing

# Write a memory manually (e.g. to bootstrap a fresh agent)
curl -X PUT http://localhost:8080/memory/junior_dev_1/tech:choice:testing \
  -H "Content-Type: application/json" \
  -d '{"value": "Use pytest with --tb=short; always scope to test/ dir to avoid conftest conflicts"}'
```

### What makes a good memory (and what wastes tokens)

**Good memory** (actionable, specific, survives restarts):
```
key: "lesson:debug:pytest_collection"
value: "Shared /workspace accumulates test files from old tasks. Always run pytest
        with explicit path: pytest tests/ -p no:cacheprovider --continue-on-collection-errors"
```

**Bad memory** (too vague, not actionable):
```
key: "lesson:testing"
value: "Testing is important"
```

**Bad memory** (session-specific, stale next run):
```
key: "current_task"
value: "Working on the auth module for thread abc123"
```

**Rule of thumb:** If the memory won't change what you'd do differently on the *next* task, don't write it.

### Cost impact of memories

Memories are injected into the **system prompt** (not conversation history). For Opus at $15/M input tokens:
- 20 memories × ~50 tokens each = ~1,000 tokens = $0.015 per agent session
- A full 30-iteration task on Opus at ~2K tokens/turn = ~$0.90 input cost
- Memory overhead: ~1.7% of task cost — negligible

**But** the value is multiplicative: good memories prevent rework, wrong delegation, repeated debugging. One memory that prevents a 5-turn debugging loop saves ~$0.15 per occurrence.

---

## Known Bugs / Pitfalls

### UTC Timezone Trap
Never use `datetime.now(timezone.utc).replace(hour=0, ...)` as a "today" filter. At e.g. 5 PM PST the DB clock is already the next UTC day — the filter returns zero rows. **Always use a rolling 24h window:**
```python
window = datetime.now(timezone.utc) - timedelta(hours=24)
```

### HTML Not Updating
If a page looks stale after editing, the container has the old file. Run `docker compose cp` — see deployment section.

### Devtunnel Restart
```bash
pkill -f "devtunnel host"
devtunnel host happy-hill-w28lhr3.usw2 &
# Do NOT pass -p flag — port 8080 is already registered on the tunnel
```

### Postgres Access
```bash
docker exec team-claw-postgres-1 psql -U teamclaw -d team_claw -c "YOUR QUERY"
```

### Inbox Flooding (stale messages between tasks)
```bash
docker exec team-claw-redis-1 redis-cli XTRIM agent:{role}:inbox MAXLEN 0
```

### Agents ignoring memories
If an agent isn't using its memories: check that `_load_agent_memories()` ran (see logs: `"Loaded N memories from store."`). If 0 memories found, the agent is starting fresh — either intentional or the orchestrator was down at startup time (restart the agent container).

### Agent producing nothing (max_tokens stop)
When `stop_reason == "max_tokens"` with no tool calls, the loop injects a continuation nudge. If you see an agent cycling with empty outputs, check the loop iteration count. This is most common with Haiku on complex tasks — consider bumping the agent to Sonnet.

---

## Adding a New Static Agent — Full Checklist

1. `mkdir agents/{role}` → write `system_prompt.md` + `config.py`
2. `orchestrator/main.py` → add `"{role}"` to `STATIC_AGENT_ROLES`
3. `docker-compose.yml` → add service block (copy pattern from existing agent)
4. `orchestrator/dashboard.html` → add `--c-{role}: {hex};` to `:root`
5. `orchestrator/home.html` → add `--c-{short}: {hex};` to `:root`, add `.tc-{short}` card styles, add team card HTML in `#team .team-grid`
6. `agents/engineering_manager/config.py` → add role to `AVAILABLE_ROLES`
7. `agents/engineering_manager/system_prompt.md` → add delegation rule
8. Deploy:
   ```bash
   docker compose cp orchestrator/main.py orchestrator:/app/main.py
   docker compose cp orchestrator/home.html orchestrator:/app/home.html
   docker compose cp orchestrator/dashboard.html orchestrator:/app/dashboard.html
   docker compose restart orchestrator
   docker compose up -d {service-name}
   ```
9. Verify: `curl http://localhost:8080/agents | python3 -m json.tool | grep {role}`

**Bootstrap the new agent's memories** (optional but recommended):
```bash
curl -X PUT http://localhost:8080/memory/{role}/onboarding \
  -H "Content-Type: application/json" \
  -d '{"value": "Initial role: {description}. Key collaborators: {roles}."}'
```

**Agent color convention:**
- `dashboard.html`: `--c-{full_role_name}` e.g. `--c-security_engineer: #f43f5e`
- `home.html`: `--c-{short}` e.g. `--c-sec: #f43f5e`
- Pick a color not already used. Existing: purple, orange, blue, pink, green×2, salmon, red, rose.

---

## Adding a New Page (e.g. /report pattern)

1. Create `orchestrator/{page}.html`
2. Add to `main.py`:
   ```python
   @app.get("/{page}", response_class=HTMLResponse)
   async def page_name():
       f = pathlib.Path(__file__).parent / "{page}.html"
       if f.exists():
           return FileResponse(str(f), media_type="text/html")
       return HTMLResponse("<h1>Not found</h1>", status_code=404)
   ```
3. Link from `home.html`: nav, hero actions, CTA section, footer — all 4 spots
4. Deploy with `docker compose cp` (both html + main.py)

---

## Key Architecture Facts

```
Redis Streams:
  team:audit          → all persisted messages (SSE source)
  team:activity       → ephemeral agent_working signals
  agent:{role}:inbox  → per-agent message queue

SSE endpoint: GET /stream/all  (reads both streams, blocks 3s)

Static agents (STATIC_AGENT_ROLES):
  product_owner, engineering_manager, architect, ux_engineer,
  senior_dev_1, senior_dev_2, junior_dev_1, junior_dev_2,
  security_engineer

Dynamic agents: stored in postgres `dynamic_agents` table,
  loaded into state.dynamic_agents on startup.
  ALL_AGENT_ROLES() = STATIC + dynamic

Cost model: _estimate_cost(model, input_tokens, output_tokens)
  uses _COST_TABLE — Opus $15/$75, Sonnet $3/$15, Haiku $0.25/$1.25
  per million tokens (input/output)

Context compression: last 5 messages verbatim,
  6–20 summarized by Haiku call,
  20+ stored in Postgres only (not in API payload)

Max iterations per agent turn: 30
Final-stretch nudge: injected at iterations 27-29
Exhaustion rescue: POST to orchestrator if loop ends without end_turn
```

---

## Pages Inventory

| URL | File | Purpose |
|-----|------|---------|
| `/` | `home.html` | Marketing / product homepage |
| `/dashboard` | `dashboard.html` | Live engineering feed, kanban, CI |
| `/report` | `report.html` | Executive dashboard (KPIs, cost, velocity) |
| `/pitch` | `pitch-deck.html` | Investor pitch deck |
| `/report/summary` | `main.py` | JSON aggregation for exec dashboard |

---

## README / Homepage Update Rules

When agent count changes, update ALL of these:
- `README.md` line: `` `9+ agents` · `13 containers` ``
- `README.md` team table
- `home.html` hero `<strong>9 specialized AI agents</strong>`
- `home.html` step card description
- `home.html` architecture section title
- `home.html` team section subtitle

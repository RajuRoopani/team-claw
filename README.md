# Team Claw

An autonomous AI software development team running in Docker. Seven Claude agents — each playing a real SDLC role — receive requirements, design, implement, test, review, and ship working code to GitHub without human intervention.

Built across 9 phases, from a minimal 2-agent loop to a full team with CI, human-in-the-loop escalation, live dashboard, and tool telemetry.

---

## The Team

| Container | Role | Model | Responsibilities |
|-----------|------|-------|-----------------|
| `product-owner` | Product Owner | claude-opus-4-6 | Refines requirements, defines acceptance criteria, signs off on delivery |
| `engineering-manager` | Engineering Manager | claude-opus-4-6 | Decomposes tasks, assigns work, tracks progress, unblocks team, triggers git push |
| `architect` | Architect | claude-sonnet-4-6 | Makes architecture/design decisions before implementation begins |
| `senior-dev-1` | Senior Dev 1 | claude-sonnet-4-6 | Implements features, reviews code, mentors Junior Dev 1 |
| `senior-dev-2` | Senior Dev 2 | claude-sonnet-4-6 | Implements features, reviews code, mentors Junior Dev 2 |
| `junior-dev-1` | Junior Dev 1 | claude-haiku-4-5 | Well-defined tasks, writes tests, asks Sr Dev 1 when blocked |
| `junior-dev-2` | Junior Dev 2 | claude-haiku-4-5 | Well-defined tasks, writes tests, asks Sr Dev 2 when blocked |

---

## Architecture

```
Human (CLI / Dashboard)
        │
        ▼
Orchestrator API (:8080)           FastAPI — task router, audit logger, SSE broadcaster
        │
        ├──→ Redis Streams          agent:{role}:inbox per agent + team:audit
        │
        ├── product_owner           Refines requirements → Engineering Manager
        ├── engineering_manager     Breaks down tasks → assigns to devs + architect
        ├── architect               Reviews design → reports back to EM
        ├── senior_dev_1/2          Implements, commits, pushes → task_complete to EM
        └── junior_dev_1/2          Implements with mentor support → task_complete to EM
                │
                ▼
          /workspace                Shared Docker volume — all code written here
                │
                ▼
          sandbox (:8081)           Isolated test runner (no network, 512 MB RAM cap)
                │
                ▼
           PostgreSQL               Messages, threads, tasks, artifacts, CI results, tool telemetry
```

---

## Quick Start

### Prerequisites
- Docker + Docker Compose
- An Anthropic API key
- A GitHub token (for agents to push code)

### 1. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```env
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...
GITHUB_USERNAME=your-github-username
```

### 2. Start the team

```bash
docker compose up --build
```

All 10 containers start: Redis, Postgres, Sandbox, Orchestrator, and 7 agent containers.

### 3. Install the CLI

```bash
pip install httpx
```

### 4. Submit a task

```bash
python3 cli.py submit \
  "Build a REST API for a todo app" \
  "Create a FastAPI service with CRUD endpoints for todos. Include pytest tests. GitHub Repo: build-a-todo-app"
```

### 5. Watch the team work

```bash
python3 cli.py watch <thread_id>
```

Or open the live dashboard at `http://localhost:8080`.

---

## CLI Reference

```
python3 cli.py <command> [options]
```

| Command | Description |
|---------|-------------|
| `submit "<title>" "<description>" [--priority high\|normal\|low]` | Submit a new task to the team |
| `watch <thread_id>` | Stream live messages for a thread (SSE) |
| `threads` | List all threads with status |
| `messages <thread_id>` | Print full message history for a thread |
| `standup [--hours N]` | Show what the team worked on in the last N hours (default: 24) |
| `budget <thread_id>` | Show token usage and budget bar for a thread |
| `tools [--agent role] [--thread id] [--limit N]` | Show tool execution history with stats |
| `questions [--thread <id>]` | List unanswered human questions (HITL) |
| `reply <thread_id> "<message>" [--to <agent_role>]` | Reply to a pending human question |

### Examples

```bash
# Submit a task
python3 cli.py submit "Build a Slack clone" "FastAPI with users, DMs, and group channels. GitHub Repo: my-slack-clone"

# Watch a thread live
python3 cli.py watch 550e8400-e29b-41d4-a716-446655440000

# See what the team built today
python3 cli.py standup --hours 8

# Check if agents have questions for you
python3 cli.py questions

# Reply to an agent's question
python3 cli.py reply 550e8400-e29b-41d4-a716-446655440000 "Use PostgreSQL, not SQLite" --to senior_dev_1

# Check token usage
python3 cli.py budget 550e8400-e29b-41d4-a716-446655440000
```

---

## Dashboard

Open `http://localhost:8080` for the live web dashboard:

- **Thread sidebar** — all threads with live status (active / waiting / complete)
- **Message feed** — real-time SSE stream of every inter-agent message
- **Context-aware chat bar** — submits a new task when no thread is selected; steers the active thread when one is selected (injects a human reply into the team's inbox)
- **Pending Questions panel** — shows unanswered `ask_human` questions; reply inline
- **CI results panel** — pass/fail per task with test counts
- **Kanban task board** — tasks per thread with status
- **Tool Activity panel** — last 8 tool calls with duration + top tools by call count
- **Budget bar** — token usage bar (green → amber → red) below the feed header
- **Agent heartbeat dots** — green/amber/gray per agent (30s heartbeat)
- **Standup modal** — one-click standup report

---

## Tools Available to Agents

| Tool | Description | Who has it |
|------|-------------|------------|
| `send_message` | Route a message to another agent via Redis | All |
| `read_file` | Read a file from /workspace | All |
| `write_file` | Write/overwrite a file in /workspace | All except PO |
| `edit_file` | Search-and-replace within a file | EM, Arch, Sr, Jr |
| `list_files` | List files under a path in /workspace | All |
| `execute_code` | Run code in the sandbox (pytest, python, etc.) | EM, Sr, Jr |
| `search_code` | Grep for a pattern across /workspace | EM, Arch, Sr, Jr |
| `find_files` | Glob pattern match across /workspace | EM, Arch, Sr, Jr |
| `git_status` | Show git status of /workspace | EM, Sr, Jr |
| `git_commit` | Commit staged changes in /workspace | EM, Sr, Jr |
| `git_push` | Push a branch to GitHub | EM, Sr, Jr |
| `git_merge` | Merge a branch into another | EM |
| `git_diff` | Show unstaged/staged/commit-range diff | EM, Arch, Sr, Jr |
| `create_task` | Create a Kanban task (tracked in Postgres) | All |
| `update_task_status` | Update task status (todo/in_progress/done) | All |
| `wiki_write` | Write to the team wiki | EM, Arch, PO |
| `wiki_read` | Read from the team wiki | All |
| `write_memory` | Persist a note to agent memory | All |
| `read_memory` | Read agent memory | All |
| `check_budget` | Check token budget for current thread | All |
| `ask_human` | Pause thread and submit a question to the human | All |

---

## How a Task Works

```
1. Human submits task via CLI or Dashboard
        ↓
2. Orchestrator creates thread → routes to Product Owner
        ↓
3. Product Owner refines requirements → sends to Engineering Manager
        ↓
4. Engineering Manager decomposes into tasks → assigns to Architect + Devs
        ↓
5. Architect reviews design decisions → reports back to EM
        ↓
6. Senior/Junior Devs implement → write tests → execute_code to verify
        ↓
7. Each Dev: git_status → git_commit → git_push → task_complete to EM
        ↓
8. EM receives all task_complete → git_merge feature branches → git_push main
        ↓
9. EM marks tasks done → CI runs in sandbox (pytest)
        ↓
10. CI passes → _auto_complete_thread() fires → thread status: complete
        ↓
11. Dashboard shows ✅ complete; GitHub repo has working code
```

If an agent is blocked or requirements are ambiguous, they call `ask_human` → thread enters `waiting` status → human replies via CLI or dashboard → thread resumes.

---

## Thread Lifecycle

```
submitted → active → (waiting) → active → complete
                         ↑
                  ask_human called;
                  human reply resumes
```

Threads can also be manually closed via the dashboard (🔒 button) or `POST /threads/{id}/close`.

---

## API Reference

### Tasks

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/task` | Submit a new task |
| `GET` | `/threads` | List all threads |
| `GET` | `/threads/{id}` | Get thread details |
| `GET` | `/threads/{id}/messages` | Get all messages in a thread |
| `GET` | `/threads/{id}/budget` | Get token usage for a thread |
| `GET` | `/threads/{id}/summary` | Get AI-generated thread summary |
| `POST` | `/threads/{id}/close` | Close a thread |

### Human-in-the-Loop

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/threads/{id}/human-reply` | Send a reply to an agent (resumes waiting thread) |
| `POST` | `/threads/{id}/ask-human` | (Agent-facing) Submit a question to the human |
| `GET` | `/pending-questions` | List all unanswered human questions |
| `GET` | `/threads/{id}/pending-questions` | List unanswered questions for a thread |

### Kanban

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/tasks` | Create a task |
| `PATCH` | `/tasks/{id}` | Update task status |
| `GET` | `/tasks` | List tasks (filter: `?thread_id=`) |

### CI

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/ci-results` | List CI results (filter: `?thread_id=&task_id=`) |
| `GET` | `/ci-results/trend` | Trend data for CI pass/fail over time |

### Telemetry

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/tool-executions` | Record a tool call (agent-facing) |
| `GET` | `/tool-history` | Query tool call history (filter: agent, tool, thread) |
| `GET` | `/tool-history/stats` | Aggregate stats + p95 latency per tool |
| `POST` | `/heartbeat/{role}` | Agent heartbeat (every 30s) |
| `GET` | `/agents` | Agent online/stale/offline status |

### Reports

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/standup` | Standup report (`?hours=24`) |
| `POST` | `/standup/publish` | Write standup to team wiki |
| `GET` | `/events` | SSE stream of all real-time events |

---

## Database Schema

| Table | Purpose |
|-------|---------|
| `threads` | Task threads with status, title, GitHub repo |
| `messages` | All inter-agent messages (full content + metadata) |
| `tasks` | Kanban tasks (todo / in_progress / done) |
| `artifacts` | Files written by agents (path + content) |
| `ci_results` | Sandbox test run results per task |
| `wiki` | Team wiki (key-value, updated by agents) |
| `agent_heartbeats` | Last ping per agent role |
| `tool_executions` | Every tool call: agent, tool, duration_ms, success |
| `human_questions` | HITL questions from agents, with answers |

---

## Project Structure

```
team-claw/
├── docker-compose.yml
├── .env.example
├── cli.py                              # Human CLI (submit, watch, standup, etc.)
│
├── orchestrator/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                         # FastAPI app — all endpoints, SSE, webhooks
│   └── dashboard.html                  # Live web dashboard (served at /)
│
├── agents/
│   ├── base/                           # Shared runtime for all agents
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   ├── agent.py                    # Core agentic loop (Claude API + tool dispatch)
│   │   ├── message_bus.py              # Redis Streams wrapper
│   │   ├── models.py                   # Message dataclass + MessageType enum
│   │   ├── entrypoint.py              # Container entrypoint (reads role from env)
│   │   └── tools/
│   │       └── __init__.py             # All tool schemas + executors + dispatcher
│   │
│   ├── product_owner/
│   │   ├── system_prompt.md
│   │   └── config.py                   # Allowed tools + reachable roles
│   ├── engineering_manager/
│   │   ├── system_prompt.md
│   │   └── config.py
│   ├── architect/
│   │   ├── system_prompt.md
│   │   └── config.py
│   ├── senior_dev/
│   │   ├── system_prompt.md
│   │   └── config.py
│   └── junior_dev/
│       ├── system_prompt.md
│       └── config.py
│
├── sandbox/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── main.py                         # Code execution API (:8081)
│
└── shared/
    ├── db/
    │   └── schema.sql                  # Postgres schema (auto-loaded at startup)
    └── workspace/
        └── .gitkeep                    # Code written by agents lives here
```

---

## Configuration

Copy `.env.example` to `.env` and set these variables:

```env
# Required
ANTHROPIC_API_KEY=sk-ant-...

# GitHub (for agents to push code)
GITHUB_TOKEN=ghp_...
GITHUB_USERNAME=your-github-username

# Database
DB_USER=teamclaw
DB_PASSWORD=teamclaw

# Optional
WEBHOOK_URL=                    # POST notifications on ci.pass/ci.fail/thread.complete
THREAD_BUDGET_TOKENS=0          # Token budget per thread (0 = unlimited)
IDLE_THREAD_MINUTES=0           # Alert on threads idle > N minutes (0 = disabled)

# Model overrides (defaults shown)
PO_MODEL=claude-opus-4-6
EM_MODEL=claude-opus-4-6
ARCH_MODEL=claude-sonnet-4-6
SR_MODEL=claude-sonnet-4-6
JR_MODEL=claude-haiku-4-5-20251001
```

---

## Webhooks

Set `WEBHOOK_URL` in `.env` to receive POST notifications:

| Event | Payload |
|-------|---------|
| `ci.pass` | `{thread_id, task_id, passed, total}` |
| `ci.fail` | `{thread_id, task_id, passed, total, output}` |
| `thread.complete` | `{thread_id}` |
| `thread.waiting` | `{thread_id, question}` |
| `thread.resumed` | `{thread_id, target_role}` |
| `thread.closed` | `{thread_id}` |
| `budget.warning` | `{thread_id, used, limit}` |
| `budget.exceeded` | `{thread_id, used, limit}` |

---

## What's Been Built (Phase History)

| Phase | What was added |
|-------|----------------|
| 1 | Foundation: Redis + Postgres + base Dockerfile, 2-agent loop (EM ↔ Sr Dev) |
| 2 | Full 7-agent team, all roles, code execution sandbox, Junior Devs |
| 3 | Context summarization, git tools (`git_status`, `git_commit`, `git_push`, `git_merge`), live dashboard |
| 4 | Shared `/workspace` volume, wiki tools, agent memory, artifact tracking |
| 5 | Agent heartbeats, Kanban task board, auto-CI (pytest in sandbox) |
| 6 | CI quality gate (blocks `done` if CI failed), webhooks, thread auto-completion |
| 7 | `search_code`, `find_files`, `check_budget` tools; standup report; budget bar in dashboard |
| 8 | `edit_file` tool, tool telemetry (duration + success per call), thread close endpoint, idle thread alerts |
| 9 | Human-in-the-loop: `ask_human` tool, `human_questions` table, pending questions panel, `git_diff` tool, context-aware chat bar (new task vs. steer mode), `questions`/`reply` CLI commands |

---

## Tips

**Including a GitHub repo in your task** ensures agents push code when done:
```
"... GitHub Repo: build-a-todo-app"
```

**Steering a running task** — select the thread in the dashboard and type in the chat bar, or:
```bash
python3 cli.py reply <thread_id> "Switch from SQLite to PostgreSQL"
```

**Checking for questions** — agents call `ask_human` when genuinely blocked:
```bash
python3 cli.py questions
python3 cli.py reply <thread_id> "Your answer here"
```

**Watching costs** — each agent reports token usage; budget bars turn amber at 80%, red at 100%.

---

Built by [RajuRoopani](https://github.com/RajuRoopani).

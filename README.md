# Team Claw

A team of Claude AI agents running in Docker, each assigned a software development lifecycle role. They communicate via Redis Streams and collectively produce working code.

## Phase 1 — Active
Two agents: **Engineering Manager** + **Senior Dev 1**

```
Human → Orchestrator → EM → Senior Dev → writes code → reports back → EM → Human
```

## Architecture

```
Human CLI
   │
   ▼
Orchestrator API (:8080)        — FastAPI, logs all messages to Postgres
   │
   ▼
Redis Message Bus               — Streams per agent inbox + shared audit stream
   │
   ├── Engineering Manager     — claude-opus-4-6
   └── Senior Dev 1            — claude-sonnet-4-6
          │
          └── /workspace       — shared Docker volume (all code goes here)
```

## Quick Start

### 1. Prerequisites
- Docker + Docker Compose
- An Anthropic API key

### 2. Configure
```bash
cp .env.example .env
# Edit .env and set your ANTHROPIC_API_KEY
```

### 3. Start the team
```bash
docker compose up --build
```

### 4. Install CLI deps (host machine)
```bash
pip install httpx
```

### 5. Submit a task
```bash
python cli.py submit \
  "Build a calculator module" \
  "Create a Python module with add, subtract, multiply, divide functions. Include unit tests."
```

### 6. Watch them work
```bash
python cli.py watch <thread_id>
```

### 7. Browse all threads
```bash
python cli.py threads
python cli.py messages <thread_id>
```

## Project Structure

```
team-claw/
├── docker-compose.yml
├── .env.example
├── cli.py                          # Human interface
│
├── orchestrator/                   # HTTP API + audit logger
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
│
├── agents/
│   ├── base/                       # Shared agent runtime
│   │   ├── Dockerfile
│   │   ├── agent.py                # Core agentic loop
│   │   ├── message_bus.py          # Redis Streams wrapper
│   │   ├── models.py               # Message dataclass
│   │   ├── entrypoint.py           # Container entrypoint
│   │   ├── requirements.txt
│   │   └── tools/
│   │       └── __init__.py         # Tool schemas + execution
│   │
│   ├── engineering_manager/
│   │   ├── system_prompt.md        # Role personality + instructions
│   │   └── config.py               # Allowed tools + reachable roles
│   │
│   └── senior_dev/
│       ├── system_prompt.md
│       └── config.py
│
└── shared/
    ├── db/
    │   └── schema.sql              # Postgres schema (auto-loaded)
    └── workspace/                  # Code written by agents lives here
```

## Message Flow

All inter-agent communication uses a structured `Message` type:

```
MessageType: task_assignment | question | answer | review_request |
             review_feedback | status_update | blocker | task_complete

Priority: high | normal | low
```

Every message flows:
1. Producer calls `send_message` tool → `MessageBus.send()`
2. Delivered to `agent:{role}:inbox` Redis stream
3. Also published to `team:audit` stream
4. Orchestrator reads `team:audit` → persists to Postgres

## Roadmap

- **Phase 1** ✅ EM + Sr1, message loop, file tools
- **Phase 2** Full 7-agent team, code execution sandbox, Jr Devs
- **Phase 3** Context summarization, git integration, dashboard
- **Phase 4** Sprint metrics, replay mode, cost tracking

# Team Claw — Claude Working Guide

> Session-distilled patterns. Read this before touching anything.

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

## Context Window — Cost Optimisation Rules

These files are dangerously large. **Never read them whole:**

| File | Size | Strategy |
|------|------|----------|
| `orchestrator/home.html` | ~80KB / 25K+ tokens | Grep for exact pattern, use `offset+limit` |
| `orchestrator/dashboard.html` | large | Grep first |
| `orchestrator/main.py` | ~2100 lines | Read in 200-line chunks with `offset` |
| `orchestrator/report.html` | ~600 lines | Safe to read whole |

**Always Grep before Read.** Example pattern for targeted edits:
```
Grep pattern="tc-jr2|team-grid" path=home.html output_mode=content context=3
```

**Parallel reads.** When you need 3+ files, launch all Read/Grep calls in a single message — never sequential unless one depends on another.

**Use the Explore agent** for open-ended codebase discovery (>3 searches needed). Protects main context from large file dumps.

---

## Known Bugs / Pitfalls

### UTC Timezone Trap
Never use `datetime.now(timezone.utc).replace(hour=0, ...)` as a "today" filter. At e.g. 5 PM PST the DB clock is already the next UTC day — the filter returns zero rows. **Always use a rolling 24h window:**
```python
window = datetime.now(timezone.utc) - timedelta(hours=24)
```

### HTML Not Updating
If a page looks stale after editing, the container has the old file. Run `docker compose cp` — see deployment section above.

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

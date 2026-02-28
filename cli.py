#!/usr/bin/env python3
"""
Team Claw CLI — submit tasks and watch the team work in real time.

Usage:
    python cli.py submit "Build user auth" "JWT-based login with email/password"
    python cli.py watch <thread_id>
    python cli.py threads
    python cli.py messages <thread_id>
    python cli.py standup [--hours N]
    python cli.py budget <thread_id>
    python cli.py tools [--agent <role>] [--thread <id>] [--limit N]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime

import httpx

BASE_URL = "http://localhost:8080"

ROLE_COLORS = {
    "orchestrator":       "\033[90m",    # grey
    "product_owner":      "\033[35m",    # magenta
    "engineering_manager": "\033[33m",   # yellow
    "architect":          "\033[36m",    # cyan
    "senior_dev_1":       "\033[34m",    # blue
    "senior_dev_2":       "\033[94m",    # bright blue
    "junior_dev_1":       "\033[32m",    # green
    "junior_dev_2":       "\033[92m",    # bright green
}
RESET = "\033[0m"
BOLD  = "\033[1m"
DIM   = "\033[2m"

TYPE_ICONS = {
    "task_assignment":  "📋",
    "question":         "❓",
    "answer":           "💡",
    "review_request":   "🔍",
    "review_feedback":  "📝",
    "status_update":    "📊",
    "blocker":          "🚧",
    "task_complete":    "✅",
    "requirement":      "📌",
    "acceptance_result": "🎯",
    "human_input":      "👤",
    "agent_reply":      "🤖",
}


def _color(role: str) -> str:
    return ROLE_COLORS.get(role, "\033[37m")


def _print_message(msg: dict) -> None:
    icon = TYPE_ICONS.get(msg.get("type", ""), "💬")
    from_role = msg.get("from_role", "?")
    to_role = msg.get("to_role", "?")
    msg_type = msg.get("type", "?")
    content = msg.get("content", "")
    created = msg.get("created_at", "")

    # Timestamp
    try:
        ts = datetime.fromisoformat(created).strftime("%H:%M:%S")
    except Exception:
        ts = created[:8] if created else "??:??:??"

    # Header line
    from_colored = f"{_color(from_role)}{BOLD}{from_role}{RESET}"
    to_colored   = f"{_color(to_role)}{to_role}{RESET}"
    print(f"\n{DIM}{ts}{RESET}  {icon} {from_colored} → {to_colored}  {DIM}[{msg_type}]{RESET}")

    # Content (indent + wrap at 100 chars)
    for line in content.splitlines():
        print(f"    {line}")


async def cmd_submit(title: str, description: str, priority: str = "normal") -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        resp = await client.post("/task", json={
            "title": title,
            "description": description,
            "priority": priority,
        })
        resp.raise_for_status()
        data = resp.json()

    thread_id = data["thread_id"]
    print(f"\n{BOLD}Task submitted!{RESET}")
    print(f"  Thread ID : {thread_id}")
    print(f"  Status    : {data['status']}")
    print(f"\nWatch the team work:")
    print(f"  python cli.py watch {thread_id}\n")


async def cmd_watch(thread_id: str) -> None:
    """Stream live messages for a thread using SSE."""
    print(f"\n{BOLD}Watching thread {thread_id[:8]}…{RESET}  (Ctrl+C to stop)\n")

    # First print existing messages
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        try:
            resp = await client.get(f"/threads/{thread_id}/messages")
            if resp.status_code == 200:
                for msg in resp.json():
                    _print_message(msg)
        except Exception:
            pass

    # Then stream new ones
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=None) as client:
        try:
            async with client.stream("GET", f"/threads/{thread_id}/stream") as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        try:
                            msg = json.loads(line[6:])
                            _print_message(msg)
                        except json.JSONDecodeError:
                            pass
        except KeyboardInterrupt:
            print("\n\nStopped watching.")


async def cmd_threads() -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        resp = await client.get("/threads")
        resp.raise_for_status()
        threads = resp.json()

    if not threads:
        print("No threads yet. Submit a task first.")
        return

    print(f"\n{BOLD}Active Threads{RESET}\n")
    print(f"  {'ID':<38}  {'Messages':>8}  {'Status':<12}  Title")
    print(f"  {'─'*38}  {'─'*8}  {'─'*12}  {'─'*40}")
    for t in threads:
        print(f"  {t['id']:<38}  {t['message_count']:>8}  {t['status']:<12}  {t['title']}")
    print()


async def cmd_messages(thread_id: str) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        resp = await client.get(f"/threads/{thread_id}/messages")
        if resp.status_code == 404:
            print(f"Thread {thread_id} not found or empty.")
            return
        resp.raise_for_status()
        messages = resp.json()

    print(f"\n{BOLD}Messages in thread {thread_id[:8]}{RESET}\n")
    for msg in messages:
        _print_message(msg)
    print()


async def cmd_standup(hours: int = 24) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=15) as client:
        resp = await client.get(f"/standup?hours={hours}")
        resp.raise_for_status()
        d = resp.json()

    print(f"\n{BOLD}📋 Daily Standup{RESET}  {DIM}(last {hours}h · {d.get('generated_at','')[:19]}){RESET}\n")

    print(f"{BOLD}Active Threads ({len(d['active_threads'])}){RESET}")
    for t in d["active_threads"]:
        print(f"  {t['title'][:50]:<50}  {t['messages']:>4} msgs  {DIM}{t['status']}{RESET}")
    if not d["active_threads"]:
        print(f"  {DIM}(none){RESET}")

    print(f"\n{BOLD}Tasks Completed ({len(d['tasks_completed'])}){RESET}")
    for t in d["tasks_completed"]:
        print(f"  ✅ {t['title'][:50]:<50}  {DIM}{t['assignee'] or '—'}{RESET}")
    if not d["tasks_completed"]:
        print(f"  {DIM}(none){RESET}")

    print(f"\n{BOLD}In Progress ({len(d['tasks_in_progress'])}){RESET}")
    for t in d["tasks_in_progress"]:
        print(f"  🔄 {t['title'][:50]:<50}  {DIM}{t['assignee'] or '—'}{RESET}")
    if not d["tasks_in_progress"]:
        print(f"  {DIM}(none){RESET}")

    ci = d["ci_summary"]
    badge = "✅" if ci["failed"] == 0 and ci["total"] > 0 else ("❌" if ci["failed"] > 0 else "—")
    print(f"\n{BOLD}CI Summary{RESET}  {badge}  passed={ci['passed']}  failed={ci['failed']}  total={ci['total']}")

    print(f"\n{BOLD}Message Activity{RESET}")
    for role, cnt in d["messages_by_agent"].items():
        print(f"  {role:<24}  {cnt:>4} messages")

    cost = d["token_cost"]
    print(f"\n{BOLD}Token Cost{RESET}")
    print(f"  Input:  {cost['total_input']:>10,} tokens")
    print(f"  Output: {cost['total_output']:>10,} tokens")
    print(f"  Cost:   {BOLD}${cost['estimated_cost_usd']:.4f}{RESET}")

    if d["recent_blockers"]:
        print(f"\n{BOLD}Recent Blockers{RESET}")
        for b in d["recent_blockers"]:
            print(f"  🚧 [{b['from_role']}]  {b['excerpt'][:80]}")
    print()


async def cmd_tools(
    agent: str | None = None,
    thread: str | None = None,
    limit: int = 50,
) -> None:
    params: dict[str, str] = {"limit": str(limit)}
    if agent:
        params["agent"] = agent
    if thread:
        params["thread_id"] = thread

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        hist_resp = await client.get("/tool-history", params=params)
        hist_resp.raise_for_status()
        records = hist_resp.json()

        stats_resp = await client.get("/tool-history/stats")
        stats_resp.raise_for_status()
        stats = stats_resp.json()

    if not records:
        print("No tool executions found.")
        return

    print(f"\n{BOLD}Tool History{RESET}  {DIM}({len(records)} records){RESET}\n")
    print(f"  {'Time':<10}  {'Agent':<22}  {'Tool':<20}  {'ms':>6}  Status")
    print(f"  {'─'*10}  {'─'*22}  {'─'*20}  {'─'*6}  {'─'*8}")
    for r in records:
        try:
            ts = datetime.fromisoformat(r["executed_at"]).strftime("%H:%M:%S")
        except Exception:
            ts = r.get("executed_at", "")[:8]
        badge = "✅" if r["success"] else "❌"
        print(f"  {ts:<10}  {r['agent_role']:<22}  {r['tool_name']:<20}  {r['duration_ms']:>6}  {badge}")
        if not r["success"] and r.get("error"):
            print(f"  {DIM}   error: {r['error'][:80]}{RESET}")

    if stats:
        print(f"\n{BOLD}Tool Stats Summary{RESET}\n")
        print(f"  {'Tool':<22}  {'Calls':>6}  {'Success%':>9}  {'Avg ms':>8}  {'P95 ms':>8}")
        print(f"  {'─'*22}  {'─'*6}  {'─'*9}  {'─'*8}  {'─'*8}")
        for s in stats:
            sr = f"{s['success_rate']*100:.1f}%"
            print(f"  {s['tool_name']:<22}  {s['total_calls']:>6}  {sr:>9}  {s['avg_duration_ms']:>8.1f}  {s['p95_duration_ms']:>8.1f}")
    print()


async def cmd_budget(thread_id: str) -> None:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=10) as client:
        resp = await client.get(f"/threads/{thread_id}/budget")
        if resp.status_code == 404:
            print(f"Thread {thread_id} not found.")
            return
        resp.raise_for_status()
        d = resp.json()

    print(f"\n{BOLD}Token Budget — thread {thread_id[:8]}{RESET}\n")
    if d["status"] == "unlimited":
        used_str = f"{d['tokens_used']:,}" if d["tokens_used"] else "0"
        print(f"  Used: {used_str} tokens  (no budget configured)")
    else:
        pct   = d["pct_used"]
        used  = d["tokens_used"]
        budget = d["budget"]
        status = d["status"]

        # Build bar
        bar_width = 30
        filled = int(min(pct, 100) / 100 * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)

        if status == "exceeded":
            color = "\033[91m"   # bright red
        elif status == "warning":
            color = "\033[33m"   # yellow
        else:
            color = "\033[32m"   # green

        # Estimate cost (rough: assume sonnet pricing for display)
        print(f"  [{color}{bar}{RESET}] {color}{pct:.1f}%{RESET}")
        print(f"  Used:   {used:>10,} tokens")
        print(f"  Budget: {budget:>10,} tokens")
        print(f"  Status: {color}{status.upper()}{RESET}")
    print()


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]

    if cmd == "submit":
        if len(args) < 3:
            print("Usage: python cli.py submit <title> <description> [priority]")
            sys.exit(1)
        title = args[1]
        description = args[2]
        priority = args[3] if len(args) > 3 else "normal"
        asyncio.run(cmd_submit(title, description, priority))

    elif cmd == "watch":
        if len(args) < 2:
            print("Usage: python cli.py watch <thread_id>")
            sys.exit(1)
        asyncio.run(cmd_watch(args[1]))

    elif cmd == "threads":
        asyncio.run(cmd_threads())

    elif cmd == "messages":
        if len(args) < 2:
            print("Usage: python cli.py messages <thread_id>")
            sys.exit(1)
        asyncio.run(cmd_messages(args[1]))

    elif cmd == "standup":
        hours = 24
        if len(args) >= 3 and args[1] == "--hours":
            try:
                hours = int(args[2])
            except ValueError:
                print("--hours must be an integer")
                sys.exit(1)
        asyncio.run(cmd_standup(hours))

    elif cmd == "budget":
        if len(args) < 2:
            print("Usage: python cli.py budget <thread_id>")
            sys.exit(1)
        asyncio.run(cmd_budget(args[1]))

    elif cmd == "tools":
        agent_filter = None
        thread_filter = None
        limit_val = 50
        i = 1
        while i < len(args):
            if args[i] == "--agent" and i + 1 < len(args):
                agent_filter = args[i + 1]; i += 2
            elif args[i] == "--thread" and i + 1 < len(args):
                thread_filter = args[i + 1]; i += 2
            elif args[i] == "--limit" and i + 1 < len(args):
                try:
                    limit_val = int(args[i + 1])
                except ValueError:
                    print("--limit must be an integer"); sys.exit(1)
                i += 2
            else:
                i += 1
        asyncio.run(cmd_tools(agent=agent_filter, thread=thread_filter, limit=limit_val))

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()

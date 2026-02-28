"""
Tool registry — builds Claude-compatible tool schemas and dispatches execution.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any

logger = logging.getLogger(__name__)

WORKSPACE_ROOT = pathlib.Path("/workspace")

# ─────────────────────────────────────────────
# Tool schemas (Anthropic tool_use format)
# ─────────────────────────────────────────────

def _send_message_schema(available_roles: list[str]) -> dict:
    return {
        "name": "send_message",
        "description": (
            "Send a message to another team member. "
            "This is how you communicate — assign tasks, ask questions, "
            "report status, request reviews, raise blockers."
        ),
        "input_schema": {
            "type": "object",
            "required": ["to_role", "message_type", "content"],
            "properties": {
                "to_role": {
                    "type": "string",
                    "description": "Recipient role identifier.",
                    "enum": available_roles,
                },
                "message_type": {
                    "type": "string",
                    "enum": [
                        "task_assignment",
                        "question",
                        "answer",
                        "review_request",
                        "review_feedback",
                        "status_update",
                        "blocker",
                        "task_complete",
                        "agent_reply",
                    ],
                },
                "content": {
                    "type": "string",
                    "description": "Message body. Be clear and specific.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["high", "normal", "low"],
                    "description": "Message priority. Default: normal.",
                },
            },
        },
    }


_WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": (
        "Write content to a file in the shared /workspace directory. "
        "Creates parent directories automatically. "
        "Path must be relative (e.g. 'src/auth/service.py')."
    ),
    "input_schema": {
        "type": "object",
        "required": ["path", "content"],
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to /workspace.",
            },
            "content": {
                "type": "string",
                "description": "Full file content to write.",
            },
        },
    },
}

_READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": "Read a file from the /workspace directory.",
    "input_schema": {
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to /workspace.",
            }
        },
    },
}

_LIST_FILES_SCHEMA = {
    "name": "list_files",
    "description": "List files in a /workspace directory.",
    "input_schema": {
        "type": "object",
        "properties": {
            "directory": {
                "type": "string",
                "description": "Directory path relative to /workspace. Defaults to root.",
                "default": "",
            }
        },
    },
}

_EXECUTE_CODE_SCHEMA = {
    "name": "execute_code",
    "description": (
        "Execute code in the sandboxed environment. "
        "Use this to run tests, verify code outputs, or check for errors. "
        "PYTHONPATH is set to /workspace root so 'from src.foo import bar' works. "
        "Always run your code before marking a task complete. "
        "REQUIRED: you MUST supply either 'code' (inline source) or 'file_path' — "
        "calling this tool with only 'language' will always fail."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Inline code to execute. REQUIRED unless file_path is given.",
            },
            "file_path": {
                "type": "string",
                "description": "Path relative to /workspace to execute. REQUIRED unless code is given.",
            },
            "language": {
                "type": "string",
                "enum": ["python", "bash", "pytest"],
                "description": "'pytest' runs the target with pytest -v. Default: python.",
                "default": "python",
            },
            "working_directory": {
                "type": "string",
                "description": "Working directory relative to /workspace. Default: root.",
                "default": "",
            },
        },
    },
}

_GIT_COMMIT_SCHEMA = {
    "name": "git_commit",
    "description": (
        "Stage and commit files to the workspace git repository. "
        "Call this after writing code to create a checkpoint. "
        "Use conventional commits: feat:, fix:, test:, docs:, refactor:"
    ),
    "input_schema": {
        "type": "object",
        "required": ["message"],
        "properties": {
            "message": {
                "type": "string",
                "description": "Commit message. E.g. 'feat: add user auth middleware'",
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific file paths to stage (relative to /workspace). Leave empty to stage all.",
            },
        },
    },
}

_GIT_STATUS_SCHEMA = {
    "name": "git_status",
    "description": "Show git status of the workspace — staged, unstaged, and untracked files.",
    "input_schema": {"type": "object", "properties": {}},
}

_GIT_LOG_SCHEMA = {
    "name": "git_log",
    "description": "Show recent git commits in the workspace.",
    "input_schema": {
        "type": "object",
        "properties": {
            "count": {
                "type": "integer",
                "description": "Number of commits to show. Default: 5.",
                "default": 5,
            }
        },
    },
}

_WRITE_MEMORY_SCHEMA = {
    "name": "write_memory",
    "description": (
        "Persist a key-value fact to your personal memory. "
        "Survives container restarts and is injected into your context at startup. "
        "Use for: file locations, coding patterns, team preferences, architectural decisions. "
        "Key should be short and specific, e.g. 'workspace_structure' or 'preferred_test_runner'."
    ),
    "input_schema": {
        "type": "object",
        "required": ["key", "value"],
        "properties": {
            "key":   {"type": "string", "description": "Short memory identifier."},
            "value": {"type": "string", "description": "Value to remember (text, up to a few sentences)."},
        },
    },
}

_READ_MEMORY_SCHEMA = {
    "name": "read_memory",
    "description": "Recall a specific memory by key. Returns the stored value.",
    "input_schema": {
        "type": "object",
        "required": ["key"],
        "properties": {
            "key": {"type": "string", "description": "Memory key to retrieve."},
        },
    },
}

_LIST_MEMORIES_SCHEMA = {
    "name": "list_memories",
    "description": "List all your persisted memory keys and values.",
    "input_schema": {"type": "object", "properties": {}},
}

_WIKI_WRITE_SCHEMA = {
    "name": "wiki_write",
    "description": (
        "Write or update a team wiki article. Visible to ALL agents. "
        "Use for: architecture decisions, API contracts, module documentation, "
        "coding standards, deployment notes. Supports Markdown."
    ),
    "input_schema": {
        "type": "object",
        "required": ["title", "content"],
        "properties": {
            "title":   {"type": "string", "description": "Article title, e.g. 'Auth Module Design'."},
            "content": {"type": "string", "description": "Markdown content of the article."},
        },
    },
}

_WIKI_READ_SCHEMA = {
    "name": "wiki_read",
    "description": "Read a team wiki article by exact title.",
    "input_schema": {
        "type": "object",
        "required": ["title"],
        "properties": {
            "title": {"type": "string", "description": "Exact title of the wiki article."},
        },
    },
}

_WIKI_SEARCH_SCHEMA = {
    "name": "wiki_search",
    "description": "Search team wiki articles by keyword. Returns matching titles and content excerpts.",
    "input_schema": {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "description": "Search keyword or phrase."},
        },
    },
}

_CREATE_TASK_SCHEMA = {
    "name": "create_task",
    "description": (
        "Create a formal task on the team Kanban board. "
        "Use this to track work items with assignee and status. "
        "Tasks start as 'pending' and progress through: pending → in_progress → review → done. "
        "CI runs automatically when a task is marked done."
    ),
    "input_schema": {
        "type": "object",
        "required": ["title", "thread_id"],
        "properties": {
            "title":       {"type": "string", "description": "Short task title, e.g. 'Implement user auth endpoint'."},
            "description": {"type": "string", "description": "Detailed description of the work required."},
            "assignee":    {"type": "string", "description": "Role to assign, e.g. 'senior_dev_1'."},
            "thread_id":   {"type": "string", "description": "Current thread ID (copy from the incoming message header)."},
        },
    },
}

_UPDATE_TASK_SCHEMA = {
    "name": "update_task_status",
    "description": (
        "Update a task's status on the Kanban board. "
        "Valid statuses: pending, in_progress, review, done. "
        "Marking a task 'done' automatically triggers the CI pipeline."
    ),
    "input_schema": {
        "type": "object",
        "required": ["task_id", "status"],
        "properties": {
            "task_id": {"type": "string", "description": "Task UUID returned by create_task."},
            "status":  {
                "type": "string",
                "enum": ["pending", "in_progress", "review", "done"],
                "description": "New task status.",
            },
        },
    },
}

_SEARCH_CODE_SCHEMA = {
    "name": "search_code",
    "description": (
        "Search for text or patterns across all files in the /workspace directory. "
        "Use this to find function definitions, usages, TODO comments, or any text. "
        "Returns matching lines with file path and line number."
    ),
    "input_schema": {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query":          {"type": "string", "description": "Text or regex pattern to search for."},
            "path":           {"type": "string", "description": "Subdirectory to search in (relative to /workspace). Default: all files."},
            "case_sensitive": {"type": "boolean", "description": "Case-sensitive search. Default: false.", "default": False},
            "max_results":    {"type": "integer", "description": "Max lines to return. Default: 40.", "default": 40},
        },
    },
}

_FIND_FILES_SCHEMA = {
    "name": "find_files",
    "description": "Find files in /workspace matching a name pattern. Use glob syntax like '*.py', 'test_*.py', '**/*.ts'.",
    "input_schema": {
        "type": "object",
        "required": ["pattern"],
        "properties": {
            "pattern":   {"type": "string", "description": "Filename glob pattern, e.g. '*.py', 'test_*.py'."},
            "directory": {"type": "string", "description": "Subdirectory to search (relative to /workspace). Default: all."},
        },
    },
}

_CHECK_BUDGET_SCHEMA = {
    "name": "check_budget",
    "description": (
        "Check the token budget for the current thread. "
        "Returns tokens used, budget limit, and percentage consumed. "
        "Call this if you're about to do expensive work or when context is large."
    ),
    "input_schema": {
        "type": "object",
        "required": ["thread_id"],
        "properties": {
            "thread_id": {"type": "string", "description": "Thread ID from the incoming message header."},
        },
    },
}

_ASK_HUMAN_SCHEMA = {
    "name": "ask_human",
    "description": (
        "Pause the current task and ask the human a question when you need clarification "
        "that cannot be resolved from the codebase, requirements, or teammates. "
        "Use sparingly — only when genuinely blocked. "
        "After calling this tool, send a status_update to your manager summarizing "
        "current progress, then stop processing. You will receive a new message when "
        "the human replies."
    ),
    "input_schema": {
        "type": "object",
        "required": ["question", "thread_id"],
        "properties": {
            "question":  {"type": "string", "description": "The specific question for the human."},
            "context":   {"type": "string", "description": "Background that helps the human answer."},
            "thread_id": {"type": "string", "description": "Current thread ID from the incoming message."},
        },
    },
}

_GIT_DIFF_SCHEMA = {
    "name": "git_diff",
    "description": (
        "Show git diff for the /workspace repository. "
        "By default shows unstaged working-tree changes. "
        "Use staged=true for staged (cached) changes. "
        "Use base/target to compare specific commits (e.g. base='HEAD~1'). "
        "Useful before committing or when reviewing code changes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "staged": {"type": "boolean",  "description": "Show staged changes (git diff --cached)."},
            "base":   {"type": "string",   "description": "Base ref/commit (e.g. HEAD~1, abc1234)."},
            "target": {"type": "string",   "description": "Target ref/commit (default: working tree)."},
            "path":   {"type": "string",   "description": "Restrict diff to this file/directory path."},
        },
    },
}

_GIT_CHECKOUT_BRANCH_SCHEMA = {
    "name": "git_checkout_branch",
    "description": (
        "Create and switch to a git branch in /workspace. "
        "Use your own role name as the branch (e.g. 'junior_dev_1', 'senior_dev_2'). "
        "Always call this before starting work on a task so your commits don't "
        "conflict with other developers' work on the same repo."
    ),
    "input_schema": {
        "type": "object",
        "required": ["branch_name"],
        "properties": {
            "branch_name": {
                "type": "string",
                "description": "Branch name to create/switch to (e.g. 'junior_dev_1', 'feature/auth').",
            },
        },
    },
}

_GIT_MERGE_SCHEMA = {
    "name": "git_merge",
    "description": (
        "Merge a branch into the current HEAD branch in /workspace. "
        "Used by Senior Devs to merge junior branches, and by the Engineering Manager "
        "to merge all feature branches into main before the final git_push. "
        "If there are conflicts, use strategy='ours' to keep your version or "
        "strategy='theirs' to accept the incoming branch."
    ),
    "input_schema": {
        "type": "object",
        "required": ["branch_name"],
        "properties": {
            "branch_name": {
                "type": "string",
                "description": "Name of the branch to merge into the current branch.",
            },
            "strategy": {
                "type": "string",
                "enum": ["", "ours", "theirs"],
                "description": "Conflict resolution strategy: 'ours' keeps current branch, 'theirs' accepts incoming.",
            },
        },
    },
}

_GIT_PUSH_SCHEMA = {
    "name": "git_push",
    "description": (
        "Push committed workspace code to a GitHub repository under the configured account. "
        "Creates the GitHub repo automatically if it doesn't exist. "
        "Call this after git_commit when a task or feature is complete, so the work is "
        "published to GitHub for the human to review. "
        "Use a descriptive repo_name based on the task (e.g. 'todo-app-planner', 'auth-service'). "
        "Requires GITHUB_TOKEN to be set in the environment."
    ),
    "input_schema": {
        "type": "object",
        "required": ["repo_name"],
        "properties": {
            "repo_name": {
                "type": "string",
                "description": "GitHub repository name (lowercase, hyphens). Created if it doesn't exist.",
            },
            "branch": {
                "type": "string",
                "description": "Branch to push to (default: 'main').",
            },
            "private": {
                "type": "boolean",
                "description": "Make the repository private (default: false).",
            },
        },
    },
}

_EDIT_FILE_SCHEMA = {
    "name": "edit_file",
    "description": (
        "Edit a file in /workspace by replacing an exact string with a new string. "
        "Use this instead of write_file when making targeted changes to existing code. "
        "The old_string must match EXACTLY (including whitespace/indentation) and must be UNIQUE in the file. "
        "Read the file first with read_file to get the exact text. "
        "Prefer small, focused edits over large replacements."
    ),
    "input_schema": {
        "type": "object",
        "required": ["path", "old_string", "new_string"],
        "properties": {
            "path":       {"type": "string", "description": "File path relative to /workspace."},
            "old_string": {"type": "string", "description": "Exact text to find and replace. Must be unique in the file."},
            "new_string": {"type": "string", "description": "Replacement text."},
        },
    },
}

ALL_SCHEMAS = {
    "write_file":          _WRITE_FILE_SCHEMA,
    "read_file":           _READ_FILE_SCHEMA,
    "list_files":          _LIST_FILES_SCHEMA,
    "execute_code":        _EXECUTE_CODE_SCHEMA,
    "git_commit":          _GIT_COMMIT_SCHEMA,
    "git_status":          _GIT_STATUS_SCHEMA,
    "git_log":             _GIT_LOG_SCHEMA,
    "git_diff":            _GIT_DIFF_SCHEMA,
    "write_memory":        _WRITE_MEMORY_SCHEMA,
    "read_memory":         _READ_MEMORY_SCHEMA,
    "list_memories":       _LIST_MEMORIES_SCHEMA,
    "wiki_write":          _WIKI_WRITE_SCHEMA,
    "wiki_read":           _WIKI_READ_SCHEMA,
    "wiki_search":         _WIKI_SEARCH_SCHEMA,
    "create_task":         _CREATE_TASK_SCHEMA,
    "update_task_status":  _UPDATE_TASK_SCHEMA,
    "search_code":         _SEARCH_CODE_SCHEMA,
    "find_files":          _FIND_FILES_SCHEMA,
    "check_budget":        _CHECK_BUDGET_SCHEMA,
    "edit_file":           _EDIT_FILE_SCHEMA,
    "ask_human":              _ASK_HUMAN_SCHEMA,
    "git_push":               _GIT_PUSH_SCHEMA,
    "git_checkout_branch":    _GIT_CHECKOUT_BRANCH_SCHEMA,
    "git_merge":              _GIT_MERGE_SCHEMA,
}


def build_tool_schemas(allowed_tools: list[str], available_roles: list[str]) -> list[dict]:
    """Return the list of tool dicts to pass to Claude."""
    schemas: list[dict] = []
    if "send_message" in allowed_tools:
        schemas.append(_send_message_schema(available_roles))
    for name in allowed_tools:
        if name in ALL_SCHEMAS:
            schemas.append(ALL_SCHEMAS[name])
    return schemas


# ─────────────────────────────────────────────
# Tool execution
# ─────────────────────────────────────────────

async def execute_tool(
    name: str,
    inputs: dict[str, Any],
    *,
    bus: Any,           # MessageBus, imported lazily to avoid circular
    current_message: Any,  # models.Message
    agent_role: str = "",  # the executing agent's own role
) -> dict[str, Any]:
    """Dispatch a tool call and return a result dict."""
    try:
        if name == "send_message":
            return await _exec_send_message(
                inputs, bus=bus, current_message=current_message, agent_role=agent_role
            )
        elif name == "write_file":
            return _exec_write_file(inputs)
        elif name == "read_file":
            return _exec_read_file(inputs)
        elif name == "list_files":
            return _exec_list_files(inputs)
        elif name == "execute_code":
            return await _exec_execute_code(inputs)
        elif name == "git_commit":
            return _exec_git_commit(inputs)
        elif name == "git_status":
            return _exec_git_status()
        elif name == "git_log":
            return _exec_git_log(inputs)
        elif name == "write_memory":
            return await _exec_write_memory(inputs, agent_role=agent_role)
        elif name == "read_memory":
            return await _exec_read_memory(inputs, agent_role=agent_role)
        elif name == "list_memories":
            return await _exec_list_memories(agent_role=agent_role)
        elif name == "wiki_write":
            return await _exec_wiki_write(inputs, agent_role=agent_role)
        elif name == "wiki_read":
            return await _exec_wiki_read(inputs)
        elif name == "wiki_search":
            return await _exec_wiki_search(inputs)
        elif name == "create_task":
            return await _exec_create_task(inputs, agent_role=agent_role, current_message=current_message)
        elif name == "update_task_status":
            return await _exec_update_task_status(inputs)
        elif name == "search_code":
            return _exec_search_code(inputs)
        elif name == "find_files":
            return _exec_find_files(inputs)
        elif name == "check_budget":
            return await _exec_check_budget(inputs)
        elif name == "edit_file":
            return _exec_edit_file(inputs)
        elif name == "ask_human":
            return await _exec_ask_human(inputs, agent_role=agent_role)
        elif name == "git_diff":
            return _exec_git_diff(inputs)
        elif name == "git_push":
            return _exec_git_push(inputs)
        elif name == "git_checkout_branch":
            return _exec_git_checkout_branch(inputs)
        elif name == "git_merge":
            return _exec_git_merge(inputs)
        else:
            return {"error": f"Unknown tool: {name}"}
    except Exception as exc:
        logger.exception("Tool %s failed: %s", name, exc)
        return {"error": str(exc)}


async def _exec_send_message(
    inputs: dict, *, bus: Any, current_message: Any, agent_role: str = ""
) -> dict:
    from models import Message, MessageType, Priority  # local import

    sender = agent_role or current_message.to_role
    msg = Message(
        from_role=sender,
        to_role=inputs["to_role"],
        type=MessageType(inputs["message_type"]),
        content=inputs["content"],
        thread_id=current_message.thread_id,
        parent_message_id=current_message.id,
        priority=Priority(inputs.get("priority", "normal")),
    )
    await bus.send(msg)
    return {"status": "sent", "to": inputs["to_role"], "message_id": msg.id}


def _safe_path(raw: str) -> pathlib.Path:
    """Resolve a user-supplied relative path under WORKSPACE_ROOT safely."""
    clean = pathlib.Path(raw)
    # Strip any leading slashes / workspace prefix supplied by the model
    parts = clean.parts
    if parts and parts[0] in ("/", "workspace"):
        clean = pathlib.Path(*parts[1:])
    resolved = (WORKSPACE_ROOT / clean).resolve()
    if not str(resolved).startswith(str(WORKSPACE_ROOT)):
        raise PermissionError(f"Path escapes workspace: {resolved}")
    return resolved


def _exec_write_file(inputs: dict) -> dict:
    path = _safe_path(inputs["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(inputs["content"], encoding="utf-8")
    logger.info("write_file: %s (%d bytes)", path, len(inputs["content"]))
    return {"status": "written", "path": str(path.relative_to(WORKSPACE_ROOT))}


def _exec_read_file(inputs: dict) -> dict:
    path = _safe_path(inputs["path"])
    if not path.exists():
        return {"error": f"File not found: {inputs['path']}"}
    content = path.read_text(encoding="utf-8")
    return {"content": content, "path": str(path.relative_to(WORKSPACE_ROOT))}


def _exec_list_files(inputs: dict) -> dict:
    directory = inputs.get("directory", "")
    root = _safe_path(directory) if directory else WORKSPACE_ROOT
    if not root.exists():
        return {"files": [], "note": "Directory does not exist yet."}
    files = [
        str(p.relative_to(WORKSPACE_ROOT))
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]
    return {"files": files, "count": len(files)}


def _exec_git_commit(inputs: dict) -> dict:
    import subprocess
    cwd = WORKSPACE_ROOT
    files: list[str] = inputs.get("files") or []
    message: str = inputs["message"]

    # Stage files
    if files:
        for f in files:
            try:
                safe = _safe_path(f)
                subprocess.run(["git", "add", str(safe)], cwd=cwd, capture_output=True)
            except PermissionError as exc:
                return {"error": str(exc)}
    else:
        subprocess.run(["git", "add", "-A"], cwd=cwd, capture_output=True)

    result = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=cwd, capture_output=True, text=True,
    )
    if result.returncode == 0:
        # Extract short hash from output
        short = result.stdout.split("]")[0].split("[")[-1].strip() if "]" in result.stdout else "?"
        return {"status": "committed", "ref": short, "message": message, "output": result.stdout.strip()}
    else:
        err = result.stderr.strip() or result.stdout.strip()
        return {"status": "failed", "error": err}


def _exec_git_status() -> dict:
    import subprocess
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=WORKSPACE_ROOT, capture_output=True, text=True,
    )
    return {
        "status": result.stdout.strip() or "(clean — nothing to commit)",
        "exit_code": result.returncode,
    }


def _exec_git_log(inputs: dict) -> dict:
    import subprocess
    count = int(inputs.get("count", 5))
    result = subprocess.run(
        ["git", "log", f"--max-count={count}", "--oneline", "--no-decorate"],
        cwd=WORKSPACE_ROOT, capture_output=True, text=True,
    )
    return {
        "log": result.stdout.strip() or "(no commits yet)",
        "exit_code": result.returncode,
    }


async def _exec_execute_code(inputs: dict) -> dict:
    import httpx

    if not inputs.get("code") and not inputs.get("file_path"):
        return {
            "stdout": "", "stderr": "",
            "exit_code": 1, "timed_out": False,
            "summary": "❌ execute_code requires 'code' or 'file_path' — you provided neither. Pass the source code you want to run as the 'code' parameter.",
        }

    sandbox_url = os.environ.get("SANDBOX_URL", "http://sandbox:8081")
    payload = {
        "code": inputs.get("code"),
        "file_path": inputs.get("file_path"),
        "language": inputs.get("language", "python"),
        "working_directory": inputs.get("working_directory", ""),
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{sandbox_url}/execute", json=payload)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError:
        return {"error": "Sandbox unavailable — is it running?", "exit_code": -1, "summary": "❌ Sandbox offline"}
    except Exception as exc:
        return {"error": str(exc), "exit_code": -1, "summary": f"❌ {exc}"}


def _orchestrator_url() -> str:
    return os.environ.get("ORCHESTRATOR_URL", "http://orchestrator:8080")


async def _exec_write_memory(inputs: dict, *, agent_role: str) -> dict:
    import httpx
    key = inputs["key"].strip()
    value = inputs["value"].strip()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.put(
                f"{_orchestrator_url()}/memory/{agent_role}/{key}",
                json={"value": value},
            )
            resp.raise_for_status()
        return {"status": "remembered", "key": key}
    except Exception as exc:
        return {"error": f"Memory write failed: {exc}"}


async def _exec_read_memory(inputs: dict, *, agent_role: str) -> dict:
    import httpx
    key = inputs["key"].strip()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_orchestrator_url()}/memory/{agent_role}/{key}")
            if resp.status_code == 404:
                return {"error": f"No memory found for key '{key}'"}
            resp.raise_for_status()
            data = resp.json()
        return {"key": data["key"], "value": data["value"]}
    except Exception as exc:
        return {"error": f"Memory read failed: {exc}"}


async def _exec_list_memories(*, agent_role: str) -> dict:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_orchestrator_url()}/memory/{agent_role}")
            resp.raise_for_status()
            items = resp.json()
        if not items:
            return {"memories": [], "note": "No memories stored yet."}
        return {"memories": [{"key": i["key"], "value": i["value"]} for i in items]}
    except Exception as exc:
        return {"error": f"Memory list failed: {exc}"}


async def _exec_wiki_write(inputs: dict, *, agent_role: str) -> dict:
    import httpx
    title = inputs["title"].strip()
    content = inputs["content"].strip()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.put(
                f"{_orchestrator_url()}/wiki/{title}",
                json={"content": content, "author": agent_role},
            )
            resp.raise_for_status()
        return {"status": "published", "title": title}
    except Exception as exc:
        return {"error": f"Wiki write failed: {exc}"}


async def _exec_wiki_read(inputs: dict) -> dict:
    import httpx
    title = inputs["title"].strip()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_orchestrator_url()}/wiki/{title}")
            if resp.status_code == 404:
                return {"error": f"No wiki article titled '{title}'. Try wiki_search first."}
            resp.raise_for_status()
            data = resp.json()
        return {"title": data["title"], "content": data["content"], "author": data["author"]}
    except Exception as exc:
        return {"error": f"Wiki read failed: {exc}"}


async def _exec_create_task(inputs: dict, *, agent_role: str, current_message: Any) -> dict:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_orchestrator_url()}/tasks",
                json={
                    "title":       inputs["title"],
                    "description": inputs.get("description", ""),
                    "assignee":    inputs.get("assignee", ""),
                    "thread_id":   inputs.get("thread_id", current_message.thread_id),
                    "created_by":  agent_role,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return {
            "status":   "created",
            "task_id":  data["id"],
            "title":    data["title"],
            "assignee": data["assignee"],
        }
    except Exception as exc:
        return {"error": f"Task creation failed: {exc}"}


async def _exec_update_task_status(inputs: dict) -> dict:
    import httpx
    task_id = inputs["task_id"]
    status  = inputs["status"]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.patch(
                f"{_orchestrator_url()}/tasks/{task_id}",
                json={"status": status},
            )
            if resp.status_code == 404:
                return {"error": f"Task {task_id} not found"}
            if resp.status_code == 422:
                detail = resp.json().get("detail", "quality gate blocked")
                return {"error": f"CI quality gate: {detail}. Fix failing tests first, then retry."}
            data = resp.json()
        note = " (CI pipeline triggered)" if status == "done" else ""
        return {
            "status":     "updated",
            "task_id":    data["id"],
            "new_status": data["status"],
            "note":       note.strip(),
        }
    except Exception as exc:
        return {"error": f"Task update failed: {exc}"}


async def _exec_wiki_search(inputs: dict) -> dict:
    import httpx
    query = inputs["query"].strip()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{_orchestrator_url()}/wiki", params={"q": query})
            resp.raise_for_status()
            articles = resp.json()
        if not articles:
            return {"results": [], "note": f"No wiki articles match '{query}'."}
        return {
            "results": [
                {"title": a["title"], "excerpt": a["content"][:200], "author": a["author"]}
                for a in articles
            ]
        }
    except Exception as exc:
        return {"error": f"Wiki search failed: {exc}"}


def _exec_search_code(inputs: dict) -> dict:
    import subprocess
    query = inputs["query"]
    path  = inputs.get("path", "")
    ci    = [] if inputs.get("case_sensitive", False) else ["-i"]
    root  = _safe_path(path) if path else WORKSPACE_ROOT
    cmd   = [
        "grep", "-rn",
        "--include=*.py", "--include=*.js", "--include=*.ts",
        "--include=*.go", "--include=*.rs", "--include=*.java",
        "--include=*.md", "--include=*.txt", "--include=*.yml",
        "--include=*.yaml", "--include=*.toml",
        *ci, "-m", str(inputs.get("max_results", 40)), query, str(root),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    lines = result.stdout.strip().splitlines()
    relative = []
    for line in lines:
        if str(WORKSPACE_ROOT) in line:
            line = line.replace(str(WORKSPACE_ROOT) + "/", "")
        relative.append(line)
    if not relative and result.returncode != 0:
        return {"matches": [], "count": 0, "note": f"No matches for '{query}'"}
    return {"matches": relative, "count": len(relative)}


def _exec_find_files(inputs: dict) -> dict:
    import fnmatch
    pattern   = inputs["pattern"]
    directory = inputs.get("directory", "")
    root = _safe_path(directory) if directory else WORKSPACE_ROOT
    matches = [
        str(p.relative_to(WORKSPACE_ROOT))
        for p in sorted(root.rglob("*"))
        if p.is_file() and fnmatch.fnmatch(p.name, pattern)
    ]
    return {"files": matches[:100], "count": len(matches)}


async def _exec_check_budget(inputs: dict) -> dict:
    import httpx
    thread_id = inputs["thread_id"]
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{_orchestrator_url()}/threads/{thread_id}/budget")
            if resp.status_code == 404:
                return {"error": "Thread not found"}
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": f"Budget check failed: {exc}"}


def _exec_edit_file(inputs: dict) -> dict:
    path = _safe_path(inputs["path"])
    if not path.exists():
        return {"error": f"File not found: {inputs['path']}. Use write_file to create it."}
    old = inputs["old_string"]
    new = inputs["new_string"]
    content = path.read_text(encoding="utf-8")
    count = content.count(old)
    if count == 0:
        return {"error": "old_string not found in file. Use read_file to verify the exact text."}
    if count > 1:
        return {"error": f"old_string matches {count} locations — add more surrounding context to make it unique."}
    updated = content.replace(old, new, 1)
    path.write_text(updated, encoding="utf-8")
    delta = len(new.splitlines()) - len(old.splitlines())
    return {
        "status": "edited",
        "path": str(path.relative_to(WORKSPACE_ROOT)),
        "line_delta": delta,
    }


async def _exec_ask_human(inputs: dict, *, agent_role: str = "") -> dict:
    import httpx
    url = _orchestrator_url()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(
                f"{url}/threads/{inputs['thread_id']}/ask-human",
                json={
                    "question":  inputs["question"],
                    "context":   inputs.get("context", ""),
                    "from_role": agent_role,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        return {
            "status": "question_submitted",
            "question_id": data["question_id"],
            "message": (
                "Your question has been sent to the human. "
                "Send a status_update to your manager summarizing current progress, "
                "then stop — you will receive a new message when the human replies."
            ),
        }
    except Exception as exc:
        return {"error": f"ask_human failed: {exc}"}


def _exec_git_diff(inputs: dict) -> dict:
    import subprocess
    cmd = ["git", "-C", str(WORKSPACE_ROOT), "diff"]
    if inputs.get("staged"):
        cmd.append("--cached")
    base = inputs.get("base", "")
    if base:
        cmd.append(base)
        target = inputs.get("target", "")
        if target:
            cmd.append(target)
    cmd.append("--")
    path = inputs.get("path", "")
    if path:
        cmd.append(path)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        diff = result.stdout
        if not diff.strip():
            return {"diff": "(no changes)", "lines_changed": 0}
        lines = diff.splitlines()
        if len(lines) > 500:
            diff = "\n".join(lines[:500]) + f"\n... [{len(lines)-500} more lines truncated]"
        changed = len([l for l in lines if l.startswith(("+", "-")) and not l.startswith(("+++", "---"))])
        return {"diff": diff, "lines_changed": changed}
    except subprocess.TimeoutExpired:
        return {"error": "git diff timed out"}
    except Exception as exc:
        return {"error": f"git diff failed: {exc}"}


def _exec_git_checkout_branch(inputs: dict) -> dict:
    import subprocess
    import re
    branch = inputs.get("branch_name", "").strip()
    if not branch:
        return {"error": "branch_name is required"}
    # Sanitise: allow slashes (for feature/xxx style), strip unsafe chars
    branch = re.sub(r"[^a-zA-Z0-9._/\-]", "-", branch)
    result = subprocess.run(
        ["git", "-C", str(WORKSPACE_ROOT), "checkout", "-B", branch],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return {"error": f"git checkout failed: {result.stderr[:300]}"}
    return {"status": "switched", "branch": branch}


def _exec_git_merge(inputs: dict) -> dict:
    import subprocess
    branch = inputs.get("branch_name", "").strip()
    if not branch:
        return {"error": "branch_name is required"}
    strategy = inputs.get("strategy", "").strip()
    cmd = ["git", "-C", str(WORKSPACE_ROOT), "merge", "--no-edit"]
    if strategy == "ours":
        cmd += ["-X", "ours"]
    elif strategy == "theirs":
        cmd += ["-X", "theirs"]
    cmd.append(branch)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        # Check for conflicts
        status = subprocess.run(
            ["git", "-C", str(WORKSPACE_ROOT), "status", "--short"],
            capture_output=True, text=True,
        )
        conflicts = [l for l in status.stdout.splitlines() if l.startswith("UU")]
        return {
            "error": f"Merge failed: {result.stderr[:400]}",
            "conflicting_files": conflicts[:10],
            "tip": "Use strategy='ours' to keep your changes or strategy='theirs' to accept theirs.",
        }
    return {
        "status": "merged",
        "branch_merged": branch,
        "output": result.stdout[:300],
    }


def _exec_git_push(inputs: dict) -> dict:
    import subprocess
    import urllib.request
    import urllib.error
    import json
    import os
    import re

    token    = os.environ.get("GITHUB_TOKEN", "").strip()
    username = os.environ.get("GITHUB_USERNAME", "RajuRoopani").strip()

    if not token:
        return {"error": "GITHUB_TOKEN is not set. Ask an admin to configure it."}

    repo_name = inputs.get("repo_name", "").strip()
    if not repo_name:
        return {"error": "repo_name is required"}

    # Sanitise: lowercase, only hyphens/dots/underscores allowed, max 100 chars
    repo_name = re.sub(r"[^a-zA-Z0-9._-]", "-", repo_name).lower()
    repo_name = re.sub(r"-{2,}", "-", repo_name).strip("-.")[:100]

    branch  = inputs.get("branch", "main")
    private = bool(inputs.get("private", False))
    ws      = str(WORKSPACE_ROOT)

    # ── 1. Ensure workspace has at least one commit ─────────────────
    log_check = subprocess.run(
        ["git", "-C", ws, "log", "--oneline", "-1"],
        capture_output=True, text=True,
    )
    if log_check.returncode != 0 or not log_check.stdout.strip():
        return {"error": "No commits in /workspace. Run git_commit first before pushing."}

    # ── 2. Create GitHub repo (idempotent — 422 means already exists) ─
    api_payload = json.dumps({
        "name": repo_name, "private": private,
        "auto_init": False,
        "description": "Built by Team Claw AI agents",
    }).encode()
    api_headers = {
        "Authorization": f"token {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/vnd.github.v3+json",
        "User-Agent":    "TeamClaw/1.0",
    }
    repo_url = f"https://github.com/{username}/{repo_name}"
    created  = False
    try:
        req = urllib.request.Request(
            "https://api.github.com/user/repos",
            data=api_payload, headers=api_headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            rdata    = json.loads(resp.read())
            repo_url = rdata.get("html_url", repo_url)
            created  = True
    except urllib.error.HTTPError as e:
        if e.code == 422:   # repo already exists — fine
            pass
        elif e.code == 401:
            return {"error": "GITHUB_TOKEN is invalid or expired."}
        elif e.code == 403:
            return {"error": "GITHUB_TOKEN lacks 'repo' scope. Regenerate with full repo permissions."}
        else:
            try:
                body = e.read().decode()[:300]
            except Exception:
                body = ""
            return {"error": f"GitHub API error {e.code}: {body}"}
    except Exception as exc:
        return {"error": f"GitHub API call failed: {exc}"}

    # ── 3. Push directly to URL (no persistent remote credential stored) ─
    remote_url = f"https://{token}@github.com/{username}/{repo_name}.git"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    push = subprocess.run(
        ["git", "-C", ws, "push", remote_url, f"HEAD:{branch}", "--force-with-lease"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    if push.returncode != 0:
        # First push to a brand-new repo has no upstream to lease against — force
        push = subprocess.run(
            ["git", "-C", ws, "push", remote_url, f"HEAD:{branch}", "--force"],
            capture_output=True, text=True, timeout=60, env=env,
        )
    if push.returncode != 0:
        err = push.stderr.replace(token, "***")[:500]   # redact token from logs
        return {"error": f"git push failed: {err}"}

    return {
        "status":   "pushed",
        "repo_url": repo_url,
        "branch":   branch,
        "created":  created,
        "message":  f"{'Created and pushed' if created else 'Pushed'} to {repo_url} (branch: {branch})",
    }

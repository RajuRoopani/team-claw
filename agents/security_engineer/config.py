# Security Engineer — role configuration

ALLOWED_TOOLS = [
    "send_message",
    "read_file",
    "list_files",
    "find_files",
    "search_code",
    "write_file",
    "edit_file",
    "execute_code",
    "git_diff",
    "git_status",
    "git_commit",
    "git_push",
    "wiki_write",
    "wiki_read",
    "wiki_search",
    "write_memory",
    "read_memory",
    "list_memories",
    "create_task",
    "update_task_status",
    "check_budget",
    "ask_human",
]

# Roles this agent is allowed to message
AVAILABLE_ROLES = [
    "engineering_manager",
    "architect",
    "senior_dev_1",
    "senior_dev_2",
    "orchestrator",
]

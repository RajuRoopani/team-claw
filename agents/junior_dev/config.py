ALLOWED_TOOLS = [
    "send_message",
    "write_file",
    "read_file",
    "list_files",
    "execute_code",
    "git_commit",
    "git_status",
    "write_memory",
    "read_memory",
    "list_memories",
    "wiki_read",
    "wiki_search",
    "create_task",
    "update_task_status",
    "search_code",
    "find_files",
    "check_budget",
    "edit_file",
]

# Populated at runtime based on MENTOR_ROLE env var
# Base set — agent.py merges MENTOR_ROLE dynamically
AVAILABLE_ROLES = [
    "engineering_manager",
    "senior_dev_1",   # may be overridden via MENTOR_ROLE
    "senior_dev_2",
]

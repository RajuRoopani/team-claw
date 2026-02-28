from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class MessageType(str, Enum):
    TASK_ASSIGNMENT = "task_assignment"
    QUESTION = "question"
    ANSWER = "answer"
    REVIEW_REQUEST = "review_request"
    REVIEW_FEEDBACK = "review_feedback"
    STATUS_UPDATE = "status_update"
    BLOCKER = "blocker"
    TASK_COMPLETE = "task_complete"
    REQUIREMENT = "requirement"
    ACCEPTANCE_RESULT = "acceptance_result"
    # System types
    HUMAN_INPUT = "human_input"
    AGENT_REPLY = "agent_reply"


class Priority(str, Enum):
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"


@dataclass
class Message:
    from_role: str
    to_role: str
    type: MessageType
    content: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    thread_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    priority: Priority = Priority.NORMAL
    artifacts: list[str] = field(default_factory=list)
    parent_message_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict = field(default_factory=dict)

    def to_redis_dict(self) -> dict[str, str]:
        """Serialize for Redis stream storage (all values must be strings)."""
        import json
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "from_role": self.from_role,
            "to_role": self.to_role,
            "type": self.type.value,
            "content": self.content,
            "priority": self.priority.value,
            "artifacts": json.dumps(self.artifacts),
            "parent_message_id": self.parent_message_id or "",
            "timestamp": self.timestamp.isoformat(),
            "metadata": json.dumps(self.metadata),
        }

    @classmethod
    def from_redis_dict(cls, data: dict) -> "Message":
        import json
        # Redis returns bytes keys/values
        decoded = {
            k.decode() if isinstance(k, bytes) else k: v.decode() if isinstance(v, bytes) else v
            for k, v in data.items()
        }
        return cls(
            id=decoded["id"],
            thread_id=decoded["thread_id"],
            from_role=decoded["from_role"],
            to_role=decoded["to_role"],
            type=MessageType(decoded["type"]),
            content=decoded["content"],
            priority=Priority(decoded["priority"]),
            artifacts=json.loads(decoded.get("artifacts", "[]")),
            parent_message_id=decoded.get("parent_message_id") or None,
            metadata=json.loads(decoded.get("metadata", "{}")),
        )

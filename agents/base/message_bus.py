from __future__ import annotations

import json
import logging
import uuid

import redis.asyncio as aioredis

from models import Message

logger = logging.getLogger(__name__)

AUDIT_STREAM = "team:audit"
THREAD_BUDGET_MAX = 20
THREAD_BUDGET_WARN = 16   # warn at 80%
THREAD_HISTORY_MAX = 30   # messages kept in per-thread ring buffer


class MessageBus:
    """
    Thin wrapper around Redis Streams.

    Each agent has its own inbox stream: agent:{role}:inbox
    Every sent message is also published to the audit stream.
    """

    def __init__(self, redis_url: str, role: str) -> None:
        self.role = role
        self._redis: aioredis.Redis = aioredis.from_url(
            redis_url, decode_responses=False
        )
        self._inbox = f"agent:{role}:inbox"
        self._group = f"grp:{role}"
        self._consumer = f"{role}-0"

    async def setup(self) -> None:
        """Create consumer group (idempotent)."""
        for stream in [self._inbox]:
            try:
                await self._redis.xgroup_create(
                    stream, self._group, id="0", mkstream=True
                )
                logger.info("[%s] Consumer group created for %s", self.role, stream)
            except aioredis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise

    async def send(self, message: Message) -> int:
        """
        Deliver to target inbox and broadcast to audit stream.
        Returns the current thread message count.
        Budget warnings are sent to engineering_manager at THREAD_BUDGET_WARN.
        """
        target_stream = f"agent:{message.to_role}:inbox"
        payload = message.to_redis_dict()
        await self._redis.xadd(target_stream, payload)
        await self._redis.xadd(AUDIT_STREAM, payload)
        logger.debug(
            "[%s] → [%s] %s (thread=%s)",
            message.from_role,
            message.to_role,
            message.type.value,
            message.thread_id[:8],
        )

        # Persist to per-thread history ring buffer (always, including budget msgs)
        history_key = f"thread:{message.thread_id}:history"
        await self._redis.rpush(history_key, json.dumps(message.to_redis_dict()))
        await self._redis.ltrim(history_key, -THREAD_HISTORY_MAX, -1)
        await self._redis.expire(history_key, 86400)

        # Thread budget tracking (skip system budget warnings to avoid recursion)
        is_budget_msg = message.metadata.get("is_budget_warning", False)
        count = 0
        if not is_budget_msg:
            count_key = f"thread:{message.thread_id}:msg_count"
            count = await self._redis.incr(count_key)
            await self._redis.expire(count_key, 86400)  # 24h TTL

            if count == THREAD_BUDGET_WARN:
                await self._send_budget_warning(message.thread_id, count)
            elif count > THREAD_BUDGET_MAX:
                logger.warning(
                    "Thread %s has exceeded budget (%d/%d messages)",
                    message.thread_id[:8], count, THREAD_BUDGET_MAX,
                )

        return count

    async def _send_budget_warning(self, thread_id: str, count: int) -> None:
        from models import MessageType, Priority  # local import to avoid circular

        warning = Message(
            id=str(uuid.uuid4()),
            thread_id=thread_id,
            from_role="system",
            to_role="engineering_manager",
            type=MessageType.STATUS_UPDATE,
            content=(
                f"⚠️ THREAD BUDGET WARNING: Thread {thread_id[:8]} has reached "
                f"{count}/{THREAD_BUDGET_MAX} messages (80%). "
                "Please work toward resolving this thread soon."
            ),
            priority=Priority.HIGH,
            metadata={"is_budget_warning": True, "count": count, "max": THREAD_BUDGET_MAX},
        )
        em_stream = "agent:engineering_manager:inbox"
        await self._redis.xadd(em_stream, warning.to_redis_dict())
        await self._redis.xadd(AUDIT_STREAM, warning.to_redis_dict())
        logger.warning("[system] Budget warning sent for thread %s", thread_id[:8])

    async def receive(
        self, count: int = 5, block_ms: int = 2000
    ) -> list[tuple[bytes, Message]]:
        """
        Read up to `count` undelivered messages.
        Returns list of (redis_stream_id, Message) pairs.
        """
        result = await self._redis.xreadgroup(
            self._group,
            self._consumer,
            {self._inbox: ">"},
            count=count,
            block=block_ms,
        )
        if not result:
            return []

        out: list[tuple[bytes, Message]] = []
        for _stream, entries in result:
            for redis_id, fields in entries:
                try:
                    msg = Message.from_redis_dict(fields)
                    out.append((redis_id, msg))
                except Exception as exc:
                    logger.warning("[%s] Failed to parse message: %s", self.role, exc)
                    await self._ack(redis_id)
        return out

    async def ack(self, redis_id: bytes) -> None:
        await self._redis.xack(self._inbox, self._group, redis_id)

    async def _ack(self, redis_id: bytes) -> None:
        await self.ack(redis_id)

    async def get_thread_history(self, thread_id: str, limit: int = 20) -> list[Message]:
        """Return the most recent `limit` messages in a thread (oldest first)."""
        history_key = f"thread:{thread_id}:history"
        raw = await self._redis.lrange(history_key, -limit, -1)
        messages: list[Message] = []
        for entry in raw:
            try:
                data = json.loads(entry.decode() if isinstance(entry, bytes) else entry)
                messages.append(Message.from_redis_dict({k.encode(): v.encode() if isinstance(v, str) else v for k, v in data.items()}))
            except Exception as exc:
                logger.debug("Failed to parse history entry: %s", exc)
        return messages

    async def close(self) -> None:
        await self._redis.aclose()

"""系统日志 SSE 实时推送 —— 供监控日志页订阅新写入的日志。"""

import asyncio
from typing import Any

_log_sse_queues: dict[asyncio.Queue, int | None] = {}


def register(queue: asyncio.Queue, user_id: int | None = None) -> None:
    """Register a log subscriber in the account (or shared guest) scope."""
    _log_sse_queues[queue] = user_id


def unregister(queue: asyncio.Queue) -> None:
    _log_sse_queues.pop(queue, None)


def broadcast_log(data: dict[str, Any], user_id: int | None = None) -> None:
    """同步广播新日志（write_log 写入 DB 后调用）。"""
    payload = {"type": "log", **data}
    scope_user_id = payload.get("user_id", user_id)
    for queue, client_user_id in list(_log_sse_queues.items()):
        if client_user_id != scope_user_id:
            continue
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def client_count(user_id: int | None = None) -> int:
    return sum(1 for client_user_id in _log_sse_queues.values() if client_user_id == user_id)

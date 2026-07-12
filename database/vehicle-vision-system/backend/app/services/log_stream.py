"""系统日志 SSE 实时推送 —— 供监控日志页订阅新写入的日志。"""

import asyncio
from typing import Any

_log_sse_queues: list[asyncio.Queue] = []


def register(queue: asyncio.Queue) -> None:
    if queue not in _log_sse_queues:
        _log_sse_queues.append(queue)


def unregister(queue: asyncio.Queue) -> None:
    try:
        _log_sse_queues.remove(queue)
    except ValueError:
        pass


def broadcast_log(data: dict[str, Any]) -> None:
    """同步广播新日志（write_log 写入 DB 后调用）。"""
    payload = {"type": "log", **data}
    for queue in _log_sse_queues[:]:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def client_count() -> int:
    return len(_log_sse_queues)

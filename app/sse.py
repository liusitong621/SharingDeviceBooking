"""
SSE 帧编码与心跳。

为什么要手写这几十行而不引三方库：
    SSE 的帧格式极简（event/data/空行），三方库大多还带一层会话管理，
    而我们的「会话」在数据库里，不需要第二套。
    要自己控制的只有两件事：帧编码、以及**心跳**。

心跳为什么必须有：
    Nginx 等网关默认 60s 空闲就断连。而"模型思考 40 秒一个 token 都没吐"是常态，
    没有心跳的话，用户会在第 60 秒看到连接断开，前端却以为是正常结束。
    每 15 秒发一行注释帧（以冒号开头，浏览器会忽略），就能把连接钉住。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

# 心跳间隔（秒）。取 15s：比常见网关的 60s 空闲超时小 4 倍，留足重试余量
HEARTBEAT_SECONDS = 15.0

# 三个响应头缺一不可：
#   Cache-Control: no-transform  —— 禁止代理压缩/改写响应体
#   X-Accel-Buffering: no        —— 关闭 Nginx 的 proxy_buffering
#   （Content-Type 由 StreamingResponse 的 media_type 设置）
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

# 心跳帧：冒号开头是 SSE 的注释语法，客户端不会收到事件，只用来保活
PING = ": ping\n\n"


def frame(event: str, data: dict[str, Any]) -> str:
    """
    编码一个 SSE 帧。

    注意 json.dumps 必须 ensure_ascii=False：否则中文会变成 \\uXXXX，
    体积膨胀 3 倍，且抓包排查时完全不可读。
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


async def pump(queue: "asyncio.Queue[tuple[str, dict] | None]", heartbeat: float = HEARTBEAT_SECONDS) -> AsyncIterator[str]:
    """
    从队列里取 (event, data) 并编码成 SSE 帧；空了就发心跳。

    用队列而不是直接 yield：生产者（检索 + 调模型）随时可能长时间不产出，
    队列让我们能够「等 15 秒没东西就发心跳」，而不是干等。
    queue 收到 None 表示流结束，pump 正常退出。
    """
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=heartbeat)
        except asyncio.TimeoutError:
            yield PING
            continue
        if item is None:
            break
        event, data = item
        yield frame(event, data)

"""
智能体确认接口。

这是整个系统里**唯一**能执行模型意图的入口，所以只有两个端点，且都不接受"自由参数"：

    POST /api/v1/agent/confirm   {pending_id, decision}
    GET  /api/v1/agent/pending/{id}

刻意不接受设备、日期、时段这类参数：那些内容在模型发起时就已经固化进
pending_actions.summary 与 booking_validations 里了。前端改不了，攻击者也改不了。
如果这里允许传时段，那么"确认"就变成了"用旧凭证建新单"的入口。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

from app import db
from app.agent import confirm
from app.auth import current_user
from app.errors import ApiError, trace_id_of

logger = logging.getLogger("app.agent_api")

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])


@router.post("/confirm")
async def confirm_action(
    request: Request, user: dict = Depends(current_user)
) -> dict[str, Any]:
    """
    确认或拒绝一个待确认动作。

    为什么用 POST 而不是页面表单 + 302：
        确认的结果是**结构化的数据**（预约单号、成功/失败原因），
        页面表单只能重定向，前端拿到结果还得再查一次，中间那一秒的
        状态不一致足以让用户重复点击。JSON 返回值最直接。
    """
    trace_id = trace_id_of(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise ApiError("INVALID_REQUEST", "请求体必须是 JSON") from None
    if not isinstance(body, dict):
        raise ApiError("INVALID_REQUEST")

    pending_id = str(body.get("pending_id") or "").strip()
    decision = str(body.get("decision") or "confirm").strip().lower()
    if not pending_id:
        raise ApiError("INVALID_REQUEST", "缺少 pending_id")
    if decision not in ("confirm", "reject"):
        raise ApiError("INVALID_REQUEST", "decision 只能是 confirm 或 reject")

    if decision == "reject":
        result = await confirm.reject_pending(pending_id, user)
        logger.info("[%s] 用户 %s 拒绝了 %s", trace_id, user["emp_no"], pending_id[:8])
        return result

    result = await confirm.confirm_pending(pending_id, user)
    logger.info(
        "[%s] 用户 %s 确认执行 %s -> %s",
        trace_id, user["emp_no"], pending_id[:8], "成功" if result.get("ok") else "失败",
    )
    return result


@router.get("/pending/{pending_id}")
async def get_pending(pending_id: str, user: dict = Depends(current_user)) -> dict[str, Any]:
    """
    查一条待确认动作的当前状态。

    前端刷新页面后用它判断卡片该显示成"待确认"还是"已处理"——
    只靠历史消息里的快照判断会出错（快照是发起时的，之后可能已被处理）。
    """
    p = await db.fetch_pending(pending_id)
    if p is None:
        raise ApiError("PENDING_NOT_FOUND")
    if p["user_id"] != user["id"]:
        raise ApiError("PENDING_FORBIDDEN")
    return {
        "pending_id": p["id"],
        "status": p["status"],
        "action": p["action"],
        "summary": p["summary"],
        "expires_at": p["expires_at"],
        "decided_at": p["decided_at"],
    }

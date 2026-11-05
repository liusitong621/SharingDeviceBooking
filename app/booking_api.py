"""
预约 REST 接口。

为什么预约不做成对话流的一部分（这是整个项目最重要的一条边界）：
    预约需要明确的 409 Conflict 语义和幂等保证。塞进 SSE 里，前端无法可靠判断
    "到底成没成"——流断了、超时了、模型说了"已预约"但库里没有，三种情况在
    客户端看起来一模一样。所以：
        **预约走同步 REST，对话只用于找时段和问规程。**

工具层（后续阶段）也只是调用本文件背后的 booking_service，不另开写入口。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from app import booking_service, db
from app.auth import admin_user, current_user
from app.domain import slots as slot_rules
from app.errors import ApiError

logger = logging.getLogger("app.booking")

router = APIRouter(prefix="/api/v1", tags=["booking"])


def _today() -> str:
    return booking_service.now().date().isoformat()


def _ip(request: Request) -> str | None:
    """
    客户端 IP，只用于写操作留痕。

    不信任 X-Forwarded-For：单机内网直连时它是客户端编的，取它等于让人自己填
    "我是谁"。真要放 Nginx 后面，应该在 Nginx 上覆盖这个头，那时再改这里。
    """
    return request.client.host if request.client else None


# =============================================================================
# 查询
# =============================================================================


@router.get("/devices")
async def list_devices(date: str | None = None, user: dict = Depends(current_user)) -> dict:
    """设备列表 + 指定日期每台的可用格数。默认查今天。"""
    day = date or _today()
    ok, reason = slot_rules.check_date(day, booking_service.now())
    if not ok:
        raise ApiError("DATE_OUT_OF_RANGE", reason, message=reason)
    # 传 user["id"] 让占用视图能标出"我的"（R02）。管理员台账页传 None，
    # 因为那里看的是资产状态，不是"我约了哪格"。
    return await booking_service.list_devices_with_availability(day, user["id"])


@router.get("/devices/{device_id}/slots")
async def device_slots(device_id: str, date: str | None = None, user: dict = Depends(current_user)) -> dict:
    """某设备某天的 6 格状态。日期越界不报错，只标 can_book=False。"""
    day = date or _today()
    if slot_rules.parse_date(day) is None:
        raise ApiError("DATE_OUT_OF_RANGE", "日期格式应为 YYYY-MM-DD", message="日期格式不正确")
    return await booking_service.device_slots(device_id, day, user["id"])


@router.get("/bookings/mine")
async def my_bookings(status: str | None = None, user: dict = Depends(current_user)) -> dict:
    rows = await db.list_user_bookings(user["id"], status)
    return {"bookings": rows, "count": len(rows)}


# =============================================================================
# 写操作
# =============================================================================


@router.post("/bookings", status_code=201)
async def create_booking(request: Request, user: dict = Depends(current_user)) -> dict:
    """
    创建预约。请求体：
        {"device_id": "...", "slot_date": "2026-10-30", "slot_starts": ["10:00"], "remark": ""}

    冲突返回 409 SLOT_TAKEN；超单日上限返回 409 QUOTA_EXCEEDED。
    这两种都是**正常业务分支**，不是异常，前端要能分别提示。
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise ApiError("INVALID_REQUEST")

    device_id = body.get("device_id")
    slot_date = body.get("slot_date")
    starts = body.get("slot_starts")

    if not isinstance(device_id, str) or not device_id:
        raise ApiError("INVALID_REQUEST", "device_id 必填")
    if not isinstance(slot_date, str):
        raise ApiError("INVALID_REQUEST", "slot_date 必填")
    if not isinstance(starts, list) or not all(isinstance(s, str) for s in starts):
        raise ApiError("INVALID_REQUEST", "slot_starts 必须是字符串数组")
    if len(starts) > slot_rules.MAX_SLOTS_PER_DAY:
        raise ApiError(
            "QUOTA_EXCEEDED",
            f"一次最多 {slot_rules.MAX_SLOTS_PER_DAY} 格",
            message=f"每天最多预约 {slot_rules.MAX_SLOTS_PER_DAY} 格",
        )

    remark = body.get("remark")
    result = await booking_service.place_booking(
        user["id"], device_id, slot_date, starts,
        remark if isinstance(remark, str) else None,
        actor=user, ip=_ip(request),
    )
    logger.info("用户 %s 预约成功：%s %s", user["emp_no"], slot_date, starts)
    return result


@router.post("/bookings/{booking_id}/cancel")
async def cancel_booking(booking_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    return await booking_service.cancel(user, booking_id, ip=_ip(request))


@router.post("/bookings/{booking_id}/pickup")
async def pickup_booking(booking_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """登记取走。**刻意不暴露给智能体工具层**：人不在现场却点"已取走"，会污染资产轨迹。"""
    return await booking_service.pickup(user, booking_id, ip=_ip(request))


@router.post("/bookings/{booking_id}/return")
async def return_booking(booking_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """登记归还。同样只允许 UI 触发。请求体可选 {"remark": "...", "returned_at": "2026-11-05T15:40"}。"""
    body: dict = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 空 body 是合法的（只点一下"已归还"）
        body = {}
    if not isinstance(body, dict):
        body = {}
    remark = body.get("remark")
    returned_at = body.get("returned_at")
    return await booking_service.give_back(
        user, booking_id,
        remark if isinstance(remark, str) else None,
        returned_at if isinstance(returned_at, str) else None,
        ip=_ip(request),
    )


# =============================================================================
# 管理员：上下架
# -----------------------------------------------------------------------------
# 这条接口存在的唯一理由是 R07：设备坏了必须能立刻从可约池里摘掉。
# 只改 devices.status，不动任何 booking —— 已约的单不受影响（那是人与人之间的事）。
# =============================================================================


@router.post("/admin/devices/{device_id}/status")
async def set_device_status(device_id: str, request: Request, user: dict = Depends(admin_user)) -> dict:
    body = await request.json()
    status = (body or {}).get("status")
    if status not in ("available", "offline", "borrowed"):
        raise ApiError("INVALID_REQUEST", "status 只能是 available / offline / borrowed")

    device = await db.fetch_device(device_id)
    if device is None:
        raise ApiError("DEVICE_NOT_FOUND")
    await db.set_device_status(device_id, status, actor=user, ip=_ip(request))
    logger.info("管理员 %s 将设备 %s 置为 %s", user["emp_no"], device["asset_no"], status)
    return {"id": device_id, "status": status}


# =============================================================================
# 过期释放（后台任务）
# -----------------------------------------------------------------------------


async def _expire_once() -> int:
    """
    扫一次过期单。时间格式必须与库内 slot_end 可比较：
        slot_end 是 "HH:MM"，所以 cutoff 取 "YYYY-MM-DDTHH:MM"，
        与 SQL 里 slot_date || 'T' || slot_end 拼接出来的串字典序可比。
    """
    cutoff = booking_service.now().strftime("%Y-%m-%dT%H:%M")
    return await db.expire_bookings(cutoff)


async def expire_worker(interval_seconds: int = 60) -> None:
    """
    常驻扫描。写成"幂等 UPDATE + 定时触发"，而不是在请求里顺手清理：
    如果放在请求路径上，没人访问的时段就不会被释放，热门格子会被"幽灵预约"占着。
    """
    while True:
        try:
            n = await _expire_once()
            if n:
                logger.info("过期释放 %d 条预约", n)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 巡检任务绝不能因为一次异常就死掉
            logger.exception("过期扫描失败")
        await asyncio.sleep(interval_seconds)

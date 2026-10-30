"""
预约业务逻辑（服务层）。

**REST 接口与智能体工具层共用这一份逻辑**，这是刻意的：
    如果工具层另写一套"模型专用"的下单路径，两边的校验迟早会分叉，
    而分叉的那一刻，绕过校验的入口就出现了。
    所以工具层也只能调这里的函数，区别仅在于它必须先拿到 validation_id。

本模块只做业务判定，不处理 HTTP：抛 ApiError，由上层转 409/403。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app import db
from app.domain import slots as slot_rules
from app.errors import ApiError


def now() -> datetime:
    """唯一的时间入口。要测"过期释放"和"今天已过的格子"就替换它。"""
    return datetime.now().astimezone()


async def list_devices_with_availability(slot_date: str) -> dict[str, Any]:
    """设备列表 + 指定日期的可用格数，供列表页与工具的 find_available_slots 使用。"""
    devices = await db.list_devices()
    out: list[dict[str, Any]] = []
    for d in devices:
        booked = await db.booked_slots(d["id"], slot_date)
        states = slot_rules.slot_states(slot_date, booked, d["status"], now())
        out.append(
            {
                "id": d["id"],
                "asset_no": d["asset_no"],
                "name": d["name"],
                "model": d["model"],
                "location": d["location"],
                "status": d["status"],
                "free_count": sum(1 for s in states if s["state"] == "free"),
                "slots": states,
            }
        )
    return {"date": slot_date, "devices": out}


async def device_slots(device_id: str, slot_date: str) -> dict[str, Any]:
    device = await db.fetch_device(device_id)
    if device is None:
        raise ApiError("DEVICE_NOT_FOUND")

    ok, reason = slot_rules.check_date(slot_date, now())
    if not ok:
        # 日期越界不影响"看"，只影响"约"。看历史占用是合理需求，
        # 所以这里不抛错，只在返回里标注 can_book=False。
        pass

    booked = await db.booked_slots(device_id, slot_date)
    states = slot_rules.slot_states(slot_date, booked, device["status"], now())
    return {
        "device": {
            "id": device["id"],
            "asset_no": device["asset_no"],
            "name": device["name"],
            "status": device["status"],
            "location": device["location"],
        },
        "date": slot_date,
        "can_book": ok,
        "reason": reason,
        "slots": states,
    }


async def place_booking(
    user_id: str,
    device_id: str,
    slot_date: str,
    slot_starts: list[str],
    remark: str | None = None,
) -> dict[str, Any]:
    """
    下单（REST 与工具层共用的唯一路径）。

    校验顺序刻意从"最不可能变"到"最可能变"排：
    ［设备存在 → 时段合法 → 日期范围 → 单日上限 → 写入冲突］
    前四条是纯规则（可以在插入前给出友好提示），最后一条只能靠唯一索引。
    """
    device = await db.fetch_device(device_id)
    if device is None:
        raise ApiError("DEVICE_NOT_FOUND")
    if device["status"] == "offline":
        raise ApiError("DEVICE_OFFLINE")

    # 去重 + 按时间排序，避免 ["12:00","10:00","12:00"] 这类输入产生奇怪行为
    starts = sorted(set(slot_starts))
    if not starts:
        raise ApiError("INVALID_SLOT", "未选择任何时段")

    pairs: list[tuple[str, str]] = []
    for s in starts:
        end = slot_rules.slot_end_of(s)
        if end is None:
            raise ApiError("INVALID_SLOT", f"{s} 不是合法的时段起点")
        pairs.append((s, end))

    ok, reason = slot_rules.check_date(slot_date, now())
    if not ok:
        code = "DATE_IN_PAST" if "过去" in reason else "DATE_OUT_OF_RANGE"
        raise ApiError(code, reason, message=reason)

    # 今天已经开始的格子不能约。判定用**起始时刻**：10–12 在 10:17 已经算开始，
    # 再约只能是半格，且可能与现场使用者冲突。文案说"已过"即可，不必让用户理解细节。
    today = now().date()
    if slot_rules.parse_date(slot_date) == today:
        states = {s["start"]: s for s in slot_rules.slot_states(slot_date, set(), "available", now())}
        expired = [s for s in starts if states.get(s, {}).get("state") == "passed"]
        if expired:
            raise ApiError(
                "DATE_IN_PAST",
                f"时段已开始：{', '.join(expired)}",
                message=f"时段 {expired[0]} 已经开始，请选择后面的时段",
            )

    used = await db.count_bookings_on_date(user_id, slot_date)
    if used + len(pairs) > slot_rules.MAX_SLOTS_PER_DAY:
        raise ApiError(
            "QUOTA_EXCEEDED",
            f"当日已有 {used} 格，再约 {len(pairs)} 格超出上限",
            message=f"每天最多预约 {slot_rules.MAX_SLOTS_PER_DAY} 格，你当天已有 {used} 格",
        )

    ids = await db.create_bookings(device_id, slot_date, pairs, user_id, remark)
    return {
        "bookings": [
            {
                "id": bid,
                "device_id": device_id,
                "asset_no": device["asset_no"],
                "device_name": device["name"],
                "slot_date": slot_date,
                "slot_start": s,
                "slot_end": e,
                "status": "booked",
            }
            for bid, (s, e) in zip(ids, pairs)
        ],
        "count": len(ids),
    }


async def preflight(
    user_id: str,
    device_id: str,
    slot_date: str,
    slot_starts: list[str],
) -> dict[str, Any]:
    """
    预约前体检（对应智能体工具 T5 validate_booking）。

    它与 place_booking 的关系必须说清楚，否则用起来一定会误解：

        preflight 通过  ≠  一定能约上

    preflight 把**能提前知道的**问题都收集出来（设备状态、时段合法性、日期范围、
    单日配额、当前已被占用的格子），一次全部返回，好让模型给用户完整的替代方案。
    但"这一格在这一秒有没有被别人抢走"永远只能靠下单时的唯一索引判定——
    所以 preflight 只负责"尽量让用户少走一次弯路"，不负责"保证成功"。

    为什么不复用 place_booking：那个函数是**逐条抛错**的（遇到第一个问题就 return），
    适合给 REST 用（用户只需要知道最要紧的那条）。而模型需要的是**全量问题清单**，
    否则它得反复试错，白白消耗轮次。两者的错误模型不同，强行合并会让两边都变难用。
    """
    device = await db.fetch_device(device_id)
    if device is None:
        return {"ok": False, "problems": [{"code": "DEVICE_NOT_FOUND", "message": "设备不存在"}]}

    problems: list[dict[str, str]] = []
    if device["status"] == "offline":
        problems.append({"code": "DEVICE_OFFLINE", "message": f"设备 {device['asset_no']} 已下架，不可预约"})

    starts = sorted(set(slot_starts or []))
    if not starts:
        problems.append({"code": "INVALID_SLOT", "message": "没有选择任何时段"})

    # 时段起点合法性：不合法就直接返回，后面的计算没有意义
    for s in starts:
        if slot_rules.slot_end_of(s) is None:
            problems.append({"code": "INVALID_SLOT", "message": f"{s} 不是合法的时段起点（应为 08:00–18:00 的整点）"})
    if problems:
        return {"ok": False, "problems": problems, "device": _device_brief(device)}

    ok, reason = slot_rules.check_date(slot_date, now())
    if not ok:
        code = "DATE_IN_PAST" if "过去" in reason else "DATE_OUT_OF_RANGE"
        problems.append({"code": code, "message": reason})

    # 今天已开始的格子（判定用起始时刻，与 place_booking 保持一致）
    today = now().date()
    if slot_rules.parse_date(slot_date) == today:
        states = {s["start"]: s for s in slot_rules.slot_states(slot_date, set(), "available", now())}
        for s in starts:
            if states.get(s, {}).get("state") == "passed":
                problems.append({"code": "DATE_IN_PAST", "message": f"时段 {s} 已经开始，不能再约"})

    # 冲突：查当前已占用的格子。注意这只是"此刻"的快照，见函数开头的说明。
    booked = await db.booked_slots(device_id, slot_date)
    conflicts = [s for s in starts if s in booked]
    for s in conflicts:
        problems.append({"code": "SLOT_TAKEN", "message": f"时段 {s} 已被他人预约"})

    used = await db.count_bookings_on_date(user_id, slot_date)
    remain = slot_rules.MAX_SLOTS_PER_DAY - used
    if remain <= 0:
        problems.append({
            "code": "QUOTA_EXCEEDED",
            "message": f"你 {slot_date} 已约满 {slot_rules.MAX_SLOTS_PER_DAY} 格，当天不能再约",
        })
    elif len(starts) > remain:
        problems.append({
            "code": "QUOTA_EXCEEDED",
            "message": f"你 {slot_date} 当天还剩 {remain} 格额度，本次想约 {len(starts)} 格",
        })

    # 替代方案：当天还有空的格子。模型有了它，才能在冲突时不空手而回。
    states = slot_rules.slot_states(slot_date, booked, device["status"], now())
    free = [s["start"] for s in states if s["state"] == "free"]

    return {
        "ok": not problems,
        "problems": problems,
        "conflicts": conflicts,
        "device": _device_brief(device),
        "slot_date": slot_date,
        "requested": starts,
        "free_alternatives": free,
        "quota_left": max(remain, 0),
    }


def _device_brief(device: dict) -> dict[str, Any]:
    return {
        "id": device["id"],
        "asset_no": device["asset_no"],
        "name": device["name"],
        "location": device.get("location"),
        "status": device["status"],
    }


async def cancel(user: dict, booking_id: str) -> dict[str, Any]:
    booking = await db.fetch_booking(booking_id)
    if booking is None:
        raise ApiError("BOOKING_NOT_FOUND")
    if booking["user_id"] != user["id"] and user.get("role") != "admin":
        raise ApiError("BOOKING_FORBIDDEN")
    if booking["status"] != "booked":
        raise ApiError("BOOKING_STATE_INVALID", f"当前状态 {booking['status']} 不可取消")
    if not await db.cancel_booking(booking_id):
        # 并发下可能已被别人改状态；这里如实报状态错误，不假装成功
        raise ApiError("BOOKING_STATE_INVALID", "预约状态已变化，请刷新后重试")
    return {"id": booking_id, "status": "cancelled", "slot_released": True}


async def pickup(user: dict, booking_id: str) -> dict[str, Any]:
    booking = await db.fetch_booking(booking_id)
    if booking is None:
        raise ApiError("BOOKING_NOT_FOUND")
    if booking["user_id"] != user["id"] and user.get("role") != "admin":
        raise ApiError("BOOKING_FORBIDDEN")
    if not await db.mark_picked(booking_id):
        raise ApiError("BOOKING_STATE_INVALID", "只有未取走的预约才能登记取用")
    return {"id": booking_id, "status": "picked"}


async def give_back(user: dict, booking_id: str) -> dict[str, Any]:
    booking = await db.fetch_booking(booking_id)
    if booking is None:
        raise ApiError("BOOKING_NOT_FOUND")
    if booking["user_id"] != user["id"] and user.get("role") != "admin":
        raise ApiError("BOOKING_FORBIDDEN")
    if not await db.mark_returned(booking_id):
        raise ApiError("BOOKING_STATE_INVALID", "该预约已归还或已取消")
    return {"id": booking_id, "status": "returned"}

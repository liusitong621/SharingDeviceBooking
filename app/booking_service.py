"""
预约业务逻辑（服务层）。

**REST 接口与智能体工具层共用这一份逻辑**，这是刻意的：
    如果工具层另写一套"模型专用"的下单路径，两边的校验迟早会分叉，
    而分叉的那一刻，绕过校验的入口就出现了。
    所以工具层也只能调这里的函数，区别仅在于它必须先拿到 validation_id。

本模块只做业务判定，不处理 HTTP：抛 ApiError，由上层转 409/403。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app import db
from app.domain import slots as slot_rules
from app.errors import ApiError


def now() -> datetime:
    """唯一的时间入口。要测"过期释放"和"今天已过的格子"就替换它。"""
    return datetime.now().astimezone()


async def list_devices_with_availability(slot_date: str, me_id: str | None = None) -> dict[str, Any]:
    """
    设备列表 + 指定日期的可用格数，供列表页与工具的 find_available_slots 使用。

    me_id 决定能否标出"我的预约"（R02）。传 None 时退化成"全部占用一视同仁"，
    管理员台账页正是这个视角——他看别人的单和看自己的单没区别。
    """
    devices = await db.list_devices()
    out: list[dict[str, Any]] = []
    for d in devices:
        booked = await db.booked_slots(d["id"], slot_date)
        states = slot_rules.slot_states(slot_date, booked, d["status"], now(), me_id)
        out.append(
            {
                "id": d["id"],
                "asset_no": d["asset_no"],
                "name": d["name"],
                "model": d["model"],
                "location": d["location"],
                "status": d["status"],
                "note": d.get("note"),
                "free_count": sum(1 for s in states if s["state"] == "free"),
                "slots": states,
            }
        )
    return {"date": slot_date, "devices": out}


async def device_slots(device_id: str, slot_date: str, me_id: str | None = None) -> dict[str, Any]:
    device = await db.fetch_device(device_id)
    if device is None:
        raise ApiError("DEVICE_NOT_FOUND")

    ok, reason = slot_rules.check_date(slot_date, now())
    if not ok:
        # 日期越界不影响"看"，只影响"约"。看历史占用是合理需求，
        # 所以这里不抛错，只在返回里标注 can_book=False。
        pass

    booked = await db.booked_slots(device_id, slot_date)
    states = slot_rules.slot_states(slot_date, booked, device["status"], now(), me_id)
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
    *,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
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

    ids = await db.create_bookings(device_id, slot_date, pairs, user_id, remark, actor=actor, ip=ip)
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
        # 自己已经约过的那一格也在这里，但说"已被他人预约"会让用户以为被人抢了，
        # 他会去找管理员而不是回「我的预约」看看。文案必须区分。
        if str(booked.get(s)) == str(user_id):
            problems.append({"code": "SLOT_TAKEN", "message": f"时段 {s} 你自己已经约过一单了"})
        else:
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
    states = slot_rules.slot_states(slot_date, booked, device["status"], now(), user_id)
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


async def cancel(user: dict, booking_id: str, *, ip: str | None = None) -> dict[str, Any]:
    booking = await db.fetch_booking(booking_id)
    if booking is None:
        raise ApiError("BOOKING_NOT_FOUND")
    if booking["user_id"] != user["id"] and user.get("role") != "admin":
        raise ApiError("BOOKING_FORBIDDEN")
    if booking["status"] != "booked":
        raise ApiError("BOOKING_STATE_INVALID", f"当前状态 {booking['status']} 不可取消")
    if not await db.cancel_booking(booking_id, actor=user, ip=ip):
        # 并发下可能已被别人改状态；这里如实报状态错误，不假装成功
        raise ApiError("BOOKING_STATE_INVALID", "预约状态已变化，请刷新后重试")
    return {"id": booking_id, "status": "cancelled", "slot_released": True}


async def pickup(user: dict, booking_id: str, *, ip: str | None = None) -> dict[str, Any]:
    booking = await db.fetch_booking(booking_id)
    if booking is None:
        raise ApiError("BOOKING_NOT_FOUND")
    if booking["user_id"] != user["id"] and user.get("role") != "admin":
        raise ApiError("BOOKING_FORBIDDEN")
    if not await db.mark_picked(booking_id, actor=user, ip=ip):
        raise ApiError("BOOKING_STATE_INVALID", "只有未取走的预约才能登记取用")
    return {"id": booking_id, "status": "picked"}


def _normalize_returned_at(raw: str | None, now_dt: datetime) -> str | None:
    """
    把用户填的归还时间规整成库内格式。

    接受两种输入：完整 datetime-local（2026-11-05T15:40）与只有时分（15:40）。
    后者按今天补日期——但**只有当天归还才能这么补**，否则"15:40"会被记成
    一个莫名其妙的日期。所以时分形态只在 slot_date 就是今天时放行，由调用方保证。

    允许最多 5 分钟的时钟前移（用户手机时间快一点是常态），
    超过就是填错了，直接拒绝而不是悄悄截断。
    """
    if not raw or not raw.strip():
        return None
    value = raw.strip().replace(" ", "T")
    if len(value) == 5 and value[2] == ":":
        value = f"{now_dt.date().isoformat()}T{value}"
    parsed: datetime | None = None
    if len(value) == 16:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
    if parsed is None:
        raise ApiError("INVALID_RETURN_TIME", f"归还时间格式应为 2026-11-05T15:40，收到 {raw}")
    parsed = parsed.astimezone()
    if parsed > now_dt + timedelta(minutes=5):
        raise ApiError("INVALID_RETURN_TIME", "归还时间不能是未来")
    return parsed.isoformat(timespec="seconds")


async def give_back(
    user: dict,
    booking_id: str,
    remark: str | None = None,
    returned_at: str | None = None,
    *,
    ip: str | None = None,
) -> dict[str, Any]:
    """
    登记归还（R05：归还填实际时间与备注）。

    两个字段都可空：只点一个"已归还"按钮也应该能成功，
    强制填表只会让人随手打个"无"。
    """
    booking = await db.fetch_booking(booking_id)
    if booking is None:
        raise ApiError("BOOKING_NOT_FOUND")
    if booking["user_id"] != user["id"] and user.get("role") != "admin":
        raise ApiError("BOOKING_FORBIDDEN")

    if remark is not None and len(remark) > 200:
        raise ApiError("INVALID_REQUEST", "备注不能超过 200 字")

    stamp = _normalize_returned_at(returned_at, now())
    # 没填归还时间时，库里会落服务端当前时间（db.mark_returned 的 `returned_at or _now()`）。
    # 这里把同一个值补上，是为了让**返回值和库里一致**：之前返回 None，
    # 调用方（页面、自检脚本）拿到的 returned_at 和实际落库的不是一回事。
    # 格式与 db._now() 保持同一口径。
    if stamp is None:
        stamp = now().isoformat(timespec="seconds")
    if not await db.mark_returned(booking_id, remark, stamp, actor=user, ip=ip):
        raise ApiError("BOOKING_STATE_INVALID", "该预约已归还或已取消")
    return {"id": booking_id, "status": "returned", "returned_at": stamp}


async def force_close(user: dict, booking_id: str, reason: str, *, ip: str | None = None) -> dict[str, Any]:
    """
    管理员强制结束（R06）。

    只有管理员能调——这里的校验不是装饰：口子一旦开给员工，
    "约不到就把它强制结束"会成为绕过冲突判定的后门。
    """
    if user.get("role") != "admin":
        raise ApiError("FORBIDDEN", "只有管理员可以强制结束预约")
    reason = (reason or "").strip()
    if len(reason) > 100:
        raise ApiError("INVALID_REQUEST", "处置说明不能超过 100 字")
    return await db.force_close_booking(booking_id, reason=reason, actor=user, ip=ip)

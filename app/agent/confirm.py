"""
待确认动作：模型的写意图在这里落库，也在**这里**被真正执行。

这个模块存在的全部意义，可以用一句话说清：

    模型可以推进流程，但永远无法自己完成最后一步。

三个环节严格分离，缺一条就会退回"模型能写库"：
    1) 模型调用 request_booking  →  harness 拦截，**不执行**；
    2) 拦截时在这里 prepare_*  →  校验凭证真伪，生成给用户看的摘要；
    3) 用户点击确认            →  走 confirm_pending，此时才碰 place_booking。

为什么确认后的执行不能"回头再问一次模型"：
    一旦把执行结果回填给模型，它就有机会改写事实（"已为您预约成功，共 2 格"，
    而实际只成功 1 格）。确认结果必须由服务端生成结构化数据、前端直接渲染，
    这条链路上不给模型留任何改写的位置。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app import booking_service, db
from app.domain import slots as slot_rules
from app.errors import ApiError


# =============================================================================
# 第一步：拦截时的准备（不写业务数据，只校验凭证 + 造摘要）
# =============================================================================


async def prepare_request_booking(ctx: Any, validation_id: str) -> dict[str, Any]:
    """
    校验凭证并生成确认摘要。

    这里必须真查一次凭证，不能信任模型传来的 id：
    模型会幻觉出一个格式正确但根本不存在的 UUID，甚至可能拿别人的凭证。
    如果直接发确认卡片给用户，用户看到"确认预约"点下去必然失败——
    比直接告诉模型"凭证无效"体验差得多。
    """
    if not validation_id:
        raise ApiError("VALIDATION_REQUIRED")

    v = await db.fetch_validation(validation_id)
    if v is None:
        raise ApiError("VALIDATION_NOT_FOUND")
    if v["user_id"] != ctx.user_id:
        # 凭证绑定签发者。别人拿到 id 也用不了，这里如实拒绝。
        raise ApiError("VALIDATION_MISMATCH", "凭证不属于当前用户")
    if v["consumed_at"]:
        raise ApiError("VALIDATION_CONSUMED")

    now = booking_service.now()
    try:
        expires = datetime.fromisoformat(v["expires_at"])
    except (TypeError, ValueError):
        raise ApiError("VALIDATION_EXPIRED") from None
    if now >= expires:
        raise ApiError("VALIDATION_EXPIRED")

    device = await db.fetch_device(v["device_id"])
    if device is None:
        raise ApiError("DEVICE_NOT_FOUND")

    starts = sorted(v["slot_starts"] or [])
    ends = [slot_rules.slot_end_of(s) or "" for s in starts]
    return {
        "kind": "booking",
        "title": "确认预约",
        "asset_no": device["asset_no"],
        "device_name": device["name"],
        "location": device.get("location"),
        "slot_date": v["slot_date"],
        "slots": [{"start": s, "end": e} for s, e in zip(starts, ends)],
        "slot_text": f"{starts[0]}–{ends[-1]}" if starts else "",
        "count": len(starts),
        # 摘要里带上"确认后会发生什么"，用户点之前就知道后果
        "effect": f"将占用 {device['asset_no']} 在 {v['slot_date']} 的 {len(starts)} 个时段",
        "expires_at": v["expires_at"],
    }


async def prepare_cancel(ctx: Any, booking_id: str) -> dict[str, Any]:
    """取消预约同样要核对：存在、属于本人、当前状态可取消。"""
    if not booking_id:
        raise ApiError("INVALID_ARGUMENT", "booking_id 不能为空")

    b = await db.fetch_booking(booking_id)
    if b is None:
        raise ApiError("BOOKING_NOT_FOUND")
    if b["user_id"] != ctx.user_id:
        raise ApiError("BOOKING_FORBIDDEN")
    if b["status"] != "booked":
        raise ApiError("BOOKING_STATE_INVALID", f"当前状态 {b['status']} 不可取消")

    device = await db.fetch_device(b["device_id"])
    return {
        "kind": "cancel",
        "title": "确认取消预约",
        "asset_no": (device or {}).get("asset_no", ""),
        "device_name": (device or {}).get("name", ""),
        "slot_date": b["slot_date"],
        "slot_text": f"{b['slot_start']}–{b['slot_end']}",
        "count": 1,
        "effect": "取消后这一格会立刻释放，其他人可以约",
        "booking_id": booking_id,
    }


# =============================================================================
# 第二步：用户点击后的执行
# =============================================================================


async def confirm_pending(pending_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """
    执行一个待确认动作。**只有用户点击能走到这里。**

    时序是刻意的，每一步都在防一种具体的失败：

        1) 取 pending 并校验归属/过期   → 防"确认别人的单""确认过期卡片"
        2) 原子 claim（pending→confirmed）→ 防"连点两下建两单"
        3) consume 凭证（原子 UPDATE）   → 第二道幂等闸门，防 2) 被绕过
        4) place_booking                 → 唯一索引兜底，防并发抢格
        5) 失败则标 failed               → 防卡片永远停在"已确认"

    第 2 步和第 3 步看似重复，但它们防的不是同一件事：
    2) 防同一个 pending 被点两次，3) 防同一张凭证被两个 pending 共用。
    少任何一道，都能构造出双单的场景。
    """
    p = await db.fetch_pending(pending_id)
    if p is None:
        raise ApiError("PENDING_NOT_FOUND")
    if p["user_id"] != user["id"]:
        raise ApiError("PENDING_FORBIDDEN")
    if p["status"] != "pending":
        # confirmed / rejected / expired / failed 全部走这一条。
        # 文案统一说"已处理过"，不暴露具体状态（用户不需要区分）。
        raise ApiError("PENDING_ALREADY_DECIDED")
    if p["expires_at"] <= booking_service.now().isoformat(timespec="seconds"):
        await db.decide_pending(pending_id, user["id"], "expired")
        raise ApiError("PENDING_EXPIRED")

    if not await db.decide_pending(pending_id, user["id"], "confirmed"):
        # 走到这里说明另一个请求刚刚抢先 claim 了（并发点击）
        raise ApiError("PENDING_ALREADY_DECIDED")

    try:
        if p["action"] == "request_booking":
            result = await _do_request_booking(p, user)
        else:
            result = await _do_cancel(p, user)
    except ApiError:
        # 执行失败：标 failed 并原样抛出，让用户看到真实原因
        # （最常见的是 SLOT_TAKEN——点下去的那一秒被别人抢了）
        await db.mark_pending_failed(pending_id)
        raise
    except Exception:
        await db.mark_pending_failed(pending_id)
        raise

    return result


async def _do_request_booking(p: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    """下单。凭证消费与落单是两步，但顺序不能反——先消费，再落单。"""
    v = await db.consume_validation(p["validation_id"], user["id"])
    if v is None:
        # 凭证已被用掉（重复确认）或已过期
        raise ApiError("VALIDATION_CONSUMED")

    result = await booking_service.place_booking(
        user_id=user["id"],
        device_id=v["device_id"],
        slot_date=v["slot_date"],
        slot_starts=v["slot_starts"],
    )

    summary = p["summary"]
    text = (
        f"预约成功：{summary.get('asset_no', '')} {summary.get('device_name', '')}，"
        f"{summary.get('slot_date', '')} 的 {summary.get('slot_text', '')}。"
        f"请按时到 {summary.get('location') or '设备存放点'} 取用。"
    )
    await _record(user, p["session_id"], text, {"kind": "booking_confirmed", "bookings": result["bookings"]})
    return {"ok": True, "action": "request_booking", "message": text, "bookings": result["bookings"]}


async def _do_cancel(p: dict[str, Any], user: dict[str, Any]) -> dict[str, Any]:
    result = await booking_service.cancel(user, p["booking_id"])
    summary = p["summary"]
    text = (
        f"已取消 {summary.get('asset_no', '')} 在 {summary.get('slot_date', '')} "
        f"{summary.get('slot_text', '')} 的预约，该时段已释放。"
    )
    await _record(user, p["session_id"], text, {"kind": "booking_cancelled", "booking": result})
    return {"ok": True, "action": "cancel_booking", "message": text}


async def reject_pending(pending_id: str, user: dict[str, Any]) -> dict[str, Any]:
    """用户点了"取消"。只改状态，什么都不执行。"""
    p = await db.fetch_pending(pending_id)
    if p is None:
        raise ApiError("PENDING_NOT_FOUND")
    if p["user_id"] != user["id"]:
        raise ApiError("PENDING_FORBIDDEN")
    if not await db.decide_pending(pending_id, user["id"], "rejected"):
        raise ApiError("PENDING_ALREADY_DECIDED")

    text = "好的，这次预约没有提交。如果时段不合适，可以让我帮你查其他空闲时段。"
    await _record(user, p["session_id"], text, {"kind": "booking_rejected"})
    return {"ok": True, "action": "rejected", "message": text}


async def _record(user: dict[str, Any], session_id: str, text: str, meta: dict[str, Any]) -> None:
    """
    把结果写进对话历史。

    为什么必须写：用户点完确认，界面上多了一条"预约成功"。如果这条只存在于前端，
    刷新页面就没了，用户会怀疑"到底约上没有"，而历史里也查不到这一单的由来。
    审计要求"按员工筛选对话记录"时，这条正是最该被看到的内容。
    """
    try:
        await db.append_message(
            session_id,
            "assistant",
            text,
            citations=[meta],   # 复用 citations 字段承载结构化结果，前端据 kind 渲染成卡片
            status="ok",
        )
    except Exception:  # noqa: BLE001 - 落库失败不能影响"预约已经成功"这个事实
        pass

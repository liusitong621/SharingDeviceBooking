"""
一条命令自检：跑完全部单元测试，再按需求 R01–R08 逐条做一次工具侧核对。

    .venv/bin/python scripts/verify.py

为什么要分两层，而不是只跑单元测试
------------------------------------
单元测试回答"每个函数对不对"，评委问的是"这几条需求做到了没有"。
两者的差距在答辩现场很具体：130 项测试全绿，但没人能一眼看出
"R03 并发不超卖"到底验证过没有。所以这里再按需求编号跑一遍端到端断言，
把"测试通过"翻译成"需求 X 有证据"。

**这里的 PASS 只代表工具侧可核对，不代表业务验收通过。**
比如 R03 的 PASS 是"8 个并发请求只成了 1 单"，它证明不了"50 人同时抢也不出问题"——
那需要压测。同理 R08 证明的是权限判定在服务层，不是"上线后不会越权"。

退出码：全部通过 0，任一项失败 1（可直接接进 CI 或 pre-commit）。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import tempfile
import unicodedata
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import booking_service, db, seed  # noqa: E402
from app.domain import slots as slot_rules  # noqa: E402
from app.errors import ApiError  # noqa: E402

# 自检只输出结论：seed 的 "已写入演示账号" 之类日志会混进表格里
logging.getLogger("app").setLevel(logging.ERROR)

WIDTH = 74
CHECKS: list[tuple[str, str, Callable[[], Awaitable[tuple[bool, str]]]]] = []


# =============================================================================
# 输出
# =============================================================================


def pad(text: str, width: int) -> str:
    shown = sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)
    return text + " " * max(0, width - shown)


def rule(char: str = "─") -> None:
    print(char * WIDTH)


def verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def check(rid: str, title: str):
    """注册一条需求检查。rid 直接用需求编号，方便和 docs/需求追溯.md 对照。"""

    def deco(fn: Callable[[], Awaitable[tuple[bool, str]]]):
        CHECKS.append((rid, title, fn))
        return fn

    return deco


# =============================================================================
# 每一幕共用的上下文（临时库 + 两个员工）
# =============================================================================


class Ctx:
    zhang: dict[str, Any]
    li: dict[str, Any]
    admin: dict[str, Any]
    devices: list[dict[str, Any]]
    today: str
    booking_for_r07: str = ""


C = Ctx()


def day(offset: int) -> str:
    return (booking_service.now() + timedelta(days=offset)).date().isoformat()


# =============================================================================
# 需求检查
# =============================================================================


@check("R01", "时段与日配额")
async def r01() -> tuple[bool, str]:
    """
    6 格 / 08:00–20:00 / 每天上限 2 格 / 今天已开始的格子不可约。

    "已开始"按起始时刻判定，所以这里用 now 反推期望值，
    而不是写死"08:00 一定是 passed"——早上 7 点跑这条检查就不该失败。
    """
    states = slot_rules.slot_states(day(1), {}, "available", booking_service.now())
    if len(states) != 6:
        return False, f"时段数应为 6，实际 {len(states)}"
    if [s["start"] for s in states][0] != "08:00" or states[-1]["end"] != "20:00":
        return False, "时段边界不是 08:00–20:00"
    if slot_rules.MAX_SLOTS_PER_DAY != 2:
        return False, f"单日上限应为 2，实际 {slot_rules.MAX_SLOTS_PER_DAY}"

    now = booking_service.now()
    today_states = slot_rules.slot_states(C.today, {}, "available", now)
    expected_passed = [s["start"] for s in today_states if s["state"] == "passed"]
    for s in today_states:
        hh = int(s["start"].split(":")[0])
        should = now.hour >= hh
        if should and s["state"] != "passed":
            return False, f"{s['start']} 应已过，实际 {s['state']}"

    try:
        await booking_service.place_booking(
            C.zhang["id"], C.devices[0]["id"], day(1), ["08:00", "10:00", "12:00"]
        )
    except ApiError as e:
        if e.code != "QUOTA_EXCEEDED":
            return False, f"超上限应报 QUOTA_EXCEEDED，实际 {e.code}"
    else:
        return False, "约 3 格竟然成功了"

    return True, f"6 格 08:00–20:00 · 上限 {slot_rules.MAX_SLOTS_PER_DAY} 格 · 今日已过 {len(expected_passed)} 格"


@check("R02", "我的预约可见")
async def r02() -> tuple[bool, str]:
    """"mine" 这一态是 R02 的验收要求：不能只显示"被占"。"""
    one = {"08:00": C.zhang["id"]}
    mine = slot_rules.slot_states(day(2), one, "available", booking_service.now(), C.zhang["id"])
    other = slot_rules.slot_states(day(2), one, "available", booking_service.now(), C.li["id"])
    s_mine = next(s for s in mine if s["start"] == "08:00")["state"]
    s_other = next(s for s in other if s["start"] == "08:00")["state"]
    if s_mine != "mine" or s_other != "taken":
        return False, f"本人应见 mine、他人应见 taken，实际 {s_mine} / {s_other}"
    return True, "本人的格子 mine，他人 taken"


@check("R03", "并发抢同一格不超卖")
async def r03() -> tuple[bool, str]:
    """
    8 个请求同时抢同一格，只能成 1 单。

    判据只看"成功数"，不看失败原因：并发下失败可能是 SLOT_TAKEN（唯一索引）
    也可能是 QUOTA_EXCEEDED（单日上限），两者都算"没多卖"。
    """
    results = await asyncio.gather(
        *[
            booking_service.place_booking(C.zhang["id"], C.devices[1]["id"], day(3), ["08:00"])
            for _ in range(8)
        ],
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    codes = sorted({r.code for r in results if isinstance(r, ApiError)})
    if len(ok) != 1:
        return False, f"8 个并发请求成功了 {len(ok)} 单"
    return True, f"8 并发 → 成功 1 单，其余 {', '.join(codes) or '—'}"


@check("R04", "取消即时释放")
async def r04() -> tuple[bool, str]:
    d = day(4)
    made = await booking_service.place_booking(C.zhang["id"], C.devices[0]["id"], d, ["12:00"])
    bid = made["bookings"][0]["id"]
    await booking_service.cancel(C.zhang, bid)

    # 同一格换个人再约，必须成功——释放不是"页面上看起来空了"，而是库里真的空了
    await booking_service.place_booking(C.li["id"], C.devices[0]["id"], d, ["12:00"])
    C.booking_for_r07 = bid
    return True, "取消后他人可立即约同一格"


@check("R05", "取用与归还留痕")
async def r05() -> tuple[bool, str]:
    made = await booking_service.place_booking(C.zhang["id"], C.devices[2]["id"], day(5), ["14:00"])
    bid = made["bookings"][0]["id"]
    await booking_service.pickup(C.zhang, bid)
    res = await booking_service.give_back(C.zhang, bid, remark="外壳有划痕", returned_at="15:40")
    if res["status"] != "returned" or not res["returned_at"]:
        return False, f"归还未落时间：{res}"
    try:
        await booking_service.give_back(C.zhang, bid)
    except ApiError as e:
        if e.code != "BOOKING_STATE_INVALID":
            return False, f"重复归还应报 BOOKING_STATE_INVALID，实际 {e.code}"
    else:
        return False, "重复归还竟然成功"
    return True, f"取用 → 归还（{res['returned_at'][11:16]}），重复归还被拒"


@check("R06", "下架设备不可约")
async def r06() -> tuple[bool, str]:
    dev = C.devices[2]
    await db.set_device_status(dev["id"], "offline", actor=C.admin)
    try:
        await booking_service.place_booking(C.zhang["id"], dev["id"], day(6), ["16:00"])
    except ApiError as e:
        if e.code != "DEVICE_OFFLINE":
            return False, f"应报 DEVICE_OFFLINE，实际 {e.code}"
    else:
        return False, "下架设备竟然能约"
    finally:
        await db.set_device_status(dev["id"], "available", actor=C.admin)

    states = slot_rules.slot_states(day(6), {}, "offline", booking_service.now())
    if all(s["state"] == "offline" for s in states):
        return True, "下架后 6 格全锁，API 也拒绝下单"
    return False, "下架状态的格子未全锁"


@check("R07", "任一预约可还原轨迹")
async def r07() -> tuple[bool, str]:
    """
    两层核对：
      1) 这笔预约的日志链完整（create + cancel 至少 2 条）；
      2) 源码里没有改日志的语句 —— "不可改"是代码层保证，不是口头承诺。
    """
    timeline = await db.booking_timeline(C.booking_for_r07)
    actions = [r["action"] for r in timeline]
    if "booking.create" not in actions or "booking.cancel" not in actions:
        return False, f"轨迹不完整：{actions}"

    src = (ROOT / "app" / "db.py").read_text(encoding="utf-8")
    for bad in ("UPDATE op_logs", "DELETE FROM op_logs"):
        if bad in src:
            return False, f"db.py 里出现了 {bad}"

    return True, f"一笔预约 {len(timeline)} 条日志，db.py 无 UPDATE/DELETE op_logs"


@check("R08", "权限边界在服务层")
async def r08() -> tuple[bool, str]:
    made = await booking_service.place_booking(C.zhang["id"], C.devices[1]["id"], day(7), ["18:00"])
    bid = made["bookings"][0]["id"]

    try:
        await booking_service.cancel(C.li, bid)
    except ApiError as e:
        if e.code != "BOOKING_FORBIDDEN":
            return False, f"越权取消应报 BOOKING_FORBIDDEN，实际 {e.code}"
    else:
        return False, "员工竟然能取消他人的预约"

    try:
        await booking_service.force_close(C.zhang, bid, "测试")
    except ApiError as e:
        if e.code not in ("FORBIDDEN", "BOOKING_FORBIDDEN"):
            return False, f"员工强制结束应被拒，实际 {e.code}"
    else:
        return False, "员工竟然能强制结束预约"

    return True, "越权取消 / 强制结束均被服务层拒绝"


# =============================================================================
# 单元测试
# =============================================================================


def run_unit_tests() -> tuple[bool, str, str]:
    """在子进程里跑，避免测试进程与检查进程共用同一个模块状态。"""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    out = (proc.stdout + proc.stderr).strip()
    tail = "\n".join(line for line in out.splitlines() if line.strip())[-600:]

    count = "?"
    for line in out.splitlines():
        if line.startswith("Ran ") and " test" in line:
            count = line.split()[1]
            break
    ok = proc.returncode == 0 and "OK" in out.splitlines()[-1]
    return ok, f"Ran {count} tests", tail if not ok else ""


# =============================================================================
# 主流程
# =============================================================================


async def prepare() -> None:
    db.DB_PATH = Path(tempfile.mkdtemp(prefix="sdb-verify-")) / "chat.db"
    db.init_db()
    await seed.seed_if_empty()

    users = {u["emp_no"]: u for u in await db.list_users()}
    C.zhang, C.li, C.admin = users["1001"], users["1002"], users["admin"]
    C.devices = await db.list_devices()
    C.today = booking_service.now().date().isoformat()


async def main() -> int:
    print()
    rule("═")
    print(" 共享设备借用助手 · 自检")
    print(f" 项目根目录 {ROOT}")
    rule("═")
    print()

    ok, detail, tail = run_unit_tests()
    print(f" {pad('单元测试', 26)}{verdict(ok):<7}{detail}")
    if tail:
        print()
        for line in tail.splitlines():
            print(f"   {line}")
        print()

    await prepare()

    failures = 0 if ok else 1
    for rid, title, fn in CHECKS:
        try:
            passed, why = await fn()
        except Exception as exc:  # noqa: BLE001 - 检查本身出错也要算失败，不能吞掉
            passed, why = False, f"{type(exc).__name__}: {exc}"
        if not passed:
            failures += 1
        print(f" {pad(f'{rid} {title}', 26)}{verdict(passed):<7}{why}")

    total = len(CHECKS) + 1
    print()
    rule()
    if failures == 0:
        print(f" {total} 项全部通过。这里的 PASS 只代表工具侧可核对，不代表业务验收通过。")
    else:
        print(f" {total - failures} 项通过，{failures} 项失败。")
    rule()
    print()
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

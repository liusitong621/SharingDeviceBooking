"""
时段规则（纯函数，零 IO）。

规则来自一页纸清单 §四：
    08:00–20:00，每 2 小时一格，共 6 格；
    只能预约今天及未来 14 天；
    每天最多 2 格；
    边界相接不算冲突（10–12 与 12–14 可以同时被两个人约上）。

最后一条为什么天然成立，值得写清楚：
    唯一索引建在 (device_id, slot_date, slot_start) 上，10:00 和 12:00 是
    两个不同的 slot_start，压根不会撞。**不要为了"严谨"再加一层区间重叠校验**，
    那会把一个唯一索引能解决的问题变成一段有 bug 的区间运算。

所有函数都接收 now 参数而不自己取时间：
    "今天已过去的格子不能约"这条规则必须能在测试里被断言，
    函数内部调 datetime.now() 的话，这条规则就只能靠人在特定时间点手工点。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# 时段定义：起点小时 + 固定时长
START_HOUR = 8
END_HOUR = 20
SLOT_HOURS = 2

# 单日上限与可约天数（与需求清单一致，改这里就是改规则）
MAX_SLOTS_PER_DAY = 2
BOOKING_WINDOW_DAYS = 14


def all_slots() -> list[tuple[str, str]]:
    """返回全部时段的 (start, end)，形如 ("08:00", "10:00")。"""
    slots: list[tuple[str, str]] = []
    hour = START_HOUR
    while hour + SLOT_HOURS <= END_HOUR:
        slots.append((f"{hour:02d}:00", f"{hour + SLOT_HOURS:02d}:00"))
        hour += SLOT_HOURS
    return slots


SLOTS: list[tuple[str, str]] = all_slots()


def slot_end_of(start: str) -> str | None:
    """由起始时刻反查结束时刻；不是合法起点返回 None。"""
    for s, e in SLOTS:
        if s == start:
            return e
    return None


def is_valid_start(start: str) -> bool:
    return slot_end_of(start) is not None


def parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def check_date(value: str, now: datetime, window_days: int = BOOKING_WINDOW_DAYS) -> tuple[bool, str]:
    """
    校验日期是否在可约范围内。返回 (是否合法, 原因)。

    三个边界都要单独判，因为它们对应三种完全不同的用户意图：
      - 格式错 → 前端 bug
      - 过去   → 用户选错了日期
      - 太远   → 用户在试探规则边界
    合成一句"日期不合法"会让排查成本高一个数量级。
    """
    d = parse_date(value)
    if d is None:
        return False, "日期格式应为 YYYY-MM-DD"
    today = now.date()
    if d < today:
        return False, "不能预约已经过去的日期"
    if (d - today).days > window_days:
        return False, f"最多只能预约 {window_days} 天内的时段"
    return True, ""


def slot_states(slot_date: str, booked: set[str], device_status: str, now: datetime) -> list[dict]:
    """
    组装某设备某天的 6 格状态，供前端/工具直接展示。

    state 取值：
      free     可约
      taken    已被约（booked 集合来自数据库）
      passed   今天已过去的格子（展示但不可点）
      offline  设备下架，整列都不可约
    """
    d = parse_date(slot_date)
    states: list[dict] = []
    for start, end in SLOTS:
        if device_status == "offline":
            state, reason = "offline", "设备已下架"
        elif d is not None and d == now.date() and _slot_started(start, now):
            state, reason = "passed", "时段已开始"
        elif start in booked:
            state, reason = "taken", "已被预约"
        else:
            state, reason = "free", ""
        states.append({"start": start, "end": end, "state": state, "reason": reason})
    return states


def _slot_started(slot_start: str, now: datetime) -> bool:
    """
    判断"这个格子是否已经开始"。

    用**起始时刻**而不是结束时刻，这是刻意的一刀：
      10:00–12:00 在 10:17 时，如果按结束时刻判断，这格仍是"可约"，
      用户约到的是只剩 1 小时 43 分的半格，而且现场可能正有人在使用
      （上一格 08:00–10:00 刚结束，人还没走）。两种结局都要人工收拾。
    所以规则定为：**时段一开始，就不能再被新预约**。

    代价是 11:50 想约 10:00–12:00 的人会被拒——但这本来就是不该发生的事，
    真要临时用设备，走的是"现场直接取用"，不在系统预约范围内。
    """
    end = slot_end_of(slot_start)          # 仅用于校验起点合法性
    if end is None:
        return True
    hh, mm = (int(x) for x in slot_start.split(":"))
    boundary = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now >= boundary


def next_days(now: datetime, count: int = BOOKING_WINDOW_DAYS) -> list[str]:
    """从今天起连续 count 天的日期串，供日期选择器使用。"""
    return [(now.date() + timedelta(days=i)).isoformat() for i in range(count)]

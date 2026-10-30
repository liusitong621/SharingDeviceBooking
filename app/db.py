"""
SQLite 持久层：会话、消息，以及「同会话互斥」的进程内锁。

三个关键设计（都在接口设计里写过，这里落成代码）：
    1) 时间用 TEXT 存 ISO8601：SQLite 无原生时间类型，ISO8601 可直接按字典序比较，
       迁移到 PostgreSQL 时对应字段原样转 timestamptz。
    2) citations 存 JSON 文本：字段名与 PG 的 jsonb 一致，迁移只换类型。
    3) 写操作全部丢进线程池（asyncio.to_thread）：sqlite3 是同步阻塞的，
       直接在事件循环里执行会卡住所有并发请求——这是 FastAPI + SQLite 最常见的性能事故。

一个必须说清的边界：会话锁是**进程内**的 dict，只在单 worker 下有效。
多 worker / 多实例部署时它形同不存在，届时必须换成数据库行锁或 Redis 锁。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from app.errors import ApiError

# 项目根目录：本文件在 app/ 下，父目录即根
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_DIR = PROJECT_ROOT / "data"
DB_PATH = DB_DIR / "chat.db"

# =============================================================================
# 建表语句
# -----------------------------------------------------------------------------
# 与阶段 1 不同：阶段 1 只有内存里的索引文件，没有库。
# 这里按之前架构文档里的 chat_sessions / chat_messages 落成 SQLite 版本。
# =============================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    id             TEXT PRIMARY KEY,
    client_token   TEXT NOT NULL,              -- 绑定浏览器的 HttpOnly cookie 值
    title          TEXT,                       -- 取首条问题前 20 字
    created_at     TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    message_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'ok' CHECK(status IN ('ok','aborted','error')),
    citations  TEXT,                           -- JSON 数组，仅 assistant 有
    model      TEXT,
    latency_ms INTEGER,
    trace_id   TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_msg_session_created ON chat_messages(session_id, created_at);

-- ===========================================================================
-- 账号（批次 1 新增）
-- ---------------------------------------------------------------------------
-- 为什么必须有 users 表：审计要求"按员工筛选对话"，没有 user_id 这条需求
-- 根本无法实现——会话匿名时，唯一能与人对上的只有 cookie，换设备就断了。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    emp_no          TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    dept            TEXT,
    role            TEXT NOT NULL CHECK(role IN ('employee','admin')),
    status          TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
    pwd_hash        TEXT NOT NULL,
    must_change_pwd INTEGER NOT NULL DEFAULT 1,   -- 首次登录强制改密
    failed_count    INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,
    last_login_at   TEXT,
    created_at      TEXT NOT NULL
);

-- ===========================================================================
-- 设备台账（预约域新增）
-- ---------------------------------------------------------------------------
-- status 是**物理状态**：available 可借 / borrowed 已借出 / offline 下架。
-- 它和 bookings.status（占用状态）是两条线，绝不能合并成一个字段——
-- 合并的典型后果是"设备借出中，但下一格又被约上了"。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS devices (
    id         TEXT PRIMARY KEY,
    asset_no   TEXT NOT NULL UNIQUE,          -- 资产编号，用户看到的就是它
    name       TEXT NOT NULL,
    model      TEXT,
    location   TEXT,
    status     TEXT NOT NULL DEFAULT 'available'
               CHECK(status IN ('available','borrowed','offline')),
    note       TEXT,
    created_at TEXT NOT NULL
);

-- ===========================================================================
-- 预约单（本系统的核心不变量所在）
-- ---------------------------------------------------------------------------
-- 唯一索引 UNIQUE(device_id, slot_date, slot_start, active_flag) 是整套系统
-- 唯一不能妥协的地方：冲突判定只允许"直接 INSERT，捕获 IntegrityError 转 409"，
-- 不允许"先 SELECT 判空再 INSERT"（TOCTOU 竞态，并发下必然出双单）。
--
-- active_flag 为什么可空：
--   有效单置 'a'；取消时置 NULL。SQLite / PG / MySQL 的唯一索引都**不约束
--   含 NULL 的行**，因此"取消过一次的那个格子"仍可被重新预约，
--   而不需要把取消记录搬到归档表去。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS bookings (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES devices(id),
    slot_date   TEXT NOT NULL,                -- YYYY-MM-DD
    slot_start  TEXT NOT NULL,                -- HH:MM（整点或半点）
    slot_end    TEXT NOT NULL,
    user_id     TEXT NOT NULL REFERENCES users(id),
    status      TEXT NOT NULL DEFAULT 'booked'
                CHECK(status IN ('booked','picked','returned','cancelled','expired')),
    active_flag TEXT,
    remark      TEXT,
    picked_at   TEXT,
    returned_at TEXT,
    created_at  TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_booking_slot
    ON bookings(device_id, slot_date, slot_start, active_flag);
CREATE INDEX IF NOT EXISTS idx_booking_user ON bookings(user_id, slot_date);
CREATE INDEX IF NOT EXISTS idx_booking_scan ON bookings(status, slot_end);

-- ===========================================================================
-- 校验凭证（智能体工具层的 T5/T6 靠它，不是靠提示词）
-- ---------------------------------------------------------------------------
-- validate_booking 生成一条记录并返回 validation_id；
-- request_booking 的唯一参数就是这个 id。payload_hash 绑定"校验的到底是什么"，
-- 防止"校验 A、建 B"。5 分钟过期，用后即废（consumed_at）。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS booking_validations (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id),
    device_id    TEXT NOT NULL,
    slot_date    TEXT NOT NULL,
    slot_starts  TEXT NOT NULL,               -- JSON 数组，如 ["10:00","12:00"]
    payload_hash TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    consumed_at  TEXT,
    created_at   TEXT NOT NULL
);

-- ===========================================================================
-- 智能体运行轨迹（阶段 5 分析的数据源）
-- ---------------------------------------------------------------------------
-- 分两张表而不是一张大表，原因是粒度不同：
--   agent_runs  = 一次提问（一个用户问题的完整生命周期）
--   agent_steps = 一次工具调用（一轮里可能有多个 tool_call）
-- 合成一张表会立刻出现"轮数字段冗余但不一致"的问题：第 3 轮有两个工具调用，
-- 轮数到底记 3 还是记 2？分开记，统计时 JOIN 即可。
--
-- status 取值刻意区分 'awaiting_confirm' 与 'answered'：
-- 前者代表"模型想写库但被拦下了"，这在阶段 5 是最重要的一类信号——
-- 它直接反映模型对写操作的冲动程度，以及用户最终有没有点确认。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS agent_runs (
    id          TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    trace_id    TEXT,
    question    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running'
                CHECK(status IN ('running','answered','awaiting_confirm','max_rounds','failed')),
    rounds      INTEGER NOT NULL DEFAULT 0,   -- 实际用掉的决策轮数，用于判断"5 轮够不够"
    total_ms    INTEGER,
    model       TEXT,
    error_code  TEXT,
    created_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_session ON agent_runs(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_run_status ON agent_runs(status, created_at);

CREATE TABLE IF NOT EXISTS agent_steps (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    round_no       INTEGER NOT NULL,          -- 第几轮决策（1..5）
    step_no        INTEGER NOT NULL,          -- 同一轮内的第几个工具调用（1..n）
    tool_name      TEXT NOT NULL,
    args_json      TEXT,                      -- 模型给的原始参数，事后必须能复原
    result_summary TEXT,                      -- 结果摘要，不存全量结果（避免日志表膨胀）
    status         TEXT NOT NULL DEFAULT 'ok'
                   CHECK(status IN ('ok','error','interrupted','skipped')),
    error_code     TEXT,
    elapsed_ms     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_step_run ON agent_steps(run_id, round_no, step_no);
CREATE INDEX IF NOT EXISTS idx_step_tool ON agent_steps(tool_name, created_at);

-- ===========================================================================
-- 待确认动作（human-in-the-loop 的落点）
-- ---------------------------------------------------------------------------
-- 模型的写操作请求不会被执行，而是变成这里的一行。真正的执行由用户点击触发，
-- 走 POST /api/v1/agent/confirm。这张表存在的意义就是把"模型想做什么"
-- 与"系统做了什么"在数据上彻底分开——出事时能证明是谁点的。
--
-- status 用原子 UPDATE 消费（WHERE status='pending'），
-- 保证用户连点两下不会建出两单。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS pending_actions (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    run_id        TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    action        TEXT NOT NULL              -- request_booking / cancel_booking
                  CHECK(action IN ('request_booking','cancel_booking')),
    validation_id TEXT,                      -- request_booking 用：T5 签发的凭证
    booking_id    TEXT,                      -- cancel_booking 用
    summary_json  TEXT NOT NULL,             -- 给前端渲染、也是用户"确认了什么"的证据
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','confirmed','rejected','expired','failed')),
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    decided_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_pending_session ON pending_actions(session_id, status);
"""


def _now() -> str:
    """本地时区 ISO8601（带偏移）。单机内网场景不存 UTC，与 MVP 决策一致。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """
    每次操作新建一个连接，用完**必须关闭**。

    为什么是 contextmanager 而不是直接返回 Connection：
        sqlite3.Connection 的 `__exit__` 只做 commit/rollback，**不关闭连接**。
        所以 `with sqlite3.connect(...) as conn:` 这种写法实际上在泄漏文件描述符——
        短时间看不出来，跑几天后开始报 "too many open files"，且报错点在
        sqlite3 内部，很难定位到是这里。这类资源泄漏只有压测或长跑才暴露。

    事务语义保持不变：`with conn:` 内抛异常会整体回滚，与原来一致。
    """
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    """初始化数据库与表。幂等，可在每次启动时调用。"""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        # WAL：读写不互斥，避免"正在写消息时读历史被阻塞"
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """
    轻量迁移：只做"给已存在的表补列"。

    为什么不用 alembic：本项目是单机 SQLite、开发期演进，
    补一列的迁移用 PRAGMA 检查比引一套迁移框架便宜得多。
    但**列一旦要改类型或拆表，就必须上 alembic**——那时 SQLite 的
    ALTER TABLE 能力不足以支撑，需要重建表 + 拷数据。
    """

    def columns(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    # 阶段 2 的会话表没有 user_id，批次 1 补上。
    # 允许为空：历史会话是匿名产生的，强行回填成某个用户等于伪造审计数据。
    if "user_id" not in columns("chat_sessions"):
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN user_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session_user ON chat_sessions(user_id)")


# =============================================================================
# 会话
# =============================================================================


async def create_session(session_id: str, client_token: str, title: str, user_id: str | None = None) -> None:
    """新建会话。session_id 由前端生成，首条提问时自动建。

    user_id 是批次 1 加的：审计页要靠它把会话归到人。
    """

    def _run() -> None:
        now = _now()
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO chat_sessions "
                "(id, client_token, title, created_at, last_active_at, message_count, user_id) "
                "VALUES (?, ?, ?, ?, ?, 0, ?)",
                (session_id, client_token, title, now, now, user_id),
            )

    await asyncio.to_thread(_run)


async def fetch_session(session_id: str) -> dict[str, Any] | None:
    """取会话元数据；不存在返回 None。"""

    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(_run)


async def delete_session(session_id: str) -> int:
    """删除会话及其消息，返回删掉的消息条数。"""

    def _run() -> int:
        with _connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (session_id,)).fetchone()[0]
            conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
            return int(n)

    return await asyncio.to_thread(_run)


# =============================================================================
# 消息
# =============================================================================


async def append_message(
    session_id: str,
    role: str,
    content: str,
    *,
    status: str = "ok",
    citations: list[dict] | None = None,
    model: str | None = None,
    latency_ms: int | None = None,
    trace_id: str | None = None,
    message_id: str | None = None,
) -> str:
    """写一条消息，并同步会话的 message_count / last_active_at。"""
    msg_id = message_id or str(uuid.uuid4())

    def _run() -> str:
        now = _now()
        with _connect() as conn:
            conn.execute(
                "INSERT INTO chat_messages "
                "(id, session_id, role, content, status, citations, model, latency_ms, trace_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg_id,
                    session_id,
                    role,
                    content,
                    status,
                    json.dumps(citations, ensure_ascii=False) if citations else None,
                    model,
                    latency_ms,
                    trace_id,
                    now,
                ),
            )
            conn.execute(
                "UPDATE chat_sessions SET message_count = message_count + 1, last_active_at = ? WHERE id = ?",
                (now, session_id),
            )
        return msg_id

    return await asyncio.to_thread(_run)


async def recent_messages(session_id: str, limit: int) -> list[dict[str, Any]]:
    """
    取最近 limit 条**成功**消息，按时间正序返回。

    两个细节：
      - 倒序取再反转：不限条数直接正序取，会话变长后每次都要全表扫描；
      - 只要 status='ok'：中断的、报错的历史不能进上下文，否则模型会模仿半截话。
    """

    def _run() -> list[dict[str, Any]]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, role, content, citations, created_at FROM chat_messages "
                "WHERE session_id = ? AND status = 'ok' "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    return await asyncio.to_thread(_run)


async def list_messages(session_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """页面刷新后恢复历史：全部状态都返回，但把 status 一并给前端以便标记。"""

    def _run() -> list[dict[str, Any]]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, role, content, status, citations, model, latency_ms, created_at "
                "FROM chat_messages WHERE session_id = ? "
                "ORDER BY created_at ASC, rowid ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                # 出库就把 JSON 文本解成数组，前端不必再解一次
                d["citations"] = json.loads(d["citations"]) if d.get("citations") else []
                out.append(d)
            return out

    return await asyncio.to_thread(_run)


# =============================================================================
# 同会话互斥锁（进程内，单 worker 有效）
# -----------------------------------------------------------------------------
# 目的：同一会话不允许并发两条提问。否则两条都读同一份历史、都写一条用户消息，
# 上下文会互相污染，用户看到的顺序也会错乱。
# 用非阻塞 acquire：抢不到立刻返回 409，而不是排队——排队会让用户以为卡死。
# =============================================================================

# 用一个 set 表示「占用中」，而不是 asyncio.Lock。
# 理由：我们需要的是同步的非阻塞判定（抢不到立即 409），而 Lock.acquire() 是协程，
# 拿私有属性去伪造状态既脆弱又难读。set 的 add/discard 在单线程事件循环里天然原子。
_busy: set[str] = set()


def try_lock(session_id: str) -> bool:
    """尝试占用会话，成功返回 True；已被占用返回 False。非阻塞。"""
    if session_id in _busy:
        return False
    _busy.add(session_id)
    return True


def unlock(session_id: str) -> None:
    """释放会话占用。必须放在 finally 里调用，否则该会话会永久 409。"""
    _busy.discard(session_id)


# =============================================================================
# 用户
# =============================================================================


async def fetch_user(user_id: str | None) -> dict[str, Any] | None:
    if not user_id:
        return None

    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(_run)


async def fetch_user_by_emp_no(emp_no: str) -> dict[str, Any] | None:
    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE emp_no = ?", (emp_no,)).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(_run)


async def list_users(limit: int = 200) -> list[dict[str, Any]]:
    def _run() -> list[dict[str, Any]]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, emp_no, name, dept, role, status, must_change_pwd, last_login_at "
                "FROM users ORDER BY emp_no LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    return await asyncio.to_thread(_run)


async def upsert_user(
    emp_no: str,
    name: str,
    pwd_hash: str,
    *,
    role: str = "employee",
    dept: str | None = None,
    must_change_pwd: bool = True,
) -> str:
    """
    建号或更新密码（按工号幂等）。seed 与 CSV 导入共用这一个入口。

    已有账号不予覆盖角色——导入 Excel 时最容易出的事故就是
    一次误操作把管理员批量改回员工。
    """
    uid = str(uuid.uuid4())

    def _run() -> str:
        now = _now()
        with _connect() as conn:
            row = conn.execute("SELECT id FROM users WHERE emp_no = ?", (emp_no,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET pwd_hash = ?, status = 'active', must_change_pwd = ?, "
                    "failed_count = 0, locked_until = NULL WHERE id = ?",
                    (pwd_hash, 1 if must_change_pwd else 0, row["id"]),
                )
                return str(row["id"])
            conn.execute(
                "INSERT INTO users (id, emp_no, name, dept, role, status, pwd_hash, "
                "must_change_pwd, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (uid, emp_no, name, dept, role, pwd_hash, 1 if must_change_pwd else 0, now),
            )
            return uid

    return await asyncio.to_thread(_run)


async def record_login_failure(user_id: str, max_attempts: int, lock_minutes: int) -> None:
    """失败计数 +1；达到阈值就上锁。解锁时间写死在行上，不靠定时任务清。"""

    def _run() -> None:
        with _connect() as conn:
            conn.execute("UPDATE users SET failed_count = failed_count + 1 WHERE id = ?", (user_id,))
            row = conn.execute("SELECT failed_count FROM users WHERE id = ?", (user_id,)).fetchone()
            if row and int(row["failed_count"]) >= max_attempts:
                until = datetime.now().astimezone() + timedelta(minutes=lock_minutes)
                conn.execute(
                    "UPDATE users SET locked_until = ?, failed_count = 0 WHERE id = ?",
                    (until.isoformat(timespec="seconds"), user_id),
                )

    await asyncio.to_thread(_run)


async def record_login_success(user_id: str) -> None:
    def _run() -> None:
        now = _now()
        with _connect() as conn:
            conn.execute(
                "UPDATE users SET failed_count = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
                (now, user_id),
            )

    await asyncio.to_thread(_run)


async def update_password(user_id: str, pwd_hash: str, must_change: bool = False) -> None:
    def _run() -> None:
        with _connect() as conn:
            conn.execute(
                "UPDATE users SET pwd_hash = ?, must_change_pwd = ? WHERE id = ?",
                (pwd_hash, 1 if must_change else 0, user_id),
            )

    await asyncio.to_thread(_run)


# =============================================================================
# 设备台账
# =============================================================================


async def list_devices(include_offline: bool = True) -> list[dict[str, Any]]:
    def _run() -> list[dict[str, Any]]:
        sql = "SELECT * FROM devices"
        if not include_offline:
            sql += " WHERE status != 'offline'"
        sql += " ORDER BY asset_no"
        with _connect() as conn:
            return [dict(r) for r in conn.execute(sql).fetchall()]

    return await asyncio.to_thread(_run)


async def fetch_device(device_id: str) -> dict[str, Any] | None:
    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(_run)


async def fetch_device_by_asset_no(asset_no: str) -> dict[str, Any] | None:
    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE asset_no = ?", (asset_no,)).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(_run)


async def set_device_status(device_id: str, status: str) -> None:
    def _run() -> None:
        with _connect() as conn:
            conn.execute("UPDATE devices SET status = ? WHERE id = ?", (status, device_id))

    await asyncio.to_thread(_run)


async def upsert_device(
    device_id: str, asset_no: str, name: str, model: str | None, location: str | None, status: str = "available"
) -> None:
    """按资产编号幂等写入设备。重复导入不会产生重复台账。"""

    def _run() -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO devices (id, asset_no, name, model, location, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(asset_no) DO UPDATE SET name = excluded.name, model = excluded.model, "
                "location = excluded.location",
                (device_id, asset_no, name, model, location, status, _now()),
            )

    await asyncio.to_thread(_run)


# =============================================================================
# 预约
# -----------------------------------------------------------------------------
# 这里只有一条铁律：create_booking 直接 INSERT，让唯一索引去判冲突。
# 任何"先查再插"的写法都是错的，哪怕它在测试里看起来从没出错。
# =============================================================================


async def booked_slots(device_id: str, slot_date: str) -> set[str]:
    """某设备某天已被占用的起始时刻集合。用于展示空闲状态，**不用于判冲突**。"""

    def _run() -> set[str]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT slot_start FROM bookings "
                "WHERE device_id = ? AND slot_date = ? AND active_flag IS NOT NULL "
                "AND status IN ('booked','picked')",
                (device_id, slot_date),
            ).fetchall()
            return {str(r["slot_start"]) for r in rows}

    return await asyncio.to_thread(_run)


async def create_booking(
    device_id: str,
    slot_date: str,
    slot_start: str,
    slot_end: str,
    user_id: str,
    remark: str | None = None,
) -> str:
    """单格落单。委托给批量版本，保证只有一条写库路径。"""
    ids = await create_bookings(device_id, slot_date, [(slot_start, slot_end)], user_id, remark)
    return ids[0]


async def create_bookings(
    device_id: str,
    slot_date: str,
    slots: list[tuple[str, str]],
    user_id: str,
    remark: str | None = None,
) -> list[str]:
    """
    批量落单：**同一个连接、同一个事务**，要么全成，要么全败。

    为什么必须同事务：用户一次约 10:00–10:00 和 12:00–12:00 两格，
    如果第一格成功、第二格被别人抢了，只落一半会让用户无法判断自己到底约到了什么，
    而且这条半成功的记录会一直留在库里污染统计。
    所以第二格冲突时抛 SLOT_TAKEN，前面那条 INSERT 随事务一起回滚。

    冲突判定依然只有一条路径：直接 INSERT，靠唯一索引报错。
    """

    def _run() -> list[str]:
        ids = [str(uuid.uuid4()) for _ in slots]
        now = _now()
        with _connect() as conn:
            for bid, (start, end) in zip(ids, slots):
                try:
                    conn.execute(
                        "INSERT INTO bookings (id, device_id, slot_date, slot_start, slot_end, "
                        "user_id, status, active_flag, remark, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'booked', 'a', ?, ?)",
                        (bid, device_id, slot_date, start, end, user_id, remark, now),
                    )
                except sqlite3.IntegrityError as exc:
                    # 只有唯一索引冲突才是"时段被占用"。**必须区分**，
                    # 因为外键失败（user_id 不存在）、NOT NULL 失败同样抛 IntegrityError，
                    # 全都报成"该时段已被预约"会让排查彻底走错方向——
                    # 明明是一张不存在的用户 id，日志里却显示"时段冲突"。
                    msg = str(exc).lower()
                    if "unique" in msg or "uq_booking_slot" in msg:
                        raise ApiError("SLOT_TAKEN", f"{slot_date} {start} 已被占用") from exc
                    raise ApiError("INTERNAL_ERROR", f"落单失败（非冲突）：{exc}") from exc
            return ids

    return await asyncio.to_thread(_run)


async def count_bookings_on_date(user_id: str, slot_date: str) -> int:
    """单日有效单数（用于 2 格/天上限）。取消和过期的单不计入。"""

    def _run() -> int:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE user_id = ? AND slot_date = ? "
                "AND active_flag IS NOT NULL AND status IN ('booked','picked')",
                (user_id, slot_date),
            ).fetchone()
            return int(row[0])

    return await asyncio.to_thread(_run)


async def fetch_booking(booking_id: str) -> dict[str, Any] | None:
    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(_run)


async def list_user_bookings(user_id: str, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    def _run() -> list[dict[str, Any]]:
        sql = (
            "SELECT b.*, d.asset_no, d.name AS device_name, d.location "
            "FROM bookings b JOIN devices d ON d.id = b.device_id WHERE b.user_id = ?"
        )
        args: list[Any] = [user_id]
        if status:
            sql += " AND b.status = ?"
            args.append(status)
        sql += " ORDER BY b.slot_date DESC, b.slot_start DESC LIMIT ?"
        args.append(limit)
        with _connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    return await asyncio.to_thread(_run)


async def cancel_booking(booking_id: str) -> bool:
    """
    取消：status 改 cancelled 且 active_flag 置 NULL。

    active_flag 置 NULL 是关键——唯一索引不约束含 NULL 的行，
    这个格子因此立刻可被别人预约，无需搬数据到归档表。
    """
    def _run() -> bool:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE bookings SET status = 'cancelled', active_flag = NULL "
                "WHERE id = ? AND status = 'booked' AND active_flag IS NOT NULL",
                (booking_id,),
            )
            return cur.rowcount > 0

    return await asyncio.to_thread(_run)


async def mark_picked(booking_id: str) -> bool:
    def _run() -> bool:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE bookings SET status = 'picked', picked_at = ? WHERE id = ? AND status = 'booked'",
                (_now(), booking_id),
            )
            return cur.rowcount > 0

    return await asyncio.to_thread(_run)


async def mark_returned(booking_id: str) -> bool:
    def _run() -> bool:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE bookings SET status = 'returned', returned_at = ? "
                "WHERE id = ? AND status IN ('booked','picked')",
                (_now(), booking_id),
            )
            return cur.rowcount > 0

    return await asyncio.to_thread(_run)


async def expire_bookings(now: str) -> int:
    """
    把已过时段仍未取走的单标记为 expired，并释放格子。

    必须写成一条幂等的 UPDATE：重复执行结果一致。
    在 Python 里循环"取出→判断→更新"会在多进程下重复处理，
    也会在两次扫描之间产生竞态。
    """
    def _run() -> int:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE bookings SET status = 'expired', active_flag = NULL "
                "WHERE active_flag IS NOT NULL AND status = 'booked' "
                "AND (slot_date || 'T' || slot_end) < ?",
                (now,),
            )
            return int(cur.rowcount)

    return await asyncio.to_thread(_run)


# =============================================================================
# 校验凭证（智能体工具 T5 → T6）
# =============================================================================


async def create_validation(
    user_id: str, device_id: str, slot_date: str, slot_starts: list[str], payload_hash: str, expires_at: str
) -> str:
    vid = str(uuid.uuid4())

    def _run() -> str:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO booking_validations (id, user_id, device_id, slot_date, slot_starts, "
                "payload_hash, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (vid, user_id, device_id, slot_date, json.dumps(slot_starts), payload_hash, expires_at, _now()),
            )
            return vid

    return await asyncio.to_thread(_run)


async def consume_validation(vid: str, user_id: str) -> dict[str, Any] | None:
    """
    原子地消费凭证：一次 UPDATE 同时完成"校验未用 + 标记已用"。

    如果先 SELECT 再 UPDATE，两个并发请求会拿到同一张凭证，
    结果就是同一凭证建了两单——而幂等本来是这张表存在的全部理由。
    """
    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE booking_validations SET consumed_at = ? "
                "WHERE id = ? AND user_id = ? AND consumed_at IS NULL",
                (_now(), vid, user_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM booking_validations WHERE id = ?", (vid,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            # 必须在这里把 JSON 文本解成列表。
            # 漏这一步的话，消费方拿到的是 '["10:00"]' 这个**字符串**，
            # 传给 place_booking 时会按字符遍历，报出莫名其妙的 INVALID_SLOT。
            # fetch_validation 已经解了，两条路径的返回结构必须一致。
            d["slot_starts"] = json.loads(d["slot_starts"]) if d.get("slot_starts") else []
            return d

    return await asyncio.to_thread(_run)


# =============================================================================
# 智能体运行轨迹
# -----------------------------------------------------------------------------
# 写入策略：每轮决策**立即写**一条 step，不等整个 run 结束。
# 理由：如果等结束时批量写，一旦进程崩了或用户关掉页面，轨迹就全丢了——
# 而"出问题的那些请求"恰恰是最需要轨迹的。
# =============================================================================


async def start_run(
    session_id: str, user_id: str, question: str, trace_id: str | None, model: str | None
) -> str:
    """开一次运行。返回 run_id，后续所有 step 都挂在它下面。"""
    run_id = str(uuid.uuid4())

    def _run() -> str:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO agent_runs (id, session_id, user_id, trace_id, question, model, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, session_id, user_id, trace_id, question[:500], model, _now()),
            )
            return run_id

    return await asyncio.to_thread(_run)


async def finish_run(
    run_id: str, status: str, rounds: int, total_ms: int, error_code: str | None = None
) -> None:
    """
    结束一次运行。

    这里用 UPDATE 而不是 INSERT ... ON CONFLICT：run 一定先被 start_run 创建过，
    用 UPDATE 能在 run_id 写错时直接暴露成"改了 0 行"，而不是悄悄插出一条孤儿记录。
    """
    def _run() -> None:
        with _connect() as conn:
            conn.execute(
                "UPDATE agent_runs SET status = ?, rounds = ?, total_ms = ?, error_code = ?, "
                "finished_at = ? WHERE id = ?",
                (status, rounds, total_ms, error_code, _now(), run_id),
            )

    await asyncio.to_thread(_run)


async def add_step(
    run_id: str,
    round_no: int,
    step_no: int,
    tool_name: str,
    args: dict[str, Any] | None,
    result_summary: str | None,
    *,
    status: str = "ok",
    error_code: str | None = None,
    elapsed_ms: int = 0,
) -> None:
    """
    记一轮决策。

    args_json 存**原始**参数而不是清洗后的：事后复盘时，模型到底传了什么
    （包括那些被校验拒掉的非法值）才是最有价值的信息。
    """
    def _run() -> None:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO agent_steps (id, run_id, round_no, step_no, tool_name, args_json, "
                "result_summary, status, error_code, elapsed_ms, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    run_id,
                    round_no,
                    step_no,
                    tool_name,
                    json.dumps(args, ensure_ascii=False) if args is not None else None,
                    (result_summary or "")[:1000],
                    status,
                    error_code,
                    elapsed_ms,
                    _now(),
                ),
            )

    await asyncio.to_thread(_run)


async def list_steps(run_id: str) -> list[dict[str, Any]]:
    """按时间正序取一次运行的全部轨迹。阶段 5 的分析接口会用到。"""
    def _run() -> list[dict[str, Any]]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT round_no, step_no, tool_name, args_json, result_summary, status, "
                "error_code, elapsed_ms, created_at FROM agent_steps "
                "WHERE run_id = ? ORDER BY round_no ASC, step_no ASC",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    return await asyncio.to_thread(_run)


# =============================================================================
# 待确认动作
# =============================================================================


async def create_pending(
    *,
    session_id: str,
    run_id: str,
    user_id: str,
    action: str,
    summary: dict[str, Any],
    validation_id: str | None = None,
    booking_id: str | None = None,
    ttl_seconds: int = 600,
) -> str:
    """
    落一条待确认动作，返回 pending_id。

    TTL 600 秒（10 分钟）比校验凭证的 5 分钟长：凭证过期后确认会失败，
    但那时用户至少能看到"这次确认已过期，请重新发起"，而不是页面上一张
    永远点不动的卡片。两者的时序关系在 confirm 里显式处理。
    """
    pid = str(uuid.uuid4())

    def _run() -> str:
        now = datetime.now().astimezone()
        with _connect() as conn:
            conn.execute(
                "INSERT INTO pending_actions (id, session_id, run_id, user_id, action, validation_id, "
                "booking_id, summary_json, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    session_id,
                    run_id,
                    user_id,
                    action,
                    validation_id,
                    booking_id,
                    json.dumps(summary, ensure_ascii=False),
                    now.isoformat(timespec="seconds"),
                    (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds"),
                ),
            )
            return pid

    return await asyncio.to_thread(_run)


async def fetch_pending(pid: str) -> dict[str, Any] | None:
    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM pending_actions WHERE id = ?", (pid,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["summary"] = json.loads(d["summary_json"]) if d.get("summary_json") else {}
            return d

    return await asyncio.to_thread(_run)


async def decide_pending(pid: str, user_id: str, status: str) -> bool:
    """
    原子地把 pending 从 pending 改成 confirmed/rejected。

    返回 False 代表"这一条已经被处理过、不属于你、或已过期"。
    这一步是幂等闸门：用户连点两下、或前端重试，第二次必然拿到 False，
    因此**不会建出两单**。校验归属也放在这条 SQL 里，
    避免"先查再改"中间被换人。
    """
    def _run() -> bool:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE pending_actions SET status = ?, decided_at = ? "
                "WHERE id = ? AND user_id = ? AND status = 'pending' AND expires_at > ?",
                (status, _now(), pid, user_id, _now()),
            )
            return cur.rowcount > 0

    return await asyncio.to_thread(_run)


async def expire_pending(now: str | None = None) -> int:
    """把超时的 pending 标记为 expired。与预约过期扫描一样，必须是幂等 UPDATE。"""
    def _run() -> int:
        with _connect() as conn:
            cur = conn.execute(
                "UPDATE pending_actions SET status = 'expired', decided_at = ? "
                "WHERE status = 'pending' AND expires_at <= ?",
                (now or _now(), now or _now()),
            )
            return cur.rowcount

    return await asyncio.to_thread(_run)


async def mark_pending_failed(pid: str) -> None:
    """
    确认已点击、但执行失败（典型：点下去的那一秒被别人抢了格）。

    单独一个终态而不是回滚成 pending：回滚会让用户看到卡片又能点了，
    但他手里的凭证已被消费，再点必然失败，体验更差。
    标成 failed 并如实告知"这次没约上，请重新发起"，才是诚实的做法。
    """
    def _run() -> None:
        with _connect() as conn:
            conn.execute(
                "UPDATE pending_actions SET status = 'failed' WHERE id = ?", (pid,)
            )

    await asyncio.to_thread(_run)


async def fetch_validation(vid: str) -> dict[str, Any] | None:
    """查凭证。harness 在拦截 request_booking 时用它判断"这个凭证是不是真的"。"""
    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM booking_validations WHERE id = ?", (vid,)).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["slot_starts"] = json.loads(d["slot_starts"]) if d.get("slot_starts") else []
            return d

    return await asyncio.to_thread(_run)


# =============================================================================
# 概览统计（后台首页用）
# =============================================================================


async def stats(today: str) -> dict[str, int]:
    def _run() -> dict[str, int]:
        with _connect() as conn:
            row = conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM devices) AS devices, "
                "(SELECT COUNT(*) FROM devices WHERE status = 'offline') AS devices_offline, "
                "(SELECT COUNT(*) FROM bookings WHERE slot_date = ? AND active_flag IS NOT NULL) AS bookings_today, "
                "(SELECT COUNT(*) FROM bookings WHERE active_flag IS NOT NULL) AS bookings_active, "
                "(SELECT COUNT(*) FROM users) AS users, "
                "(SELECT COUNT(*) FROM users WHERE status = 'disabled') AS users_disabled, "
                "(SELECT COUNT(*) FROM chat_sessions) AS sessions, "
                "(SELECT COUNT(*) FROM chat_messages) AS messages",
                (today,),
            ).fetchone()
            return {k: int(row[k]) for k in row.keys()}

    return await asyncio.to_thread(_run)

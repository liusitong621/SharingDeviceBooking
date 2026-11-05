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
from datetime import date, datetime, timedelta
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
    created_at TEXT NOT NULL,
    deleted_at TEXT                           -- 软删除标记，见下方 delete_device 的说明
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
    -- 本轮实际使用的提示词版本；NULL 表示用的是内置模板。
    -- 没有这一列，提示词管理页就只是一个编辑器：改了之后回答变好还是变坏，
    -- 永远无法和具体某一轮对话对上。
    prompt_version INTEGER,
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

-- ===========================================================================
-- 操作留痕（R07）
-- ---------------------------------------------------------------------------
-- 这张表与 agent_steps 的分工必须说清楚，否则一定会有人问"为什么不合并"：
--   agent_steps = 模型做了什么（决策轨迹，供调优）
--   op_logs     = 人对系统做了什么（业务留痕，供追责）
-- 前者可以丢，后者不能丢——出了"谁把张伟的预约取消了"这种争议时，
-- 唯一能回答的只有这一张表。
--
-- 三个刻意的字段设计：
--   1) actor_label 冗余存工号，不只存 actor_id。用户被停用、改名、甚至删除后，
--      日志仍要能读出"当时是谁"。外键在这里是负担而不是保障。
--   2) before_json/after_json 存**变更前后整行快照**，不存"改了哪个字段"。
--      因为字段列表会随版本演进，而快照永远能还原当时的事实。
--   3) 没有任何 UPDATE/DELETE 入口。代码里只提供写入与查询，
--      "日志不可被普通管理员删除"这条要求靠"没有这个函数"来保证，
--      比靠权限判断更可靠——权限判断会写错，不存在的函数不会。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS op_logs (
    id          TEXT PRIMARY KEY,
    actor_id    TEXT,                          -- 操作人 id；系统任务为 NULL
    actor_label TEXT NOT NULL,                 -- 冗余快照：工号，或 system:xxx
    action      TEXT NOT NULL,                 -- booking.create / device.status ...
    target_type TEXT NOT NULL,                 -- booking / device / user
    target_id   TEXT NOT NULL,
    before_json TEXT,
    after_json  TEXT,
    ip          TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oplog_target ON op_logs(target_type, target_id, created_at);
CREATE INDEX IF NOT EXISTS idx_oplog_actor ON op_logs(actor_id, created_at);
CREATE INDEX IF NOT EXISTS idx_oplog_time ON op_logs(created_at);

-- ===========================================================================
-- 提示词模板（批次 3）
-- ---------------------------------------------------------------------------
-- 三条设计决定，每条都是被现实逼出来的：
--
--  1) **追加版本，不原地改。** 保存一次就插一行新版本，旧行转 archived。
--     如果原地 UPDATE，就永远回答不了"上周回答变差是不是因为改了提示词"——
--     而那恰好是唯一值得追问的问题。代价只是表变长，一行几 KB。
--
--  2) **每个 key 至多一个 active，交给数据库保证。**
--     部分唯一索引 `WHERE status='active'` 与 bookings 的 active_flag 同一手法：
--     应用层"先查再改"在多进程下会写出两个启用版本，而数据库不会。
--
--  3) **内容不入 op_logs。** 日志记"谁在什么时候发布了 v7、说明是什么"，
--     全文留给本表按版本查。否则日志页会变成一面文字墙，
--     而审计真正要看的是变更点，不是两千字正文。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS prompt_templates (
    id         TEXT PRIMARY KEY,
    tpl_key    TEXT NOT NULL,                  -- agent_system / （后续可扩）
    version    INTEGER NOT NULL,
    content    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'archived'
               CHECK(status IN ('active','archived')),
    note       TEXT,                           -- 本次修改说明，便于回看
    created_by TEXT,                           -- 操作人 id；NULL 表示系统初始化
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_version ON prompt_templates(tpl_key, version);
CREATE UNIQUE INDEX IF NOT EXISTS uq_prompt_active  ON prompt_templates(tpl_key) WHERE status = 'active';

-- ===========================================================================
-- 审计标记（批次 4）
-- ---------------------------------------------------------------------------
-- 管理员看对话审计时，遇到"这句话不对劲、要跟进"的，需要留个记号。
--
-- 为什么单独一张表而不是给 agent_runs 加三列：
--   agent_runs 是**机器产生的事实**，写进去就不该再改；标记是**人的判断**，
--   会改、会撤、会换理由。混在一张表里，以后就没法回答"这行到底哪些是
--   当时的原始数据"。分开存，轨迹永远干净。
--
-- 为什么主键是 run_id 而不是自增 id：
--   一次运行只该有一个当前标记。要不要保留历史标记，交给 op_logs——
--   那里本来就记着"谁在什么时候标了什么、什么时候撤了"。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS audit_flags (
    run_id     TEXT PRIMARY KEY REFERENCES agent_runs(id) ON DELETE CASCADE,
    reason     TEXT NOT NULL,
    flagged_by TEXT,
    flagged_at TEXT NOT NULL
);

-- ===========================================================================
-- 制度文档登记（批次 5）
-- ---------------------------------------------------------------------------
-- 语料的**实体**一直是磁盘上的 markdown 文件，这张表只登记它们的元数据与
-- 启停状态，不存正文。为什么不做成"上传即入库"：
--   制度文件是要归档、要盖章、要交接的，文件本身就该是唯一真相。
--   把它切成 500 字片段存进库，等于把"制度原文"降级成"检索片段"，
--   日后要证明"当时那句原文是什么"就得从碎片里拼。
--
-- 为什么停用是软操作而不是删文件：
--   旧制度作废时，正确动作是"不再被检索到"，不是"销毁"。停用只改一行状态，
--   一年后要追溯"当时为什么这么答"，原件还在，切片规则还能复现。
--
-- on_disk 与 status 拆开：
--   前者是**事实**（文件在不在磁盘上，由每次扫描刷新），后者是**人的决定**。
--   合成一列的话，一次误删就会把"管理员主动停用"覆盖成"文件丢失"，
--   两种完全不同的处置理由被抹平。
-- ===========================================================================
CREATE TABLE IF NOT EXISTS kb_documents (
    doc_key      TEXT PRIMARY KEY,             -- 相对路径：制度.md / uploads/xxx.md
    display_name TEXT NOT NULL,                -- 页面展示用（上传的保留原始文件名）
    origin       TEXT NOT NULL CHECK(origin IN ('builtin','uploaded')),
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK(status IN ('active','disabled')),
    sha256       TEXT,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    chunks       INTEGER NOT NULL DEFAULT 0,   -- 上一次重建时的切片数
    file_mtime   TEXT,
    on_disk      INTEGER NOT NULL DEFAULT 1,
    note         TEXT,
    synced_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kb_status ON kb_documents(status);
"""


def _now() -> str:
    """本地时区 ISO8601（带偏移）。单机内网场景不存 UTC，与 MVP 决策一致。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


# =============================================================================
# 操作留痕（R07）
# -----------------------------------------------------------------------------
# 为什么 `_log` 收的是 conn 而不是自己开连接：
#   审计的可信度完全取决于"业务变更与日志在**同一个事务**里"。
#   如果先改状态再另开连接写日志，两个事务之间进程崩了，库里就会出现
#   "预约没了、日志也没有"的黑洞——而这种黑洞恰恰出现在最需要日志的时候。
#   传 conn 进来，日志与变更要么一起提交，要么一起回滚。
#
# 为什么日志失败不吞异常：
#   这是少数几个**必须**让业务失败的地方。日志写不进去，就等于这个操作
#   将来无法被解释，宁可让用户重试，也不能留下一条无据可查的变更。
# =============================================================================


def _log(
    conn: sqlite3.Connection,
    *,
    actor: dict[str, Any] | None,
    action: str,
    target_type: str,
    target_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    actor_label: str | None = None,
) -> None:
    """
    在**调用方的事务内**写一条操作日志。

    actor 为 None 表示系统动作（过期扫描、批量迁移）。
    这类记录不写成"无人操作"，而是明确标 system:xxx——
    把系统行为伪装成"没人做的事"，是审计里最糟糕的一种含糊。
    """
    if actor is not None:
        label = str(actor.get("emp_no") or actor.get("id") or "unknown")
        actor_id = actor.get("id")
    else:
        label = actor_label or "system"
        actor_id = None

    conn.execute(
        "INSERT INTO op_logs (id, actor_id, actor_label, action, target_type, target_id, "
        "before_json, after_json, ip, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(uuid.uuid4()),
            actor_id,
            label,
            action,
            target_type,
            target_id,
            json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
            json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
            ip,
            _now(),
        ),
    )


async def write_op_log(
    *,
    actor: dict[str, Any] | None,
    action: str,
    target_type: str,
    target_id: str,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    actor_label: str | None = None,
) -> None:
    """
    独立事务版日志，给"变更本身不在 db 层"的场景用（例如登录后补记一条）。

    注意：能用 `_log` 塞进业务事务的，就不要用这个——
    两条独立事务之间不存在原子性。
    """

    def _run() -> None:
        with _connect() as conn:
            _log(
                conn, actor=actor, action=action, target_type=target_type,
                target_id=target_id, before=before, after=after, ip=ip,
                actor_label=actor_label,
            )

    await asyncio.to_thread(_run)


async def list_op_logs(
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    actor_id: str | None = None,
    action: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """
    按条件倒序查日志。limit 默认 200，与后台一页的量级一致。

    刻意**不提供 offset 分页**：日志量在 MVP 规模（日预约 ≤200 单）下
    一年也就几万条，需要翻页时说明查询条件没写对，应该加筛选而不是往下翻。
    """

    def _run() -> list[dict[str, Any]]:
        sql = "SELECT * FROM op_logs WHERE 1 = 1"
        args: list[Any] = []
        if target_type:
            sql += " AND target_type = ?"
            args.append(target_type)
        if target_id:
            sql += " AND target_id = ?"
            args.append(target_id)
        if actor_id:
            sql += " AND actor_id = ?"
            args.append(actor_id)
        if action:
            sql += " AND action = ?"
            args.append(action)
        sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(limit)
        with _connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    return await asyncio.to_thread(_run)


async def booking_timeline(booking_id: str) -> list[dict[str, Any]]:
    """
    一张预约的完整轨迹（R07 验收口径：「任一预约可还原完整状态轨迹」）。

    直接复用 list_op_logs，不另写 SQL——轨迹与日志是同一种事实，
    分开查会让两个入口在过滤条件上慢慢分叉。
    """
    return await list_op_logs(target_type="booking", target_id=booking_id, limit=100)


# =============================================================================
# 提示词模板（批次 3）
# -----------------------------------------------------------------------------
# 读路径刻意**每次查库**，不做进程内缓存。
#
# 理由：一次 SQLite 单行查询是微秒级，而一次对话要调一次大模型（秒级），
# 差三个数量级。相比之下，缓存的代价是"改完不生效，要等缓存过期或重启"——
# 那正是这一批要消灭的问题。用性能换正确性，在一次 LLM 调用面前是笔稳赚的买卖。
#
# 但如果哪天接入 Redis 或多进程部署，这里必须加带版本号的失效机制，
# 不能简单把缓存加上去。
# =============================================================================


async def active_prompt(tpl_key: str) -> dict[str, Any] | None:
    """取当前启用版本。没有则返回 None，由调用方回落到内置模板。"""

    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM prompt_templates WHERE tpl_key = ? AND status = 'active'",
                (tpl_key,),
            ).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(_run)


async def fetch_prompt_version(tpl_key: str, version: int) -> dict[str, Any] | None:
    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM prompt_templates WHERE tpl_key = ? AND version = ?",
                (tpl_key, version),
            ).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(_run)


async def list_prompt_versions(tpl_key: str, limit: int = 30) -> list[dict[str, Any]]:
    """
    版本历史，新的在前。

    只给 limit 不给 offset：模板半年改十次已经算频繁，翻不到第二页。
    真要翻，说明该按 key 拆开了，而不是加页码。
    """

    def _run() -> list[dict[str, Any]]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM prompt_templates WHERE tpl_key = ? "
                "ORDER BY version DESC LIMIT ?",
                (tpl_key, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    return await asyncio.to_thread(_run)


async def publish_prompt(
    *,
    tpl_key: str,
    content: str,
    note: str | None,
    actor: dict[str, Any] | None,
    ip: str | None = None,
) -> dict[str, Any]:
    """
    发布一个新版本并置为启用。

    **整个动作在一个事务里**：算版本号 → 归档旧版 → 插新版 → 写日志。
    四条语句分开提交的话，进程在两行之间挂掉，就会出现"没有启用版本"
    （对话直接掉回内置模板，而管理员以为自己刚保存成功）。

    并发两个管理员同时点保存：`uq_prompt_version` 唯一索引会让后到的那个
    冲突报错，而不是两条都写成 v8。VersionConflict 由调用方转成可读提示。
    """

    def _run() -> dict[str, Any]:
        now = _now()
        pid = str(uuid.uuid4())
        with _connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS v FROM prompt_templates WHERE tpl_key = ?",
                (tpl_key,),
            ).fetchone()
            new_version = int(row["v"]) + 1

            before = conn.execute(
                "SELECT version FROM prompt_templates WHERE tpl_key = ? AND status = 'active'",
                (tpl_key,),
            ).fetchone()

            conn.execute(
                "UPDATE prompt_templates SET status = 'archived' "
                "WHERE tpl_key = ? AND status = 'active'",
                (tpl_key,),
            )
            conn.execute(
                "INSERT INTO prompt_templates (id, tpl_key, version, content, status, note, "
                "created_by, created_at) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
                (pid, tpl_key, new_version, content, note, actor.get("id") if actor else None, now),
            )
            _log(
                conn,
                actor=actor,
                action="prompt.publish",
                target_type="prompt",
                target_id=tpl_key,
                before={"version": int(before["version"])} if before else None,
                after={"version": new_version, "chars": len(content), "note": note or ""},
                ip=ip,
            )
            return {
                "id": pid,
                "tpl_key": tpl_key,
                "version": new_version,
                "chars": len(content),
                "created_at": now,
            }

    try:
        return await asyncio.to_thread(_run)
    except sqlite3.IntegrityError as exc:
        # 唯一索引冲突 = 另一个管理员刚刚发布了同一版本号。
        # 这不是"服务器错误"，而是"你看到的内容已经旧了"，提示必须这么说。
        raise ApiError(
            "PROMPT_VERSION_CONFLICT",
            "已有别人同时修改了这份模板，请刷新页面后重新编辑",
            message="已有别人同时修改了这份模板，请刷新后重试",
        ) from exc


async def rollback_prompt(
    *,
    tpl_key: str,
    version: int,
    actor: dict[str, Any] | None,
    ip: str | None = None,
) -> dict[str, Any]:
    """
    回滚到某个历史版本。

    实现方式是**把旧内容复制成新版本**，而不是把旧行改回 active。
    区别在于可解释性：复制之后，版本号只增不减，"v9 是回滚 v3 得到的"
    这句话永远成立；而把 v3 直接激活，会让"当前是 v3"和"最新是 v9"
    同时为真，谁也说不清中间那六次修改还算不算数。
    """
    old = await fetch_prompt_version(tpl_key, version)
    if old is None:
        raise ApiError("PROMPT_VERSION_NOT_FOUND", f"没有 v{version} 这个版本", message="该版本不存在")

    return await publish_prompt(
        tpl_key=tpl_key,
        content=old["content"],
        note=f"回滚自 v{version}（原文：{old.get('note') or '无说明'}）",
        actor=actor,
        ip=ip,
    )



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

    # 批次 2：设备改软删除。老库补列，新库由 SCHEMA 直接建出来。
    if "deleted_at" not in columns("devices"):
        conn.execute("ALTER TABLE devices ADD COLUMN deleted_at TEXT")

    # 批次 3：记录本轮用的提示词版本。
    # 老数据留 NULL（= 内置模板），符合事实——那些轮次确实没有入库模板。
    if "prompt_version" not in columns("agent_runs"):
        conn.execute("ALTER TABLE agent_runs ADD COLUMN prompt_version INTEGER")


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
                # trace_id 必须带出来：审计详情页靠它认出「这一轮的问题与回答」。
                # 之前漏选这一列，页面上永远是「本次没有产生回答」——
                # 界面不报错，只是悄悄少了一句，这种缺列比崩溃更难发现。
                "SELECT id, role, content, status, citations, model, latency_ms, trace_id, created_at "
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


async def create_user_if_absent(
    *,
    emp_no: str,
    name: str,
    dept: str | None,
    pwd_hash: str,
    role: str = "employee",
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> tuple[str, bool]:
    """
    批量导入用：工号已存在则**原样跳过**，返回 (user_id, False)。

    为什么不复用 upsert_user：那个函数会覆盖已有账号的密码。
    导入名单是每周都可能做一次的例行操作，而一份名单里出现老工号极其常见
    （HR 导的是全量，不是增量）。用 upsert 的后果是：某位同事周一上班，
    密码被重置成了名单里的初始密码，且必须改密——而且没有任何人被告知。

    这个函数的存在就是为了让"重复导入"成为安全动作。
    """
    uid = str(uuid.uuid4())

    def _run() -> tuple[str, bool]:
        with _connect() as conn:
            row = conn.execute("SELECT id FROM users WHERE emp_no = ?", (emp_no,)).fetchone()
            if row:
                return str(row["id"]), False
            conn.execute(
                "INSERT INTO users (id, emp_no, name, dept, role, status, pwd_hash, "
                "must_change_pwd, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?, 1, ?)",
                (uid, emp_no, name, dept, role, pwd_hash, _now()),
            )
            _log(
                conn,
                actor=actor,
                actor_label="system:user",
                action="user.create",
                target_type="user",
                target_id=uid,
                before=None,
                after={"emp_no": emp_no, "name": name, "dept": dept, "role": role, "status": "active"},
                ip=ip,
            )
            return uid, True

    return await asyncio.to_thread(_run)


async def set_user_status(
    user_id: str,
    status: str,
    *,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """
    停用 / 启用（R08：停用即时生效）。

    "即时生效"由 auth.user_from_request 保证——它每次请求都回库查 status，
    不信 token 里的任何字段。所以这里只需要改库，不需要额外踢人下线。
    """
    def _run() -> None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise ApiError("USER_NOT_FOUND")
            before = dict(row)
            conn.execute("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
            after = dict(before)
            after["status"] = status
            _log(
                conn,
                actor=actor,
                actor_label="system:user",
                action=f"user.{'disable' if status == 'disabled' else 'enable'}",
                target_type="user",
                target_id=user_id,
                before=_strip_secret(before),
                after=_strip_secret(after),
                ip=ip,
            )

    await asyncio.to_thread(_run)


async def reset_user_password(
    user_id: str,
    pwd_hash: str,
    *,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """
    管理员重置密码：置为初始密码并强制下次登录改密。

    重置后 must_change_pwd=1 不是可选项。管理员知道初始密码，
    如果不强制改，这个密码就成了一条永久可用的后门。
    """
    def _run() -> None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise ApiError("USER_NOT_FOUND")
            conn.execute(
                "UPDATE users SET pwd_hash = ?, must_change_pwd = 1, failed_count = 0, "
                "locked_until = NULL WHERE id = ?",
                (pwd_hash, user_id),
            )
            _log(
                conn,
                actor=actor,
                actor_label="system:user",
                action="user.reset_password",
                target_type="user",
                target_id=user_id,
                before={"emp_no": row["emp_no"]},
                after={"emp_no": row["emp_no"], "must_change_pwd": 1, "locked_until": None},
                ip=ip,
            )

    await asyncio.to_thread(_run)


async def count_active_bookings_of_user(user_id: str) -> int:
    """
    某人名下未完成的预约数。停用账号时用来提示管理员——
    按待答项 3 的默认（不自动释放），这些格子会继续被占着，
    管理员得知道有几条需要自己处置。
    """

    def _run() -> int:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE user_id = ? AND active_flag IS NOT NULL "
                "AND status IN ('booked','picked')",
                (user_id,),
            ).fetchone()
            return int(row[0])

    return await asyncio.to_thread(_run)


def _strip_secret(user: dict[str, Any]) -> dict[str, Any]:
    """
    写日志前摘掉密码哈希。

    op_logs 是给管理员看的，管理员不该看到别人的密码哈希——
    即使它是 scrypt 的，也不该出现在一个能导出、能截图、能被审计查看的地方。
    """
    return {k: v for k, v in user.items() if k != "pwd_hash"}


# =============================================================================
# 设备台账
# =============================================================================


async def list_devices(include_offline: bool = True, include_deleted: bool = False) -> list[dict[str, Any]]:
    """
    设备列表。默认**不含已删除**——软删除的设备对业务不可见，
    但对审计可见（查日志时仍能读出设备名），这是软删除相对硬删除的唯一代价，
    也是它换来的好处：历史预约不会变成孤儿。
    """
    def _run() -> list[dict[str, Any]]:
        sql = "SELECT * FROM devices WHERE 1 = 1"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        if not include_offline:
            sql += " AND status != 'offline'"
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


async def count_active_bookings(device_id: str) -> int:
    """该设备名下"未完成"的预约数（booked / picked）。删除前的唯一闸门。"""

    def _run() -> int:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE device_id = ? AND active_flag IS NOT NULL "
                "AND status IN ('booked','picked')",
                (device_id,),
            ).fetchone()
            return int(row[0])

    return await asyncio.to_thread(_run)


async def set_device_status(
    device_id: str,
    status: str,
    *,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    def _run() -> None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
            before = dict(row) if row else None
            conn.execute("UPDATE devices SET status = ? WHERE id = ?", (status, device_id))
            after = dict(before or {})
            after["status"] = status
            _log(
                conn,
                actor=actor,
                actor_label="system:device",
                action="device.status",
                target_type="device",
                target_id=device_id,
                before=before,
                after=after,
                ip=ip,
            )

    await asyncio.to_thread(_run)


async def create_device(
    asset_no: str,
    name: str,
    model: str | None = None,
    location: str | None = None,
    note: str | None = None,
    *,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> str:
    """
    新增设备（R01）。

    资产编号冲突在这里拦而不是靠调用方预查：预查与插入之间的窗口期内，
    另一个管理员同样能插进去，最后靠 UNIQUE 兜底，不如一开始就只信 UNIQUE。
    """
    device_id = str(uuid.uuid4())

    def _run() -> str:
        with _connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO devices (id, asset_no, name, model, location, status, note, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 'available', ?, ?)",
                    (device_id, asset_no, name, model, location, note, _now()),
                )
            except sqlite3.IntegrityError as exc:
                if "unique" in str(exc).lower():
                    raise ApiError("DEVICE_EXISTS", f"资产编号 {asset_no} 已存在") from exc
                raise
            _log(
                conn,
                actor=actor,
                actor_label="system:device",
                action="device.create",
                target_type="device",
                target_id=device_id,
                before=None,
                after={
                    "asset_no": asset_no, "name": name, "model": model,
                    "location": location, "status": "available", "note": note,
                },
                ip=ip,
            )
            return device_id

    return await asyncio.to_thread(_run)


async def update_device(
    device_id: str,
    *,
    asset_no: str,
    name: str,
    model: str | None,
    location: str | None,
    status: str,
    note: str | None,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """编辑设备基础信息。整行覆盖式更新，before/after 快照因此可直接对比。"""

    def _run() -> None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
            if row is None:
                raise ApiError("DEVICE_NOT_FOUND")
            before = dict(row)
            try:
                conn.execute(
                    "UPDATE devices SET asset_no = ?, name = ?, model = ?, location = ?, "
                    "status = ?, note = ? WHERE id = ?",
                    (asset_no, name, model, location, status, note, device_id),
                )
            except sqlite3.IntegrityError as exc:
                if "unique" in str(exc).lower():
                    raise ApiError("DEVICE_EXISTS", f"资产编号 {asset_no} 已被其它设备占用") from exc
                raise
            after = dict(before)
            after.update({
                "asset_no": asset_no, "name": name, "model": model,
                "location": location, "status": status, "note": note,
            })
            _log(
                conn,
                actor=actor,
                actor_label="system:device",
                action="device.update",
                target_type="device",
                target_id=device_id,
                before=before,
                after=after,
                ip=ip,
            )

    await asyncio.to_thread(_run)


async def delete_device(
    device_id: str,
    *,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    """
    删除设备（R01）：无未完成预约才允许，**且是软删除**。

    为什么不物理删除——两个理由，第二个是决定性的：
      1) bookings.device_id 有外键指向 devices。历史预约永远存在，
         物理删除必然违反外键（报错），或者被迫级联删掉历史（毁掉留痕）。
      2) R07 要求"任一预约可还原完整状态轨迹"。设备名是轨迹的一部分，
         删掉设备，那条轨迹就读不懂了。

    所以这里做的是"从台账消失"，而不是"从数据库消失"：deleted_at 打标记，
    列表过滤掉，历史预约与日志原样保留。资产编号仍被占用（UNIQUE 不排除已删行），
    这是刻意的——报废设备的资产编号不该被新设备复用。
    """

    def _run() -> None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
            if row is None:
                raise ApiError("DEVICE_NOT_FOUND")
            before = dict(row)

            active = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE device_id = ? AND active_flag IS NOT NULL "
                "AND status IN ('booked','picked')",
                (device_id,),
            ).fetchone()
            if int(active[0]) > 0:
                raise ApiError(
                    "DEVICE_HAS_ACTIVE_BOOKINGS",
                    f"该设备还有 {int(active[0])} 条未完成的预约，不能删除",
                    message=f"这台设备还有 {int(active[0])} 条未完成的预约。"
                            "请先在「超期与未取」里处置完，或先下架让它不再被新约。",
                )

            conn.execute("UPDATE devices SET deleted_at = ? WHERE id = ?", (_now(), device_id))
            after = dict(before)
            after["deleted_at"] = _now()
            _log(
                conn,
                actor=actor,
                actor_label="system:device",
                action="device.delete",
                target_type="device",
                target_id=device_id,
                before=before,
                after=after,
                ip=ip,
            )

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


async def booked_slots(device_id: str, slot_date: str) -> dict[str, str]:
    """
    某设备某天已被占用的 {slot_start: user_id}。用于展示空闲状态，**不用于判冲突**。

    为什么返回 user_id 而不是只返回 {"10:00"}：
    R02 的验收口径要求占用视图能区分"空闲 / 已约 / **我的** / 已下架"四态。
    只回时刻集合，这一态就永远做不出来——员工看到自己的单和别人的单长得一样。
    判冲突仍然只信唯一索引，这里多带一个字段不改变那条铁律。
    """

    def _run() -> dict[str, str]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT slot_start, user_id FROM bookings "
                "WHERE device_id = ? AND slot_date = ? AND active_flag IS NOT NULL "
                "AND status IN ('booked','picked')",
                (device_id, slot_date),
            ).fetchall()
            return {str(r["slot_start"]): str(r["user_id"]) for r in rows}

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
    *,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> list[str]:
    """
    批量落单：**同一个连接、同一个事务**，要么全成，要么全败。

    为什么必须同事务：用户一次约 10:00–10:00 和 12:00–12:00 两格，
    如果第一格成功、第二格被别人抢了，只落一半会让用户无法判断自己到底约到了什么，
    而且这条半成功的记录会一直留在库里污染统计。
    所以第二格冲突时抛 SLOT_TAKEN，前面那条 INSERT 随事务一起回滚。

    冲突判定依然只有一条路径：直接 INSERT，靠唯一索引报错。

    actor 与数据写入同一事务：预约单与"谁约的"这条留痕必须同生共死，
    否则会出现"有一单，但不知道是谁下的"这种没法追的记录。
    """

    def _run() -> list[str]:
        ids = [str(uuid.uuid4()) for _ in slots]
        now = _now()
        log_actor = actor or {"id": user_id, "emp_no": f"uid:{user_id[:8]}"}
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

                _log(
                    conn,
                    actor=log_actor,
                    action="booking.create",
                    target_type="booking",
                    target_id=bid,
                    before=None,
                    after={
                        "device_id": device_id,
                        "slot_date": slot_date,
                        "slot_start": start,
                        "slot_end": end,
                        "user_id": user_id,
                        "status": "booked",
                        "remark": remark,
                    },
                    ip=ip,
                )
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


async def cancel_booking(
    booking_id: str,
    *,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> bool:
    """
    取消：status 改 cancelled 且 active_flag 置 NULL。

    active_flag 置 NULL 是关键——唯一索引不约束含 NULL 的行，
    这个格子因此立刻可被别人预约，无需搬数据到归档表。

    读 before 与写变更在同一个事务里：分开读会让日志记下"另一个时刻"的状态，
    并发下甚至可能记成"从 picked 取消"，而实际取消的是 booked。
    """
    def _run() -> bool:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            before = dict(row) if row else None
            cur = conn.execute(
                "UPDATE bookings SET status = 'cancelled', active_flag = NULL "
                "WHERE id = ? AND status = 'booked' AND active_flag IS NOT NULL",
                (booking_id,),
            )
            if cur.rowcount == 0:
                return False
            after = dict(before or {})
            after.update({"status": "cancelled", "active_flag": None})
            _log(
                conn,
                actor=actor or {"id": (before or {}).get("user_id"), "emp_no": "self"},
                action="booking.cancel",
                target_type="booking",
                target_id=booking_id,
                before=before,
                after=after,
                ip=ip,
            )
            return True

    return await asyncio.to_thread(_run)


async def mark_picked(
    booking_id: str,
    *,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> bool:
    def _run() -> bool:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            before = dict(row) if row else None
            now = _now()
            cur = conn.execute(
                "UPDATE bookings SET status = 'picked', picked_at = ? WHERE id = ? AND status = 'booked'",
                (now, booking_id),
            )
            if cur.rowcount == 0:
                return False
            after = dict(before or {})
            after.update({"status": "picked", "picked_at": now})
            _log(
                conn,
                actor=actor or {"id": (before or {}).get("user_id"), "emp_no": "self"},
                action="booking.pickup",
                target_type="booking",
                target_id=booking_id,
                before=before,
                after=after,
                ip=ip,
            )
            return True

    return await asyncio.to_thread(_run)


async def mark_returned(
    booking_id: str,
    remark: str | None = None,
    returned_at: str | None = None,
    *,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> bool:
    """
    登记归还。

    remark 与 returned_at 是 R05 的要求："归还填实际时间与备注"。
    为什么允许用户自填归还时间：设备实际是 15:40 还的、但 17:00 才走到电脑前登记，
    这两种时刻在追责时含义完全不同——前者证明按时归还，后者才是"登记延迟"。
    只用服务端 now() 会把前者记成后者。
    """
    def _run() -> bool:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            before = dict(row) if row else None
            stamp = returned_at or _now()
            # 备注是追加而不是覆盖：预约时填的用途说明（"带去看现场"）同样是有效信息，
            # 归还时补的这半句（"外壳有裂痕"）不该把前半句抹掉。
            old_remark = (before or {}).get("remark")
            merged = remark.strip() if isinstance(remark, str) else ""
            if merged:
                merged = f"{old_remark}｜归还备注：{merged}" if old_remark else f"归还备注：{merged}"
            else:
                merged = old_remark

            cur = conn.execute(
                "UPDATE bookings SET status = 'returned', returned_at = ?, remark = ?, "
                "active_flag = NULL WHERE id = ? AND status IN ('booked','picked')",
                (stamp, merged, booking_id),
            )
            if cur.rowcount == 0:
                return False
            after = dict(before or {})
            after.update({
                "status": "returned", "returned_at": stamp, "remark": merged,
                # 归还就把格子放回去。最初这里漏了这一句，后果是"归还过的格子
                # 永久不可再约"——展示层看着是空的（booked_slots 过滤了 status），
                # 一点提交就被唯一索引挡回 409，用户完全无法理解发生了什么。
                "active_flag": None,
            })
            _log(
                conn,
                actor=actor or {"id": (before or {}).get("user_id"), "emp_no": "self"},
                action="booking.return",
                target_type="booking",
                target_id=booking_id,
                before=before,
                after=after,
                ip=ip,
            )
            return True

    return await asyncio.to_thread(_run)


async def expire_bookings(now: str) -> int:
    """
    把已过时段仍未取走的单标记为 expired，并释放格子。

    必须写成幂等的 UPDATE：重复执行结果一致。
    在 Python 里循环"取出→判断→更新"会在多进程下重复处理，
    也会在两次扫描之间产生竞态。

    实现上仍是一条 UPDATE，但**先 SELECT 出 id 再逐条记日志**：
    过期释放也是一种状态变更，R07 要求它可追溯。
    用一个 WHERE 子句兼顾两边（同事务，条件相同，结果必然一致）。
    """
    def _run() -> int:
        cutoff_sql = "active_flag IS NOT NULL AND status = 'booked' AND (slot_date || 'T' || slot_end) < ?"
        with _connect() as conn:
            rows = conn.execute(f"SELECT * FROM bookings WHERE {cutoff_sql}", (now,)).fetchall()
            if not rows:
                return 0
            cur = conn.execute(
                f"UPDATE bookings SET status = 'expired', active_flag = NULL WHERE {cutoff_sql}",
                (now,),
            )
            for row in rows:
                before = dict(row)
                after = dict(before)
                after.update({"status": "expired", "active_flag": None})
                _log(
                    conn,
                    actor=None,
                    actor_label="system:expire_worker",
                    action="booking.expire",
                    target_type="booking",
                    target_id=str(before["id"]),
                    before=before,
                    after=after,
                )
            return int(cur.rowcount)

    return await asyncio.to_thread(_run)


# =============================================================================
# 超期与未取（R06）
# -----------------------------------------------------------------------------
# 为什么是三条查询而不是文档里的两条：
#   expire_worker 每 60 秒会把"过了时段还没取走"的单自动置 expired（R05 要求，
#   否则格子会被幽灵预约占着）。这意味着真正意义上的"过时未取"在库里
#   只存活不到一分钟——管理员永远看不到一个"待处置"的过时未取列表。
#   所以拆成语义更能指导动作的三类：
#     awaiting_pickup  时段已开始、还没登记取走 → 管理员现场催一下，或强制结束
#     not_returned     取走了、时段结束还没还 → 真正需要处置的那一类
#     missed_recent    已自动过期且从未取走 → 只读，用于追责与黑名单
# =============================================================================


async def list_overdue(now: str) -> dict[str, list[dict[str, Any]]]:
    """
    超期与未取的三个清单。

    now 形如 "2026-11-05T17:30"，与库内 slot_date || 'T' || slot_end 字典序可比。
    """

    def _run() -> dict[str, list[dict[str, Any]]]:
        base = (
            "SELECT b.*, d.asset_no, d.name AS device_name, d.location, "
            "u.emp_no, u.name AS user_name, u.dept "
            "FROM bookings b JOIN devices d ON d.id = b.device_id "
            "JOIN users u ON u.id = b.user_id "
        )
        with _connect() as conn:
            awaiting = conn.execute(
                base + "WHERE b.active_flag IS NOT NULL AND b.status = 'booked' "
                "AND (b.slot_date || 'T' || b.slot_start) <= ? "
                "ORDER BY b.slot_date ASC, b.slot_start ASC",
                (now,),
            ).fetchall()
            not_returned = conn.execute(
                base + "WHERE b.active_flag IS NOT NULL AND b.status = 'picked' "
                "AND (b.slot_date || 'T' || b.slot_end) < ? "
                "ORDER BY b.slot_date ASC, b.slot_start ASC",
                (now,),
            ).fetchall()
            # 近 7 天，只读。再往前的看过也没法处置，只会把列表撑长。
            missed = conn.execute(
                base + "WHERE b.status = 'expired' AND b.picked_at IS NULL "
                "AND b.slot_date >= date(?, '-7 day') "
                "ORDER BY b.slot_date DESC, b.slot_start DESC LIMIT 100",
                (now[:10],),
            ).fetchall()
            return {
                "awaiting_pickup": [dict(r) for r in awaiting],
                "not_returned": [dict(r) for r in not_returned],
                "missed_recent": [dict(r) for r in missed],
            }

    return await asyncio.to_thread(_run)


async def force_close_booking(
    booking_id: str,
    *,
    reason: str,
    actor: dict[str, Any] | None = None,
    ip: str | None = None,
) -> dict[str, Any]:
    """
    管理员强制结束（R06）。

    两种入参状态对应两个终态，不做"一律置 expired"的粗暴处理：
      booked → expired   人根本没来，格子作废
      picked → returned  设备已经取走，强行置 expired 会让"设备在哪"彻底失联；
                         置 returned 至少如实表达"这台已回到可用池"，
                         是否真的完好由备注说明，由人判断。
    """
    def _run() -> dict[str, Any]:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            if row is None:
                raise ApiError("BOOKING_NOT_FOUND")
            before = dict(row)

            from_status = before.get("status")
            if from_status == "booked":
                to_status = "expired"
            elif from_status == "picked":
                to_status = "returned"
            else:
                raise ApiError(
                    "BOOKING_STATE_INVALID",
                    f"当前状态 {from_status} 不在可强制结束的范围内",
                    message="这条预约已经结束了，刷新页面试试。",
                )

            stamp = _now()
            old_remark = before.get("remark")
            mark = f"管理员强制结束（{reason}）" if reason else "管理员强制结束"
            remark = f"{old_remark}｜{mark}" if old_remark else mark

            conn.execute(
                "UPDATE bookings SET status = ?, active_flag = NULL, remark = ?, "
                "returned_at = CASE WHEN ? = 'returned' THEN ? ELSE returned_at END "
                "WHERE id = ?",
                (to_status, remark, to_status, stamp, booking_id),
            )

            after = dict(before)
            after.update({
                "status": to_status, "active_flag": None, "remark": remark,
                "returned_at": stamp if to_status == "returned" else before.get("returned_at"),
            })
            _log(
                conn,
                actor=actor,
                actor_label="system:force_close",
                action="booking.force_close",
                target_type="booking",
                target_id=booking_id,
                before=before,
                after={**after, "reason": reason},
                ip=ip,
            )
            return {"id": booking_id, "from": from_status, "to": to_status}

    return await asyncio.to_thread(_run)


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
    session_id: str,
    user_id: str,
    question: str,
    trace_id: str | None,
    model: str | None,
    prompt_version: int | None = None,
) -> str:
    """开一次运行。返回 run_id，后续所有 step 都挂在它下面。"""
    run_id = str(uuid.uuid4())

    def _run() -> str:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO agent_runs (id, session_id, user_id, trace_id, question, model, "
                "prompt_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, session_id, user_id, trace_id, question[:500], model,
                 prompt_version, _now()),
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
# 对话审计（批次 4）
# -----------------------------------------------------------------------------
# 这一页要回答的不是"系统运行得好不好"，而是三个具体到人的问题：
#
#   1) 谁在什么时候问了什么？（追责的起点）
#   2) 模型有没有试图写库，人最后点没点确认？（这是审计的核心，不是聊天记录）
#   3) 出问题那句话，模型当时调了什么工具、拿到什么回执？（复盘的依据）
#
# 所以查询一律从 agent_runs 出发，而不是从 chat_messages。
# 聊天记录只回答"说了什么"，回答不了"做了什么"。
# =============================================================================


async def list_runs(
    *,
    keyword: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_flagged: bool = False,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    审计列表。

    三个统计列用关联子查询而不是 JOIN + GROUP BY：
    agent_steps 一个 run 可能有十几行，JOIN 后再聚合会让 LIMIT 语义
    变成"先乘出来再截断"，翻页时数量对不上。子查询只算一次，且不影响行数。

    日期用字符串前缀比较（created_at 是 `2026-11-05T10:30:00+08:00`）：
    这种 ISO 格式在同一时区偏移下字典序即时间序，所以 `>= '2026-11-05'`
    等价于"当天 00:00 之后"。跨时区偏移会失效——本项目单机单时区，
    真的要跨时区，那时得改成存 UTC 时间戳。
    """

    def _run() -> list[dict[str, Any]]:
        sql = (
            "SELECT r.id, r.session_id, r.user_id, r.trace_id, r.question, r.status, "
            "r.rounds, r.total_ms, r.model, r.prompt_version, r.error_code, "
            "r.created_at, r.finished_at, "
            "u.emp_no, u.name AS user_name, u.dept, "
            "(SELECT COUNT(*) FROM agent_steps s WHERE s.run_id = r.id) AS step_count, "
            "(SELECT COUNT(*) FROM agent_steps s WHERE s.run_id = r.id AND s.status = 'error') AS error_steps, "
            "(SELECT COUNT(*) FROM pending_actions p WHERE p.run_id = r.id) AS pending_count, "
            "f.reason AS flag_reason, f.flagged_at, "
            "fu.emp_no AS flagged_by_no "
            "FROM agent_runs r "
            "LEFT JOIN users u ON u.id = r.user_id "
            "LEFT JOIN audit_flags f ON f.run_id = r.id "
            "LEFT JOIN users fu ON fu.id = f.flagged_by "
            "WHERE 1 = 1"
        )
        args: list[Any] = []

        if keyword:
            sql += " AND (u.emp_no LIKE ? OR u.name LIKE ? OR r.question LIKE ?)"
            like = f"%{keyword.strip()}%"
            args.extend([like, like, like])
        if status:
            sql += " AND r.status = ?"
            args.append(status)
        if date_from:
            sql += " AND r.created_at >= ?"
            args.append(date_from)
        if date_to:
            # 用户在筛选框里选"到 11-05"，意思是"包含 11-05 全天"。
            # 直接用 `<= '2026-11-05'` 会把当天的记录全漏掉——因为
            # '2026-11-05T10:30:00' > '2026-11-05'。这是差一天的经典写法错误。
            try:
                nxt = (date.fromisoformat(date_to) + timedelta(days=1)).isoformat()
                sql += " AND r.created_at < ?"
                args.append(nxt)
            except ValueError:
                pass
        if only_flagged:
            sql += " AND f.run_id IS NOT NULL"

        sql += " ORDER BY r.created_at DESC, r.id DESC LIMIT ?"
        args.append(limit)

        with _connect() as conn:
            return [dict(r) for r in conn.execute(sql, args).fetchall()]

    return await asyncio.to_thread(_run)


async def fetch_run(run_id: str) -> dict[str, Any] | None:
    """单次运行的详情（带员工信息与标记）。"""

    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute(
                "SELECT r.*, u.emp_no, u.name AS user_name, u.dept, "
                "f.reason AS flag_reason, f.flagged_at, fu.emp_no AS flagged_by_no "
                "FROM agent_runs r "
                "LEFT JOIN users u ON u.id = r.user_id "
                "LEFT JOIN audit_flags f ON f.run_id = r.id "
                "LEFT JOIN users fu ON fu.id = f.flagged_by "
                "WHERE r.id = ?",
                (run_id,),
            ).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(_run)


async def list_pendings_by_run(run_id: str) -> list[dict[str, Any]]:
    """
    这次运行产生的写意图，以及它们最终被怎么处理。

    这是审计页最关键的一块：`agent_steps` 只能说明"模型想写"，
    真正说明"最后有没有写成"的是这张表——status 为 confirmed 才算数。
    """

    def _run() -> list[dict[str, Any]]:
        with _connect() as conn:
            # 不 JOIN users：pending_actions.user_id 是发起人，也就是**唯一有权确认的人**
            # （decide_pending 里会校验）。再 JOIN 一次只会拿到同一个名字。
            rows = conn.execute(
                "SELECT * FROM pending_actions WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                try:
                    d["summary"] = json.loads(d["summary_json"]) if d.get("summary_json") else {}
                except (ValueError, TypeError):
                    d["summary"] = {}
                out.append(d)
            return out

    return await asyncio.to_thread(_run)


async def audit_summary(days: int = 7) -> dict[str, Any]:
    """
    近 N 天的总体情况。

    刻意统计 `awaiting_confirm` 占比：它衡量的是"模型有多想直接写库"。
    这个数越高，说明提示词里那条"你没有直接完成预约的权限"越需要加强。
    """

    def _run() -> dict[str, Any]:
        since = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
        with _connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) AS answered, "
                "SUM(CASE WHEN status = 'awaiting_confirm' THEN 1 ELSE 0 END) AS awaiting_confirm, "
                "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed, "
                "SUM(CASE WHEN status = 'max_rounds' THEN 1 ELSE 0 END) AS max_rounds, "
                "AVG(rounds) AS avg_rounds, AVG(total_ms) AS avg_ms "
                "FROM agent_runs WHERE created_at >= ?",
                (since,),
            ).fetchone()
            steps = conn.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors, "
                "SUM(CASE WHEN status = 'interrupted' THEN 1 ELSE 0 END) AS interrupted "
                "FROM agent_steps WHERE created_at >= ?",
                (since,),
            ).fetchone()
            flagged = conn.execute("SELECT COUNT(*) AS n FROM audit_flags").fetchone()

        return {
            "days": days,
            "total": int(row["total"] or 0),
            "answered": int(row["answered"] or 0),
            "awaiting_confirm": int(row["awaiting_confirm"] or 0),
            "failed": int(row["failed"] or 0),
            "max_rounds": int(row["max_rounds"] or 0),
            "avg_rounds": round(float(row["avg_rounds"] or 0), 1),
            "avg_ms": int(row["avg_ms"] or 0),
            "steps": int(steps["n"] or 0),
            "step_errors": int(steps["errors"] or 0),
            "step_interrupted": int(steps["interrupted"] or 0),
            "flagged": int(flagged["n"] or 0),
        }

    return await asyncio.to_thread(_run)


async def flag_run(
    run_id: str,
    reason: str,
    actor: dict[str, Any] | None,
    ip: str | None = None,
) -> bool:
    """
    标记一次运行为"需要跟进"。已标记则更新理由（而不是报错）——
    管理员第二次点标记，本意多半是"补充说明"，不是"我要标两次"。
    """

    def _run() -> bool:
        now = _now()
        with _connect() as conn:
            existed = conn.execute(
                "SELECT reason FROM audit_flags WHERE run_id = ?", (run_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO audit_flags (run_id, reason, flagged_by, flagged_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET reason = excluded.reason, "
                "flagged_by = excluded.flagged_by, flagged_at = excluded.flagged_at",
                (run_id, reason, actor.get("id") if actor else None, now),
            )
            _log(
                conn,
                actor=actor,
                action="audit.flag" if not existed else "audit.reflag",
                target_type="run",
                target_id=run_id,
                before={"reason": existed["reason"]} if existed else None,
                after={"reason": reason},
                ip=ip,
            )
            return True

    return await asyncio.to_thread(_run)


async def unflag_run(run_id: str, actor: dict[str, Any] | None, ip: str | None = None) -> bool:
    """撤销标记。撤销本身也留痕——"谁把这条标记去掉了"同样值得追问。"""

    def _run() -> bool:
        with _connect() as conn:
            row = conn.execute("SELECT reason FROM audit_flags WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM audit_flags WHERE run_id = ?", (run_id,))
            _log(
                conn,
                actor=actor,
                action="audit.unflag",
                target_type="run",
                target_id=run_id,
                before={"reason": row["reason"]},
                ip=ip,
            )
            return True

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


async def stats(today: str, now: str | None = None) -> dict[str, int]:
    """
    概览数字。

    now 是 R06 加的两个计数需要的：它们不是"历史有多少"，而是"此刻有多少
    需要人去处置"——管理员上班第一件事应该是看这两个数，而不是看总量。
    不传 now 时这两个数退化成 0，避免影响只关心总量的调用方。
    """
    stamp = now or ""

    def _run() -> dict[str, int]:
        with _connect() as conn:
            row = conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM devices WHERE deleted_at IS NULL) AS devices, "
                "(SELECT COUNT(*) FROM devices WHERE status = 'offline' AND deleted_at IS NULL) AS devices_offline, "
                "(SELECT COUNT(*) FROM bookings WHERE slot_date = ? AND active_flag IS NOT NULL) AS bookings_today, "
                "(SELECT COUNT(*) FROM bookings WHERE active_flag IS NOT NULL) AS bookings_active, "
                "(SELECT COUNT(*) FROM users) AS users, "
                "(SELECT COUNT(*) FROM users WHERE status = 'disabled') AS users_disabled, "
                "(SELECT COUNT(*) FROM chat_sessions) AS sessions, "
                "(SELECT COUNT(*) FROM chat_messages) AS messages, "
                "(SELECT COUNT(*) FROM bookings WHERE active_flag IS NOT NULL AND status = 'booked' "
                " AND (slot_date || 'T' || slot_start) <= ?) AS awaiting_pickup, "
                "(SELECT COUNT(*) FROM bookings WHERE active_flag IS NOT NULL AND status = 'picked' "
                " AND (slot_date || 'T' || slot_end) < ?) AS not_returned",
                (today, stamp, stamp),
            ).fetchone()
            return {k: int(row[k]) for k in row.keys()}

    return await asyncio.to_thread(_run)


# =============================================================================
# 制度文档登记（批次 5）
# -----------------------------------------------------------------------------
# 这一节只有元数据，没有正文。正文永远是磁盘上的文件，理由见表结构处的注释。
# 页面需要的信息全在这里：谁在磁盘上、谁被停用、各占多少切片。
# =============================================================================


async def list_kb_documents() -> list[dict[str, Any]]:
    """全部登记过的制度文档，启用的排前面，同组按来源和文件名排。"""
    def _run() -> list[dict[str, Any]]:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM kb_documents "
                "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, origin, display_name"
            ).fetchall()
            return [dict(r) for r in rows]

    return await asyncio.to_thread(_run)


async def get_kb_document(doc_key: str) -> dict[str, Any] | None:
    def _run() -> dict[str, Any] | None:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM kb_documents WHERE doc_key = ?", (doc_key,)).fetchone()
            return dict(row) if row else None

    return await asyncio.to_thread(_run)


async def disabled_doc_keys() -> set[str]:
    """
    被停用的 doc_key 集合。检索重建时用它把文件从语料里剔掉。

    为什么不在扫描阶段就按数据库过滤（在 SQL 里 join 文件列表）：
    文件系统是扫描的源头，数据库只是"备注"。先扫盘、后减掉停用项，
    这样"库里有记录但文件已删"和"文件在但没登记"两种状态都不会漏。
    """
    def _run() -> set[str]:
        with _connect() as conn:
            rows = conn.execute("SELECT doc_key FROM kb_documents WHERE status = 'disabled'").fetchall()
            return {str(r["doc_key"]) for r in rows}

    return await asyncio.to_thread(_run)


async def count_kb_documents(status: str | None = None) -> int:
    def _run() -> int:
        with _connect() as conn:
            if status:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM kb_documents WHERE status = ?", (status,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) AS n FROM kb_documents").fetchone()
            return int(row["n"])

    return await asyncio.to_thread(_run)


async def sync_kb_documents(docs: list[dict[str, Any]]) -> None:
    """
    重建后把磁盘现状同步进登记表。

    两条规则，缺一不可：
      1) 本次扫到的文档 → 插入或更新元数据，并把 on_disk 拉回 1；
      2) 本次没扫到的 → on_disk 置 0，但**保留 status**，
         因为"管理员停用了"和"文件被删了"是两件事，不能互相覆盖。
    更新时**不动 status**：一次重建不该悄悄把停用的文档重新启用。
    """
    now = _now()

    def _run() -> None:
        with _connect() as conn:
            for d in docs:
                conn.execute(
                    "INSERT INTO kb_documents (doc_key, display_name, origin, status, sha256, "
                    "size_bytes, chunks, file_mtime, on_disk, synced_at) "
                    "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(doc_key) DO UPDATE SET "
                    "display_name = excluded.display_name, origin = excluded.origin, "
                    "sha256 = excluded.sha256, size_bytes = excluded.size_bytes, "
                    "chunks = excluded.chunks, file_mtime = excluded.file_mtime, "
                    "on_disk = 1, synced_at = excluded.synced_at",
                    (
                        d["doc_key"],
                        d["display_name"],
                        d["origin"],
                        d.get("sha256"),
                        int(d.get("size_bytes") or 0),
                        int(d.get("chunks") or 0),
                        d.get("file_mtime"),
                        now,
                    ),
                )
            if docs:
                marks = ",".join("?" * len(docs))
                conn.execute(
                    f"UPDATE kb_documents SET on_disk = 0 WHERE doc_key NOT IN ({marks})",
                    [d["doc_key"] for d in docs],
                )
            else:
                conn.execute("UPDATE kb_documents SET on_disk = 0")

    await asyncio.to_thread(_run)


async def set_kb_document_status(
    doc_key: str,
    status: str,
    actor: dict[str, Any] | None,
    ip: str | None = None,
) -> dict[str, Any]:
    """启用 / 停用一份制度文档。状态没变时如实返回 changed=False。"""
    if status not in ("active", "disabled"):
        raise ApiError("INVALID_REQUEST", f"状态只能是 active / disabled，收到 {status}")

    def _run() -> dict[str, Any]:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM kb_documents WHERE doc_key = ?", (doc_key,)).fetchone()
            if row is None:
                raise ApiError("KB_DOC_NOT_FOUND")
            before = dict(row)
            if before["status"] == status:
                return {"doc_key": doc_key, "status": status, "changed": False}
            conn.execute(
                "UPDATE kb_documents SET status = ?, synced_at = ? WHERE doc_key = ?",
                (status, _now(), doc_key),
            )
            _log(
                conn,
                actor=actor,
                action="kb.disable" if status == "disabled" else "kb.enable",
                target_type="kb",
                target_id=doc_key,
                before={"status": before["status"], "chunks": before["chunks"]},
                after={"status": status},
                ip=ip,
            )
            return {"doc_key": doc_key, "status": status, "changed": True, "chunks": int(before["chunks"] or 0)}

    return await asyncio.to_thread(_run)


async def delete_kb_document(
    doc_key: str,
    actor: dict[str, Any] | None,
    ip: str | None = None,
) -> dict[str, Any]:
    """
    删除登记（仅限上传文档；内置文档只能停用，见 web 层）。物理文件由 rag 层先移走。
    """
    def _run() -> dict[str, Any]:
        with _connect() as conn:
            row = conn.execute("SELECT * FROM kb_documents WHERE doc_key = ?", (doc_key,)).fetchone()
            if row is None:
                raise ApiError("KB_DOC_NOT_FOUND")
            before = dict(row)
            if before["origin"] != "uploaded":
                raise ApiError("KB_DOC_BUILTIN", "内置文档只能停用，不能删除")
            conn.execute("DELETE FROM kb_documents WHERE doc_key = ?", (doc_key,))
            _log(
                conn,
                actor=actor,
                action="kb.delete",
                target_type="kb",
                target_id=doc_key,
                before={"display_name": before["display_name"], "chunks": before["chunks"]},
                ip=ip,
            )
            return {"doc_key": doc_key, "display_name": before["display_name"]}

    return await asyncio.to_thread(_run)

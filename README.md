# SharingDeviceBooking

共享设备借用系统。当前进度：**阶段 4 —— 智能体编排（Function Calling + 执行循环）**。

系统已从"固定流程问答"升级为"自主决策的智能体"：模型自己决定要不要检索、要不要查时段、
要不要校验，但**写操作永远由人点击确认**。

## 已实现

**员工侧**

- `POST /api/v1/chat`：SSE 流式问答，走智能体执行循环（模型决策 → 执行工具 → 结果回填，最多 5 轮）。
- 单页聊天界面：打字机效果、分步进度提示、引用卡片可展开、待确认卡片可点击。
- 未配置大模型密钥时自动降级为「制度原文直出」，不白屏、不伪造回答。
- 预约：6 格时段（08:00–20:00，每 2 小时一格）、单日 2 格上限、冲突 409、过期自动释放。
- `/bookings` 页面：取消、登记取走、登记归还。**这三个动作只能由人点击，不开放给模型。**

**智能体（阶段 4 新增）**

- 7 个工具，其中 2 个写操作（`request_booking` / `cancel_booking`）**只打标记不执行**。
- 写操作前强制中断：模型发起 → 服务端生成待确认动作 → 用户点击 → 才落单。
- 每轮决策落 `agent_steps` 表（工具名、参数、耗时、结果摘要、错误码），供阶段 5 分析。

**管理员**

- `/admin` 概览、`/admin/devices` 设备台账（上下架）。
- 设备下架只拦新预约，不动已生效的单——人与人之间的协调不由系统代劳。

**认证**

- 工号 + 密码登录，scrypt 哈希，HS256 JWT 存 HttpOnly Cookie，CSRF 双提交校验。
- 首次登录强制改密；连续 5 次密码错误锁定 10 分钟。
- 权限以数据库为准（不信任 token 里的 role），降权/停用即时生效。

## 运行

```bash
uv venv .venv --python 3.13
uv pip install --python .venv/bin/python numpy faiss-cpu rank-bm25 jieba httpx fastapi "uvicorn[standard]" pyjwt jinja2 python-multipart

.venv/bin/uvicorn app.main:app --port 8000
# 打开 http://127.0.0.1:8000 → 会跳到 /login
```

演示账号（**首次登录会强制改密**，密码规则见 `app/seed.py`）：

| 工号 | 密码 | 角色 |
|---|---|---|
| `admin` | `Admin@12345` | 管理员 |
| `1001` | `Staff@12345` | 员工 |
| `1002` | `Staff@12345` | 员工 |

配置真实模型（**智能体循环需要模型支持 Function Calling**；不配则降级为原文直出）：

```bash
export LLM_PROVIDER=qwen          # openai / deepseek / qwen / ollama
export LLM_API_KEY=sk-xxx
export LLM_MODEL=qwen-plus
export JWT_SECRET=...             # 不设会自动生成并落盘到 data/.jwt_secret
```

> 降级模式下聊天**不会**走智能体循环（没有模型就没有决策），只会检索并贴出原文。
> 想看确认卡片，需要配一个支持工具调用的模型；或直接跑 `tests/test_harness.py` 里的
> `test_sse_interrupt_frames`，它用脚本化假模型跑通了同一条链路。

## 核心接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/chat` | 流式问答（需登录），智能体循环 |
| POST | `/api/v1/agent/confirm` | **确认/拒绝待确认动作**（写操作的唯一执行入口） |
| GET | `/api/v1/agent/pending/{id}` | 查待确认动作状态（刷新页面后判断卡片是否还可点） |
| GET | `/api/v1/devices?date=` | 设备列表 + 当日可用格数 |
| GET | `/api/v1/devices/{id}/slots?date=` | 某设备某天 6 格状态 |
| POST | `/api/v1/bookings` | 创建预约，冲突 409（页面直接下单走这条） |
| GET | `/api/v1/bookings/mine` | 我的预约 |
| POST | `/api/v1/bookings/{id}/cancel` | 取消 |
| POST | `/api/v1/admin/devices/{id}/status` | 上下架（管理员） |
| GET | `/api/v1/auth/me` | 当前用户 |

## 智能体编排

### 为什么是自研 harness，而不是 LangGraph

`harness` 不是一个可安装的框架，而是"模型外层运行循环"这个模式的通称。三个真实选项：

| 选项 | 优势 | 本项目的取舍 |
|---|---|---|
| LangGraph | 图调度、checkpoint 持久化、原生 `interrupt()` | 会重写现有 SSE 链路；本项目 7 个工具一条主干 |
| OpenAI Agents SDK | 开箱即用 | 绑定 OpenAI 协议，与"可换供应商"冲突 |
| **自研薄 harness** | 约 400 行、零新依赖、与现有 409/幂等/唯一索引完全对齐 | 选它 |

决定性理由：**中断之后，执行确认的必须是 REST 路径，不能回到模型。**
图框架的 `resume` 语义是"从断点继续跑图"，而这里要的恰恰是"永远不要继续跑图"——
用框架反而要和它的设计意图对抗。

### 7 个工具

| 工具 | 类型 | 参数 | 说明 |
|---|---|---|---|
| `search_policy` | 读 | `query` | 制度检索，返回带编号的片段（编号与引用卡片一一对应） |
| `find_available_slots` | 读 | `date?`, `asset_no?` | 查某天空闲格，返回 `device_id` 供后续使用 |
| `get_device_info` | 读 | `asset_no` | 设备状态、位置、型号 |
| `get_my_bookings` | 读 | `status?` | 只能查到本人名下的单 |
| `validate_booking` | 校验 | `device_id`, `slot_date`, `slot_starts` | 通过后签发 5 分钟一次性凭证 |
| `request_booking` | **写** | **仅 `validation_id`** | 被中断，转成待确认 |
| `cancel_booking` | **写** | `booking_id` | 同样被中断 |

### 顺序约束写在签名里，不写在提示词里

`request_booking` **不接受设备和时段**，唯一参数是 `validation_id`。
即使模型被"别废话直接约"诱导、或提示词被注入，它手里也没有凭证，调用必然失败。
约束写在签名里，就不可能被一段话绕过。

四道闸门，每一道都能独立挡住双单：

1. **签名**：没有凭证就调不了；
2. **待确认动作的原子 claim**（`WHERE status='pending'`）：连点两下只有一次生效；
3. **凭证的原子 consume**：同一张凭证用第二次返回空；
4. **唯一索引**：并发抢同一格，数据库兜底。

### 中断流程

```
模型调用 request_booking
   │
   ├── 凭证无效（幻觉/别人的/过期）→ 回填错误给模型，循环继续，**不发卡片**
   └── 凭证有效 → 落 pending_actions → 发 confirm_required → 循环结束
                        │
                        └── 用户点击 → POST /api/v1/agent/confirm
                                        └── place_booking（唯一索引兜底）
```

凭证无效时**不中断**这条很容易漏：如果照样发卡片，用户看到"确认预约"点下去必然失败。

### 日志表（阶段 5 的数据源）

- `agent_runs`：一次提问 = 一行。`status` 区分 `answered` / `awaiting_confirm` / `max_rounds` / `failed`。
  其中 `awaiting_confirm` 是最有价值的信号——它直接反映模型对写操作的冲动程度。
- `agent_steps`：一次工具调用 = 一行。`status` 区分 `ok` / `error` / `interrupted` / `skipped`。
- `pending_actions`：模型的写意图。它把"模型想做什么"和"系统做了什么"在数据上彻底分开。

## 不变量的保证方式

**同一台设备的同一格只能有一单**，靠数据库唯一索引，不靠应用层判断：

```sql
CREATE UNIQUE INDEX uq_booking_slot ON bookings(device_id, slot_date, slot_start, active_flag);
```

- 下单只有一条路径：直接 `INSERT`，捕获 `IntegrityError` 转 409。**禁止"先 SELECT 判空再 INSERT"**（TOCTOU 竞态，并发下必然双单）。
- 取消时 `active_flag = NULL`。唯一索引不约束含 NULL 的行，所以这个格子立刻可被重约，无需把取消记录搬到归档表。
- 实测：50 并发抢同一格 → 恰好 1 个 201、49 个 409、库内 1 条有效单。

**时段边界**由 `app/domain/slots.py` 的纯函数保证，全部有单测（`tests/test_slots.py`）。含一条容易做错的规则：**时段一开始就不能再被预约**（10:17 时 10:00–12:00 已不可约），否则用户会约到半格，且可能与现场使用者冲突。

## 语料

- 手册放 `docs/manuals/`，**不要**和调研报告混在一个索引里——实测 223 个调研切片会把规程文档挤出 top3。
- 可用 `RAG_DOCS_DIR` / `RAG_PATTERN` 覆盖默认值。

## 目录

```
app/
  main.py              应用装配、启动预热索引、起过期扫描任务
  errors.py            错误码表与异常处理器
  auth.py              密码哈希、JWT、CSRF、权限依赖
  db.py                SQLite：用户/会话/设备/预约/校验凭证/智能体轨迹
  domain/slots.py      时段规则（纯函数，可单测）
  booking_service.py   预约业务逻辑（REST 与工具层共用）
  booking_api.py       预约 REST + 过期扫描
  chat_api.py          聊天路由与编排（有模型走智能体，无模型走降级）
  agent_api.py         确认接口（写操作的唯一执行入口）
  web.py               登录/改密/后台页面（Jinja2 服务端渲染）
  agent/
    tools.py           7 个工具：schema + 执行 + 注册表（write 打 side_effect 标记）
    harness.py         执行循环：决策 → 执行 → 回填，最多 5 轮，写操作前中断
    confirm.py         待确认动作的准备与执行（唯一碰 place_booking 的地方）
    prompts.py         系统提示词（含日期上下文、三步流程、禁止话术）
  seed.py              首次启动的种子账号与设备
  sse.py / llm.py / rag.py
templates/             后台与登录页模板
static/                聊天界面 + 后台样式
tests/test_slots.py    时段规则单测
tests/test_harness.py  执行循环、中断、确认、SSE 接线（用脚本化假模型）
scripts/stage1_rag_prototype.py   阶段 1 单文件原型（检索核心模块唯一定义处）
docs/manuals/          设备借用制度（RAG 语料）
```

## 已知边界

- 会话互斥锁与限流都在进程内，**单 worker 有效**；多 worker 需换数据库锁/Redis。
- 未配置 embedding 接口时用本地哈希向量，只捕捉字面重合，语义检索能力接近于无。
- 预约冲突只保证单实例内一致（SQLite 单写者模型本身保证了这一点）；迁移 PostgreSQL 时唯一索引语义不变。
- 智能体的**最终回答是非流式的**：决策要拿完整 `tool_calls`，只能用非流式调用，
  所以首字延迟 = 回答生成完毕的时刻（100–300 字约 2–4 秒）。
  要真流式需改成"决策非流式 + 收尾再发一次流式请求"，代价是多一次调用且两次回答可能不一致。
- 待确认动作没有后台定时清理，靠 `expires_at` 惰性判定（`decide_pending` 里带过期条件）。
  量大了需要加一轮 `db.expire_pending()` 的定时任务。
- 审计页、制度知识库管理、提示词模板管理尚未实现（后台导航里标了「待上线」）。

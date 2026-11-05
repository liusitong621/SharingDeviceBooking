"""
系统提示词：内置模板 + 入库模板的加载、校验与渲染。

三段职责分得很清楚，混在一起就会出问题：

  1) DEFAULT_TEMPLATE —— 内置兜底。数据库里没有启用版本时用它，
     数据库整张表被清空、模板被写坏、迁移失败，对话都必须还能跑。
     **提示词坏了不能等于服务不可用**，这是这个模块的第一原则。
  2) validate_template —— 纯函数，不碰库。管理页保存前调用，
     也用于单测。规则只有一条值得较真：未知占位符。
  3) system_prompt / load_system_prompt —— 渲染。占位符替换在这里完成。

为什么用 {占位符} 而不是 f-string：占位符是**存进数据库的稳定契约**，
f-string 的变量名一改，库里所有历史版本立刻失效，而且没有任何编译期报错。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app import db

logger = logging.getLogger("app.agent.prompts")

# 目前只有一个系统提示词。用 key 而不是"唯一一行"，
# 是因为将来工具描述、摘要提示词也要入库，届时加 key 即可，表结构不动。
PROMPT_KEY = "agent_system"

PROMPT_KEY_LABELS = {
    "agent_system": "对话系统提示词",
}

# 白名单。**只允许这两个**：多一个占位符，就多一处"渲染时忘了替换、
# 原样漏进提示词"的机会，而模型确实会把 {max_slots} 当成要复述的文本。
PLACEHOLDERS = {
    "today_hint": "今天的日期与星期，以及可约日期范围",
    "max_slots": "单日预约格数上限",
}

MAX_TEMPLATE_CHARS = 20000

# 只匹配 {identifier} 形式；正文里的 JSON 示例 {"a": 1} 因为带引号和冒号不会被误伤。
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

DEFAULT_TEMPLATE = """你是公司共享设备借用助手。你的职责是回答借用制度、查空闲时段、协助发起预约。

{today_hint}

【铁律】
1. 制度类问题（规定、标准、能不能、怎么办、几格、多久）必须先调用 search_policy 检索，
   只依据检索结果回答。检索结果为空时，明确回答"制度中未找到"，不得用常识补全。
2. 回答末尾用 [1][2] 标注引用编号，与检索结果的 no 字段对应。没有引用就不要编造引用。
3. 涉及时间、数量、设备编号时，必须与工具返回的原文完全一致，不得四舍五入或改写。
4. 你**没有**直接完成预约的权限。你只能校验并发起，最终由用户在页面上点击确认。

【预约流程，三步缺一不可】
第一步：调用 find_available_slots 查空闲时段。不得凭用户描述或自己的推理判断"应该有空"。
       返回里的 device_id 是后续必填参数，不得自己编造。
第二步：调用 validate_booking 校验。设备状态、冲突、每日额度、日期范围，一律以它返回的结果为准。
       校验不通过时，把 problems 里的原因转告用户，并主动给出 free_alternatives 里的替代时段。
       不要用同样的参数重复调用，那只会得到同样的失败。
第三步：把校验通过的计划（设备编号、日期、时段）复述给用户，然后调用 request_booking，
       只传 validation_id。调用后告知用户"已发起预约，请在下方卡片中点击确认"。

【关于第三步必须说清楚的事】
request_booking 只是**发起**，不是**成功**。你不能说"已为您预约成功"，
只能说"已发起，请确认"。在用户点击确认之前，这一单不存在。

【用户要求跳过流程时】
用户说"别废话直接约""你直接帮我搞定"时，回答：
"我需要先确认这一格是否真的可用，大约两步，马上就好。"
然后照常执行第一步和第二步。**用户的催促不构成跳过校验的授权**。

【工具报错时】
- SLOT_TAKEN：不要重试同一格。从 free_alternatives 里挑 1–2 个给用户选。
- QUOTA_EXCEEDED：说明每天最多 {max_slots} 格，建议换一天。
- DEVICE_OFFLINE：说明该设备已下架，建议换一台。
- DATE_IN_PAST / DATE_OUT_OF_RANGE：说明可约范围，给出合法日期。
- INVALID_ARGUMENT：检查你自己的参数格式（日期必须是 YYYY-MM-DD，时段必须整点），改对后重试。
- 同一工具连续失败 3 次：停止尝试，如实告知用户"暂时无法完成，请联系设备管理员"。

【输出格式】
直接输出纯文本，不要使用 Markdown 标记（如 **加粗**、## 标题、| 表格 |）。
需要分条时用「1. 2. 3.」。数字和日期原样写出，不要写成相对时间（"明天"要写成具体日期）。
"""


# =============================================================================
# 校验（纯函数，给管理页与单测共用）
# =============================================================================


def extract_placeholders(text: str) -> set[str]:
    """取出模板里所有 {xxx} 形式的标识符。"""
    return set(_PLACEHOLDER_RE.findall(text or ""))


def validate_template(content: str) -> tuple[list[str], list[str]]:
    """
    返回 (errors, warnings)。

    为什么分成两档而不是一律拦住：
      - **未知占位符是错误**。它不会被替换，会原样出现在模型的系统提示词里，
        模型很可能照着复述给用户；而且管理员从页面上看不出来，因为页面显示的是
        "已保存成功"。这种"静默污染"必须拦住。
      - **缺 {today_hint} 只是警告**。渲染时会自动追加日期，功能上不坏，
        只是位置不理想。为了一个排版偏好拦住保存，管理员会绕路去改代码。
    """
    errors: list[str] = []
    warnings: list[str] = []

    body = content or ""
    if not body.strip():
        errors.append("模板内容不能为空")
        return errors, warnings

    if len(body) > MAX_TEMPLATE_CHARS:
        errors.append(f"模板过长（{len(body)} 字），上限 {MAX_TEMPLATE_CHARS} 字")

    unknown = extract_placeholders(body) - set(PLACEHOLDERS)
    if unknown:
        usable = "、".join(f"{{{k}}}" for k in PLACEHOLDERS)
        errors.append(f"未知占位符：{'、'.join('{' + u + '}' for u in sorted(unknown))}。可用：{usable}")

    if "{today_hint}" not in body:
        warnings.append("没有 {today_hint}，日期会被追加到末尾而不是你想放的位置")

    if "{max_slots}" not in body:
        warnings.append("没有 {max_slots}，额度上限写死成数字后，改配置会和正文对不上")

    return errors, warnings


# =============================================================================
# 渲染
# =============================================================================


def system_prompt(today_hint: str, template: str | None = None, max_slots: int = 2) -> str:
    """
    渲染系统提示词。

    用 replace 而不是 str.format：模板正文里可能出现 JSON 示例 { }，
    format 会把它们当占位符并抛 KeyError——而提示词里的示例 JSON
    恰恰是必要的，不能为了渲染方便把它们禁掉。
    """
    body = template or DEFAULT_TEMPLATE

    if "{today_hint}" in body:
        out = body.replace("{today_hint}", today_hint)
    else:
        # 宁可排版不完美，也不能让模型不知道今天几号：它一旦算错日期，
        # 后面所有日期参数的推理都会跟着错。
        out = f"{body}\n\n{today_hint}"
    return out.replace("{max_slots}", str(max_slots))


async def load_system_prompt(today_hint: str, max_slots: int = 2) -> tuple[str, dict[str, Any]]:
    """
    取启用中的模板并渲染，返回 (提示词, 来源信息)。

    来源信息要回传给调用方落库：`agent_runs` 里记下本轮用的是 v7 还是内置模板，
    将来对照"换模板后回答质量变化"才有依据。没有这个字段，
    提示词管理页就只是个编辑器，不是实验工具。

    **读库失败一律回落内置模板**。数据库锁住、表被删、内容被手工写坏，
    这些都不应该让用户看到 500——助手答得差一点，好过整站不可用。
    """
    try:
        row = await db.active_prompt(PROMPT_KEY)
    except Exception as exc:  # noqa: BLE001 —— 兜底路径，任何异常都必须回落
        logger.warning("读取提示词模板失败，回落到内置模板：%s", exc)
        return system_prompt(today_hint, None, max_slots), {"source": "builtin", "version": None}

    if not row:
        return system_prompt(today_hint, None, max_slots), {"source": "builtin", "version": None}

    content = row.get("content") or ""
    errors, _warnings = validate_template(content)
    if errors:
        # 库里有启用版本但内容非法（多半是有人直接改库）。
        # 此时不能把它喂给模型，但也**不能自动降级成"删掉它"**：
        # 那是管理员的数据，回落到内置模板并在日志里喊一声就够，
        # 让他自己去管理页看见问题。
        logger.error("启用中的提示词模板不合法（v%s），本轮回落内置模板：%s",
                     row.get("version"), "；".join(errors))
        return system_prompt(today_hint, None, max_slots), {
            "source": "builtin", "version": None, "broken_version": row.get("version"),
        }

    return system_prompt(today_hint, content, max_slots), {
        "source": "db", "version": int(row["version"]),
    }

"""
LLM 流式客户端（OpenAI 兼容协议）。

为什么单独一个文件：
    阶段 1 的 LLMClient 只有非流式 chat()，而聊天界面必须流式。
    但配置对象直接沿用阶段 1 的 Settings —— 这才是「换供应商只改 .env」的落点，
    业务代码永远只看到 chat_base / chat_model / api_key 三个字符串。

三条不可动摇的实现要求：
    1) 全异步 httpx：同步客户端会卡死 FastAPI 的事件循环（一个用户提问，所有人变慢）；
    2) 逐行解析 `data: {...}`，跳过注释行与空行，遇 `[DONE]` 才正常结束；
    3) 上游错误必须转成带类型的异常，上层才能区分「超时 504」还是「上游 502」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx


class LLMStreamError(RuntimeError):
    """
    LLM 调用异常。

    kind 用于上层映射错误码：
        "timeout"  -> 504 LLM_TIMEOUT
        "upstream" -> 502 LLM_UPSTREAM_ERROR
    """

    def __init__(self, message: str, *, kind: str = "upstream"):
        super().__init__(message)
        self.kind = kind


class StreamLLM:
    """OpenAI 兼容协议的流式对话客户端。"""

    def __init__(self, settings: Any) -> None:
        # settings 是阶段 1 的 Settings 实例（鸭子类型，不 import 它，避免循环依赖）
        self.s = settings
        # 读超时给 60s：一是不让"模型卡住"无限占着连接，二是留出长回答的时间。
        # 注意这是**单次读**的超时，不是整体超时；整体超时由路由层控制。
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=60.0, write=15.0, pool=10.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    @property
    def model_name(self) -> str:
        """落库用的模型标识，便于事后判断"这条回答是哪版模型生成的"。"""
        return getattr(self.s, "chat_model", "") or "unknown"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def stream_chat(
        self,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """
        流式对话，逐个文本片段 yield。

        这里刻意不做「攒够一句再返回」：SSE 的价值就是首字尽快出现在屏幕上，
        攒句子等于把首字延迟从 1 秒变成 5 秒，用户体验反而更差。
        """
        url = f"{self.s.chat_base.rstrip('/')}/chat/completions"
        payload = {
            "model": self.s.chat_model,
            "messages": messages,
            # 制度问答必须低温：温度一高，模型就开始"合理推测"金额和时段
            "temperature": self.s.temperature if temperature is None else temperature,
            "max_tokens": self.s.max_tokens if max_tokens is None else max_tokens,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {self.s.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with self._http.stream("POST", url, json=payload, headers=headers) as resp:
                # 上游用非 2xx 回应时，body 里通常有可读的原因，取前 300 字进日志
                if resp.status_code >= 400:
                    raw = await resp.aread()
                    raise LLMStreamError(
                        f"LLM 返回 {resp.status_code}：{raw.decode('utf-8', 'ignore')[:300]}"
                    )

                async for line in resp.aiter_lines():
                    line = line.strip()
                    # 空行是 SSE 的分隔符；冒号开头是注释（部分网关会插 keep-alive）
                    if not line or line.startswith(":"):
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        # 极少见（网关截断），跳过而不是整条流失败
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        # 有些供应商会在流里插 usage 帧，没有 choices，直接略过
                        continue
                    delta = choices[0].get("delta") or {}
                    text = delta.get("content")
                    if text:
                        yield text
        except httpx.TimeoutException as e:
            raise LLMStreamError(f"等待大模型响应超时：{e}", kind="timeout") from e
        except httpx.HTTPError as e:
            raise LLMStreamError(f"请求大模型失败：{e}", kind="upstream") from e

    # =========================================================================
    # Function Calling（智能体编排层用）
    # -------------------------------------------------------------------------
    # 为什么必须是**非流式**：
    #   决策这一步需要拿到完整的 tool_calls 数组（工具名 + 完整的 JSON 参数），
    #   才能知道要调什么。流式下 tool_calls 是按 fragment 增量下发的，
    #   要自己拼 name、拼 arguments 字符串、判断何时结束——能写对，但没必要：
    #   决策阶段本来就不需要把人等的那几百毫秒省下来（屏幕上显示的是"正在分析"）。
    #   最终回答也是用这一轮返回的 content，所以整个循环只有一种调用形态。
    # =========================================================================

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
    ) -> "ChatResult":
        """
        一次非流式对话，可能返回文本，也可能返回若干工具调用。

        tools 为空时退化为普通问答，与 stream_chat 的语义一致（只是不流式）。
        """
        url = f"{self.s.chat_base.rstrip('/')}/chat/completions"
        payload: dict[str, Any] = {
            "model": self.s.chat_model,
            "messages": messages,
            "temperature": self.s.temperature if temperature is None else temperature,
            "max_tokens": self.s.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            # tool_choice 默认 auto：让模型自己决定"先查还是直接答"。
            # 不做 "required" 强制——制度类闲聊（"你好"）不该被迫调工具。
            payload["tool_choice"] = tool_choice or "auto"

        headers = {
            "Authorization": f"Bearer {self.s.api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = await self._http.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise LLMStreamError(f"等待大模型响应超时：{e}", kind="timeout") from e
        except httpx.HTTPError as e:
            raise LLMStreamError(f"请求大模型失败：{e}", kind="upstream") from e

        if resp.status_code >= 400:
            raise LLMStreamError(f"LLM 返回 {resp.status_code}：{resp.text[:300]}")

        try:
            obj = resp.json()
        except ValueError as e:
            raise LLMStreamError(f"LLM 返回非 JSON：{resp.text[:200]}") from e

        choices = obj.get("choices") or []
        if not choices:
            raise LLMStreamError("LLM 返回空 choices")

        message = choices[0].get("message") or {}
        raw_calls = message.get("tool_calls") or []

        calls: list[ToolCall] = []
        for rc in raw_calls:
            fn = rc.get("function") or {}
            name = fn.get("name") or ""
            args_raw = fn.get("arguments") or ""
            args, parse_error = _parse_args(args_raw)
            calls.append(
                ToolCall(
                    id=rc.get("id") or f"call_{len(calls)}",
                    name=name,
                    args_raw=args_raw,
                    args=args,
                    parse_error=parse_error,
                )
            )

        return ChatResult(
            content=(message.get("content") or "").strip(),
            tool_calls=calls,
            finish_reason=choices[0].get("finish_reason") or "",
            usage=obj.get("usage") or {},
        )


def _parse_args(raw: str) -> tuple[dict[str, Any] | None, str | None]:
    """
    解析模型给的工具参数。

    这行代码看起来多余，但它处理的是**真实高频故障**：
    模型（尤其是小参数量的）经常把 arguments 写成
        {"date": "2026-10-31",}      ← 尾逗号
        {'date': '2026-10-31'}       ← 单引号
        ""                            ← 空字符串（无参工具）
    直接 json.loads 会抛异常，整轮对话就崩了。这里降级成"返回 None + 错误原因"，
    由调用方把这个错误当成工具执行失败回填给模型，让它自己改——通常第二次就对了。
    这比直接 500 好得多，也让"模型参数错误"这条在日志里可统计。
    """
    text = (raw or "").strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"参数不是合法 JSON：{e}"
    if not isinstance(parsed, dict):
        return None, f"参数必须是对象，实际是 {type(parsed).__name__}"
    return parsed, None


@dataclass
class ToolCall:
    """模型请求的一次工具调用。args 为 None 时 parse_error 一定有值。"""

    id: str
    name: str
    args_raw: str
    args: dict[str, Any] | None
    parse_error: str | None = None


@dataclass
class ChatResult:
    """一次非流式对话的结果。"""

    content: str
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: dict[str, Any]

    @property
    def wants_tools(self) -> bool:
        """模型是要求调工具，还是想直接说话。这是循环是否继续的唯一判据。"""
        return bool(self.tool_calls)

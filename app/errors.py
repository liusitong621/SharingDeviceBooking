"""
统一错误码与异常处理。

设计约束（对应接口设计第三、四节）：
    HTTP 4xx 只能出现在「开流之前」。一旦返回 200 并开始推送 SSE 事件流，
    状态码就再也改不回来了，所以所有可预见的校验都必须在 StreamingResponse 之前完成。
    流内异常统一走 event: error，用同一份 code 表，只是外层从 HTTP 状态码变成事件。

三条规则：
    1) 每一条错误都必须有中文 message，能直接展示给用户；
    2) detail 只放给开发者排查用的信息（异常原文、字段名），绝不出现在前端 UI；
    3) 不把堆栈返回给客户端，堆栈只进服务端日志。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("app.errors")

# =============================================================================
# 错误码表：code -> (HTTP 状态码, 中文提示)
# -----------------------------------------------------------------------------
# 这张表是前后端唯一的契约。前端只认 code，展示 message。
# 改文案不用改前端，新增 code 必须同时在这里登记，否则会退化成 500。
# =============================================================================
ERROR_TABLE: dict[str, tuple[int, str]] = {
    # --- 入参类（都能在开流前拦下）---
    "INVALID_REQUEST": (400, "请求参数不合法，请检查后重试"),
    "QUESTION_EMPTY": (400, "问题不能为空"),
    "QUESTION_TOO_LONG": (400, "问题过长，请控制在 500 字以内"),
    "INVALID_SESSION_ID": (400, "会话标识格式不正确"),
    "INVALID_HISTORY_TURNS": (422, "历史轮数参数超出范围"),
    # --- 会话类 ---
    "SESSION_FORBIDDEN": (403, "无权访问该会话，请重新开始对话"),
    "SESSION_NOT_FOUND": (404, "会话不存在，请刷新页面重新开始"),
    "SESSION_BUSY": (409, "上一条提问还在处理中，请稍候再试"),
    # --- 认证与权限（批次 1）---
    "UNAUTHORIZED": (401, "请先登录"),
    "FORBIDDEN": (403, "没有权限执行该操作"),
    "INVALID_CREDENTIALS": (401, "工号或密码不正确"),
    "ACCOUNT_LOCKED": (423, "账号已被锁定，请稍后再试"),
    "ACCOUNT_DISABLED": (403, "账号已停用，请联系管理员"),
    "WEAK_PASSWORD": (400, "新密码不符合要求"),
    "CSRF_FAILED": (403, "页面已过期，请刷新后重试"),
    # --- 预约（预约 REST 接口）---
    "INVALID_SLOT": (400, "时段不合法"),
    "DATE_OUT_OF_RANGE": (400, "只能预约未来 14 天内的时段"),
    "DATE_IN_PAST": (400, "不能预约已经过去的时段"),
    "DEVICE_NOT_FOUND": (404, "设备不存在"),
    "DEVICE_OFFLINE": (409, "该设备已下架，暂不可预约"),
    "SLOT_TAKEN": (409, "该时段已被预约，请换一个时段"),
    "QUOTA_EXCEEDED": (409, "已达到单日预约上限（2 格）"),
    "BOOKING_NOT_FOUND": (404, "预约不存在"),
    "BOOKING_FORBIDDEN": (403, "只能操作自己的预约"),
    "BOOKING_STATE_INVALID": (409, "当前状态下不能执行该操作"),
    # --- 智能体校验凭证（T5 → T6）---
    "VALIDATION_REQUIRED": (400, "请先校验时段再发起预约"),
    "VALIDATION_NOT_FOUND": (404, "校验凭证不存在，请重新校验时段"),
    "VALIDATION_EXPIRED": (409, "校验结果已过期，请重新校验"),
    "VALIDATION_CONSUMED": (409, "该校验已被使用，请重新校验"),
    "VALIDATION_MISMATCH": (409, "校验内容与提交内容不一致"),
    # --- 智能体待确认动作（human-in-the-loop）---
    "PENDING_NOT_FOUND": (404, "这条待确认请求不存在或已被清理"),
    "PENDING_FORBIDDEN": (403, "只能确认自己的预约请求"),
    "PENDING_ALREADY_DECIDED": (409, "这条请求已经处理过了，请勿重复提交"),
    "PENDING_EXPIRED": (410, "确认已超时，请重新发起预约"),
    # --- 智能体循环内部（一般只进日志，不直接暴露给用户）---
    "INVALID_ARGUMENT": (400, "工具参数不正确"),
    "UNKNOWN_TOOL": (400, "调用了不存在的工具"),
    "TOOL_FAILED": (500, "工具执行失败"),
    "MAX_ROUNDS_EXCEEDED": (504, "分析轮次超限，请把问题描述得更具体一些"),
    # --- 资源与上游 ---
    "RATE_LIMITED": (429, "提问太频繁了，请稍后再试"),
    "RETRIEVAL_UNAVAILABLE": (503, "检索服务暂不可用，请稍后再试"),
    "LLM_TIMEOUT": (504, "大模型响应超时，请稍后重试"),
    "LLM_UPSTREAM_ERROR": (502, "大模型服务异常，请稍后重试"),
    "INTERNAL_ERROR": (500, "服务内部错误，请稍后重试"),
}

DEFAULT_MESSAGE = "服务内部错误，请稍后重试"


class ApiError(Exception):
    """业务异常。抛出它，由全局处理器统一转成 {code, message, trace_id, detail}。

    message 可覆盖表里的默认文案：有些错误需要带上具体数字（"请 7 分钟后重试"），
    这类文案本身就是给用户看的，不能塞进 detail（detail 只进日志）。
    """

    def __init__(self, code: str, detail: str | None = None, message: str | None = None):
        self.code = code
        self.status, default_message = ERROR_TABLE.get(code, (500, DEFAULT_MESSAGE))
        self.message = message or default_message
        self.detail = detail
        super().__init__(f"{code}: {self.message}")


def new_trace_id() -> str:
    """链路 ID：日志、错误体、SSE meta 事件三处用同一个值，方便对账。"""
    return uuid.uuid4().hex[:16]


def trace_id_of(request: Request) -> str:
    """取本次请求的 trace_id；没有就用请求头 X-Request-Id，再没有就现生成。"""
    tid = getattr(request.state, "trace_id", None)
    if tid:
        return tid
    return request.headers.get("X-Request-Id") or new_trace_id()


def error_body(code: str, trace_id: str, detail: str | None = None, message: str | None = None) -> dict:
    """
    统一错误体结构。detail 允许为空，前端不渲染它。

    message 允许覆盖表里的默认文案——否则 ApiError 里那些带具体数字的文案
    （"请 7 分钟后重试"、"时段 10:00 已经开始"）会被这里悄悄丢掉，
    用户永远只看到笼统的默认提示。这是一个很容易漏的分层细节。
    """
    _, default_message = ERROR_TABLE.get(code, (500, DEFAULT_MESSAGE))
    return {"code": code, "message": message or default_message, "trace_id": trace_id, "detail": detail}


def install_error_handlers(app: FastAPI) -> None:
    """注册三类异常处理器：业务异常、参数校验异常、兜底异常。"""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        tid = trace_id_of(request)
        # 4xx 属于正常业务分支，用 info；5xx 才值得报警
        log = logger.warning if exc.status < 500 else logger.error
        log("[%s] %s %s -> %s %s", tid, request.method, request.url.path, exc.code, exc.detail or "")
        return JSONResponse(
            status_code=exc.status,
            content=error_body(exc.code, tid, exc.detail, message=exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 注意：不把 exc.errors() 原样返回，里面含内部字段路径与类型信息
        tid = trace_id_of(request)
        logger.warning("[%s] 参数校验失败：%s", tid, exc.errors())
        return JSONResponse(
            status_code=400,
            content=error_body("INVALID_REQUEST", tid, detail="字段类型或必填项不符合要求"),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 兜底：堆栈只进日志，客户端只拿到一个 code
        tid = trace_id_of(request)
        logger.exception("[%s] 未捕获异常 %s %s", tid, request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_body("INTERNAL_ERROR", tid, detail=type(exc).__name__),
        )

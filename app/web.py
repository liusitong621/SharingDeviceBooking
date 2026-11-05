"""
服务端渲染的页面路由：登录、改密、后台骨架。

三个决定：

1) **页面与 API 共用同一套认证依赖。**
   页面未登录 → 302 到 /login；API 未登录 → 401 JSON。
   两者的区别只在"失败时给什么"，不在"怎么判"。

2) **表单 POST + 302 + 一次性 flash，不用前端框架。**
   后台只有 4 个页面，引入 React/Vue 会让部署多一个构建步骤，
   而收益为零。Jinja2 直出在千人内网场景够用，且天然对 SEO/打印友好。

3) **错误页与登录页单独渲染，不继承后台布局。**
   未登录时如果套用带导航的布局，会出现"没登录也能看到管理菜单"的错觉，
   即使点了打不开，也是糟糕的心理暗示。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth, booking_service, db, rag
from app.agent import prompts
from app.agent.tools import build_today_hint
from app.auth import current_user
from app.domain import slots as slot_rules
from app.errors import ApiError

logger = logging.getLogger("app.web")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = PROJECT_ROOT / "templates"

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# =============================================================================
# 渲染工具
# =============================================================================


def render(request: Request, name: str, ctx: dict[str, Any] | None = None, status_code: int = 200) -> HTMLResponse:
    """统一注入 csrf、当前用户、当前路径，避免每个页面重复传。"""
    data: dict[str, Any] = {
        "request": request,
        "csrf": auth.csrf_token_for(request),
        "current_path": request.url.path,
    }
    if ctx:
        data.update(ctx)
    resp = templates.TemplateResponse(request, name, data, status_code=status_code)
    # CSRF cookie 必须在首次访问时就下发，否则第一次提交表单必然失败
    if not request.cookies.get(auth.CSRF_COOKIE):
        resp.set_cookie(auth.CSRF_COOKIE, data["csrf"], samesite="lax", max_age=auth.TOKEN_TTL_HOURS * 3600)
    return resp


def login_redirect(request: Request) -> RedirectResponse:
    """
    未登录跳登录页，并把来路带过去，登录后回到原页面。

    必须把 **query 也带上**：前端支持 `/?session=<id>` 直接打开指定会话，
    只带 path 的话，登录回来后 session 参数就丢了，用户会看到一个新会话，
    而他点的那条链接看起来"没反应"。这个 bug 只在带参数的链接上出现，
    平时点导航点不出来。
    """
    full = request.url.path
    if request.url.query:
        full = f"{full}?{request.url.query}"
    nxt = quote(full, safe="/?=&-_.~")
    # 两端各有自己的登录页。从 /admin/** 被踢出来的人要回到管理端登录，
    # 否则会遇到"在员工端登录成功、进门还是 403"的循环。
    entry = "/admin/login" if request.url.path.startswith("/admin") else "/login"
    return RedirectResponse(f"{entry}?next={nxt}", status_code=302)


async def page_user(request: Request) -> dict[str, Any] | None:
    return await auth.user_from_request(request)


async def guard(request: Request) -> tuple[dict[str, Any] | None, RedirectResponse | None]:
    """
    页面级守卫，返回 (user, 需要跳转的响应)。

    强制改密必须放在这里，而不是只放在登录成功后的那一跳：
    用户完全可以手输 /admin 绕过跳转——"只在登录时检查"等于没检查。
    这条规则是**所有**需登录页面的公共前置条件，所以只能有一个入口。
    """
    user = await page_user(request)
    if user is None:
        return None, login_redirect(request)
    if user.get("must_change_pwd") and request.url.path != "/change-password":
        return user, RedirectResponse("/change-password", status_code=302)
    return user, None


async def require_admin(request: Request) -> tuple[dict[str, Any] | None, Any | None]:
    """
    管理员页面的公共前置：先过登录/改密，再判角色。

    抽出来的理由很实际：管理页从 2 个涨到 6 个之后，这段判断会在每个函数里
    重复一遍，而每重复一次就多一次"某天新增页面忘了判角色"的机会。
    权限检查最怕的不是写错，是漏写。
    """
    user, redirect = await guard(request)
    if redirect:
        return None, redirect
    if user.get("role") != "admin":
        return None, render(
            request, "error.html",
            {"user": user, "code": 403, "message": "这个页面只有管理员可以访问"},
            403,
        )
    return user, None


def _ip(request: Request) -> str | None:
    """客户端 IP，仅用于操作留痕。不采信 X-Forwarded-For，理由见 booking_api._ip。"""
    return request.client.host if request.client else None


# =============================================================================
# 登录 / 登出 / 改密
# =============================================================================


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/") -> HTMLResponse:
    user = await page_user(request)
    if user:
        return RedirectResponse(_home_for(user), status_code=302)
    return render(request, "login.html", {"next": next, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    emp_no: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
) -> HTMLResponse:
    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "login.html", {"next": next, "error": "页面已过期，请刷新后重试"}, 403)

    emp_no = emp_no.strip()
    if not emp_no or not password:
        return render(request, "login.html", {"next": next, "error": "请输入工号和密码"}, 400)

    try:
        user = await auth.authenticate(emp_no, password)
    except ApiError as exc:
        # 这里直接展示 exc.message：登录页是唯一允许把认证失败原因说清楚的地方，
        # 但"工号不存在"和"密码错误"仍然共用同一句文案（防工号枚举）。
        return render(request, "login.html", {"next": next, "error": exc.message}, exc.status)

    if user.get("must_change_pwd"):
        target = "/change-password"
    else:
        target = next if next.startswith("/") and not next.startswith("//") else _home_for(user)

    resp = RedirectResponse(target, status_code=302)
    auth.attach_session(resp, user, request)
    logger.info("登录成功：%s (%s)", user["emp_no"], user["role"])
    return resp


@router.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request, next: str = "/admin") -> HTMLResponse:
    """
    管理端登录页。与员工端刻意长得不一样——两个入口混用同一张表单时，
    员工会从后台入口登进去、看到 403，然后以为是"系统坏了"。

    已登录但角色不是管理员时不自动跳走：让他看见"当前登录的是员工账号"，
    比默默把他弹回员工端更清楚。这里**不销毁**现有会话，由他决定换号还是回去。
    """
    user = await page_user(request)
    if user and user.get("role") == "admin":
        return RedirectResponse(_safe_next(next, "/admin"), status_code=302)
    return render(
        request, "admin_login.html",
        {"next": next, "error": None, "signed_in_as": user.get("emp_no") if user else None},
    )


@router.post("/admin/login", response_class=HTMLResponse)
async def admin_login_submit(
    request: Request,
    emp_no: str = Form(""),
    password: str = Form(""),
    next: str = Form("/admin"),
) -> HTMLResponse:
    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "admin_login.html",
                      {"next": next, "error": "页面已过期，请刷新后重试", "signed_in_as": None}, 403)

    emp_no = emp_no.strip()
    if not emp_no or not password:
        return render(request, "admin_login.html",
                      {"next": next, "error": "请输入工号和密码", "signed_in_as": None}, 400)

    try:
        user = await auth.authenticate(emp_no, password)
    except ApiError as exc:
        return render(request, "admin_login.html",
                      {"next": next, "error": exc.message, "signed_in_as": None}, exc.status)

    # 关键：角色不对就不下发会话。只在页面上提示"员工请走员工端"是不够的，
    # 那等于让他先拿到一个能用的登录态。
    if user.get("role") != "admin":
        logger.info("管理端登录被拒（非管理员）：%s", user["emp_no"])
        return render(
            request, "admin_login.html",
            {"next": next, "signed_in_as": None,
             "error": "这个入口只给管理员。员工请使用员工端登录页。"},
            403,
        )

    target = "/change-password" if user.get("must_change_pwd") else _safe_next(next, "/admin")
    resp = RedirectResponse(target, status_code=302)
    auth.attach_session(resp, user, request)
    logger.info("管理端登录成功：%s (%s)", user["emp_no"], user["role"])
    return resp


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    """
    退出后回到"他刚才所在那一端的登录页"。

    用 Referer 判断而不是角色：管理员在员工端看效果时点退出，
    应该回员工端登录，而不是被扔到管理端登录页。
    """
    user = await page_user(request)
    referer = request.headers.get("referer", "")
    on_admin = referer.split("://", 1)[-1].partition("/")[2].startswith("admin")
    target = "/admin/login" if (on_admin and user and user.get("role") == "admin") else "/login"

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return RedirectResponse(target, status_code=302)
    resp = RedirectResponse(target, status_code=302)
    auth.clear_session(resp)
    return resp


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request) -> HTMLResponse:
    user = await page_user(request)
    if user is None:
        return login_redirect(request)
    return render(request, "change_password.html", {"user": user, "error": None, "forced": user.get("must_change_pwd")})


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_submit(
    request: Request,
    old_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
) -> HTMLResponse:
    user = await page_user(request)
    if user is None:
        return login_redirect(request)

    form = await request.form()
    ctx = {"user": user, "forced": user.get("must_change_pwd")}
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "change_password.html", {**ctx, "error": "页面已过期，请刷新后重试"}, 403)

    if not auth.verify_password(old_password, user["pwd_hash"]):
        return render(request, "change_password.html", {**ctx, "error": "当前密码不正确"}, 400)
    if new_password != confirm_password:
        return render(request, "change_password.html", {**ctx, "error": "两次输入的新密码不一致"}, 400)
    if new_password == old_password:
        return render(request, "change_password.html", {**ctx, "error": "新密码不能与当前密码相同"}, 400)

    weak = auth.validate_password_strength(new_password)
    if weak:
        return render(request, "change_password.html", {**ctx, "error": weak}, 400)

    await db.update_password(user["id"], auth.hash_password(new_password), must_change=False)
    logger.info("用户 %s 已修改密码", user["emp_no"])
    resp = RedirectResponse(_home_for(user), status_code=302)
    return resp


def _home_for(user: dict[str, Any]) -> str:
    return "/admin" if user.get("role") == "admin" else "/"


def _safe_next(nxt: str, fallback: str) -> str:
    """
    登录后落点校验：只接受站内绝对路径。

    挡的是 `next=//evil.com` 这种开放重定向——浏览器会把 `//host` 当成
    协议相对 URL 直接跳走，登录页正好是最容易被塞参数的地方。
    """
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else fallback


# =============================================================================
# 当前用户（前端渲染侧栏用）
# =============================================================================


@router.get("/api/v1/auth/me")
async def auth_me(user: dict = Depends(current_user)) -> dict:
    """
    返回当前登录用户。前端拿它渲染侧栏、决定是否显示"管理后台"入口。

    这里返回的 role 只用于**界面装饰**。真正的权限判定在服务端每一次请求上，
    前端隐藏一个按钮从来不算权限控制。
    """
    return {
        "id": user["id"],
        "emp_no": user["emp_no"],
        "name": user["name"],
        "dept": user.get("dept"),
        "role": user["role"],
        "must_change_pwd": bool(user.get("must_change_pwd")),
    }


# =============================================================================
# 后台
# =============================================================================


@router.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request) -> HTMLResponse:
    user, deny = await require_admin(request)
    if deny:
        return deny

    today = booking_service.now().date().isoformat()
    st = await db.stats(today, booking_service.now().strftime("%Y-%m-%dT%H:%M"))
    return render(request, "admin_dashboard.html", {"user": user, "stats": st, "today": today})


@router.get("/admin/devices", response_class=HTMLResponse)
async def admin_devices(request: Request, edit: str | None = None) -> HTMLResponse:
    """
    设备台账。

    edit 走 query 参数而不是行内展开：表格里塞一个 <form> 会把布局撑坏，
    而引前端框架只为了一个编辑框，代价远大于收益。
    `/admin/devices?edit=<id>` 既能让管理员直接收藏某一台的编辑链接，
    也天然支持"改完 302 回来"的流程。
    """
    user, deny = await require_admin(request)
    if deny:
        return deny

    target = await db.fetch_device(edit) if edit else None
    return await _devices_page(request, user, edit_device=target)


@router.post("/admin/devices/{device_id}/status")
async def admin_set_device_status(request: Request, device_id: str, status: str = Form(...)):
    """
    上下架。用表单提交而不是 fetch，是为了在没有 JS 的环境下也能操作——
    管理员在会议室的旧电脑上遇到设备坏了，不该因为脚本没加载而无法下架。
    """
    user, redirect = await guard(request)
    if redirect:
        return redirect
    if user.get("role") != "admin":
        return render(request, "error.html", {"user": user, "code": 403, "message": "这个页面只有管理员可以访问"}, 403)

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    if status not in ("available", "offline"):
        raise ApiError("INVALID_REQUEST", "只支持 available / offline")

    device = await db.fetch_device(device_id)
    if device is None:
        raise ApiError("DEVICE_NOT_FOUND")
    await db.set_device_status(device_id, status)
    logger.info("管理员 %s 将设备 %s 置为 %s", user["emp_no"], device["asset_no"], status)
    return RedirectResponse("/admin/devices", status_code=302)


# =============================================================================
# 员工侧页面
# =============================================================================


@router.get("/bookings", response_class=HTMLResponse)
async def my_bookings_page(request: Request) -> HTMLResponse:
    """
    我的预约（表格页，不做 JSON）。

    这是"取消"和"取还登记"的唯一入口：这两类操作必须有明确的用户点击，
    不能由模型代劳。所以它们只有 HTML 表单，没有对应的工具。
    """
    user, redirect = await guard(request)
    if redirect:
        return redirect
    rows = await db.list_user_bookings(user["id"])
    now_dt = booking_service.now()
    return render(
        request,
        "my_bookings.html",
        {
            "user": user,
            "bookings": rows,
            "now_date": now_dt.date().isoformat(),
            # datetime-local 的默认值：让"现在归还"成为零输入操作
            "now_input": now_dt.strftime("%Y-%m-%dT%H:%M"),
        },
    )


@router.post("/bookings/{booking_id}/{action}")
async def booking_action(request: Request, booking_id: str, action: str):
    """cancel / pickup / return 三个动作共用一条路由，行为由 action 决定。"""
    user, redirect = await guard(request)
    if redirect:
        return redirect

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    try:
        if action == "cancel":
            await booking_service.cancel(user, booking_id, ip=_ip(request))
        elif action == "pickup":
            await booking_service.pickup(user, booking_id, ip=_ip(request))
        elif action == "return":
            # 归还的两个字段都可空：只点一下"已归还"也必须能提交成功
            remark = form.get("remark")
            returned_at = form.get("returned_at")
            await booking_service.give_back(
                user,
                booking_id,
                remark if isinstance(remark, str) else None,
                returned_at if isinstance(returned_at, str) else None,
                ip=_ip(request),
            )
        else:
            raise ApiError("INVALID_REQUEST", f"未知动作 {action}")
    except ApiError as exc:
        return render(
            request,
            "error.html",
            {"user": user, "code": exc.status, "message": exc.message},
            exc.status,
        )
    return RedirectResponse("/bookings", status_code=302)


# =============================================================================
# R06 超期与未取
# -----------------------------------------------------------------------------
# 这一页存在的理由写在需求清单风险第 2 条里：
# "约满但没人用"不会自动消失，只能靠这两个清单 + 管理员手动释放。
# 也就是说，这一页不是"锦上添花的功能"，它是 A 方案（无审批、无信用）的兜底。
# =============================================================================


async def _overdue_page(request: Request, user: dict, error: str | None = None) -> HTMLResponse:
    now_dt = booking_service.now()
    data = await db.list_overdue(now_dt.strftime("%Y-%m-%dT%H:%M"))
    return render(
        request,
        "admin_overdue.html",
        {
            "user": user,
            "lists": data,
            "now_text": now_dt.strftime("%Y-%m-%d %H:%M"),
            "error": error,
        },
    )


@router.get("/admin/overdue", response_class=HTMLResponse)
async def admin_overdue(request: Request) -> HTMLResponse:
    user, deny = await require_admin(request)
    if deny:
        return deny
    return await _overdue_page(request, user)


# =============================================================================
# 员工侧占用视图（R02）
# -----------------------------------------------------------------------------
# 为什么在已经有聊天助手的情况下还要这一页：
#   助手擅长"我明天想借频谱仪，有空的吗"这种模糊问法；
#   而 R02 要的是"按日期 × 设备看 6 格状态"的确定性视图——
#   一眼扫完三台设备哪格空着，比跟模型来回三轮快得多。
#   两者不是替代关系：助手负责找，这一页负责看和点。
#
# 为什么下单用表单 POST 而不是 fetch：
#   与后台保持一致——按钮能不能点由服务端状态决定，不依赖脚本是否加载成功。
# =============================================================================


def _slot_error_text(code: str | None) -> str | None:
    """
    把 URL 上的错误码翻成一句人话。

    刻意不接受任意错误文案：URL 里的文字会被渲染进页面，
    虽然 Jinja 会转义，但"只允许白名单里的码"比"信任转义"更省心。
    """
    if not code:
        return None
    return {
        "SLOT_TAKEN": "这格刚被别人约走了，换一格试试。",
        "QUOTA_EXCEEDED": "你当天已经约满 2 格，先取消一格再约。",
        "DEVICE_OFFLINE": "这台设备已下架，暂时不能约。",
        "DATE_IN_PAST": "这格已经开始或已过期，不能再约。",
        "DATE_OUT_OF_RANGE": "只能预约 14 天内的时段。",
        "INVALID_SLOT": "时段不合法，刷新页面重试。",
        "DEVICE_NOT_FOUND": "设备不存在，可能刚被删除。",
        "INVALID_RETURN_TIME": "归还时间不合法。",
    }.get(code, "操作没有成功，请刷新后重试。")


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request, date: str | None = None) -> HTMLResponse:
    user, redirect = await guard(request)
    if redirect:
        return redirect

    now_dt = booking_service.now()
    day = date or now_dt.date().isoformat()
    if slot_rules.parse_date(day) is None:
        day = now_dt.date().isoformat()

    data = await booking_service.list_devices_with_availability(day, user["id"])
    return render(
        request,
        "devices.html",
        {
            "user": user,
            "devices": data["devices"],
            "date": day,
            "today": now_dt.date().isoformat(),
            "days": slot_rules.next_days(now_dt, 14),
            "error": _slot_error_text(request.query_params.get("err")),
            "notice": "预约成功。" if request.query_params.get("ok") else None,
        },
    )


@router.post("/bookings/new")
async def booking_create_form(request: Request):
    """
    占用视图上点空格直接下单。

    这是**人点出来的**预约，与智能体那条"模型发起 → 人确认"的路径最终都落到
    place_booking 同一个写入口，所以配额、冲突、日期规则一份都不会分叉。
    """
    user, redirect = await guard(request)
    if redirect:
        return redirect

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    device_id = form.get("device_id")
    slot_date = form.get("slot_date")
    slot_start = form.get("slot_start")
    back = form.get("back")
    target = back if isinstance(back, str) and back.startswith("/devices") else "/devices"

    if not all(isinstance(v, str) and v for v in (device_id, slot_date, slot_start)):
        raise ApiError("INVALID_REQUEST", "缺少设备、日期或时段")

    try:
        await booking_service.place_booking(
            user["id"], device_id, slot_date, [slot_start],
            actor=user, ip=_ip(request),
        )
    except ApiError as exc:
        # 回到原页面而不是错误页：用户正在挑格子，跳走就得重新选日期
        sep = "&" if "?" in target else "?"
        return RedirectResponse(f"{target}{sep}err={exc.code}", status_code=302)

    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}ok=1", status_code=302)


@router.post("/admin/bookings/{booking_id}/force-close")
async def admin_force_close(request: Request, booking_id: str):
    """
    强制结束。POST 完立刻 302 回列表，而不是渲染一个"操作成功"页——
    管理员会连续处理好几条，每处理一条就多一次点击是纯粹的浪费。
    """
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    reason = form.get("reason")
    try:
        result = await booking_service.force_close(
            user, booking_id, reason if isinstance(reason, str) else "", ip=_ip(request)
        )
    except ApiError as exc:
        # 用页内提示而不是错误页：管理员刚填了处置说明，跳走就白填了
        return await _overdue_page(request, user, error=exc.message)

    logger.info(
        "管理员 %s 强制结束预约 %s：%s -> %s（%s）",
        user["emp_no"], booking_id, result["from"], result["to"], reason or "无说明",
    )
    return RedirectResponse("/admin/overdue", status_code=302)


# =============================================================================
# R01 设备台账增删改
# -----------------------------------------------------------------------------
# 上下架已经存在；这里补的是"台账本身能维护"。
# 没有这三个入口之前，新买一台设备只能手工往 SQLite 里插一行。
# =============================================================================


async def _devices_page(
    request: Request,
    user: dict,
    error: str | None = None,
    form: dict[str, str] | None = None,
    edit_device: dict[str, Any] | None = None,
) -> HTMLResponse:
    day = booking_service.now().date().isoformat()
    data = await booking_service.list_devices_with_availability(day)
    return render(
        request,
        "admin_devices.html",
        {
            "user": user,
            "devices": data["devices"],
            "today": day,
            "error": error,
            "form": form or {},
            "edit_device": edit_device,
        },
    )


def _clean(value: Any, limit: int = 60) -> str:
    return (value.strip() if isinstance(value, str) else "")[:limit]


@router.post("/admin/devices")
async def admin_create_device(request: Request):
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    asset_no = _clean(form.get("asset_no"), 40)
    name = _clean(form.get("name"), 60)
    model = _clean(form.get("model"), 80) or None
    location = _clean(form.get("location"), 80) or None
    note = _clean(form.get("note"), 200) or None

    filled = {"asset_no": asset_no, "name": name, "model": model or "", "location": location or "", "note": note or ""}
    if not asset_no or not name:
        return await _devices_page(request, user, "资产编号和设备名称都必须填", filled)

    try:
        await db.create_device(asset_no, name, model, location, note, actor=user, ip=_ip(request))
    except ApiError as exc:
        return await _devices_page(request, user, exc.message, filled)

    logger.info("管理员 %s 新增设备 %s %s", user["emp_no"], asset_no, name)
    return RedirectResponse("/admin/devices", status_code=302)


@router.post("/admin/devices/{device_id}/edit")
async def admin_edit_device(request: Request, device_id: str):
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    asset_no = _clean(form.get("asset_no"), 40)
    name = _clean(form.get("name"), 60)
    model = _clean(form.get("model"), 80) or None
    location = _clean(form.get("location"), 80) or None
    note = _clean(form.get("note"), 200) or None
    status = _clean(form.get("status"), 20) or "available"

    if not asset_no or not name:
        return await _devices_page(request, user, "资产编号和设备名称都必须填")
    if status not in ("available", "borrowed", "offline"):
        return await _devices_page(request, user, "设备状态只能是 available / borrowed / offline")

    try:
        await db.update_device(
            device_id, asset_no=asset_no, name=name, model=model,
            location=location, status=status, note=note,
            actor=user, ip=_ip(request),
        )
    except ApiError as exc:
        return await _devices_page(request, user, exc.message)

    logger.info("管理员 %s 编辑设备 %s", user["emp_no"], asset_no)
    return RedirectResponse("/admin/devices", status_code=302)


@router.post("/admin/devices/{device_id}/delete")
async def admin_delete_device(request: Request, device_id: str):
    """
    删除（软删除）。

    这里**不做二次确认弹窗**，而是靠"有未完成预约就删不掉"这条硬约束——
    弹窗挡不住误点，约束才挡得住。前端仍然用 confirm()，那是给手滑用的。
    """
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    try:
        await db.delete_device(device_id, actor=user, ip=_ip(request))
    except ApiError as exc:
        return await _devices_page(request, user, exc.message)

    logger.info("管理员 %s 删除设备 %s", user["emp_no"], device_id)
    return RedirectResponse("/admin/devices", status_code=302)


# =============================================================================
# R08 账号管理
# -----------------------------------------------------------------------------
# 批量导入刻意用"粘贴文本"而不是上传 Excel/CSV 文件：
# 上传要多一个文件解析依赖（openpyxl/python-multipart 已在，但编码问题永远有），
# 而 HR 给的名单本来就三四十行，从 Excel 复制过来粘贴是最短路径。
# =============================================================================

# 导入时没写初始密码就用这个，与 seed.py 的演示账号保持一致，便于培训时统一口径
DEFAULT_IMPORT_PASSWORD = "Staff@12345"
MAX_IMPORT_ROWS = 500


def _parse_import(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """
    解析粘贴的名单。返回 (有效行, 错误说明)。

    支持逗号（中英文）、制表符分隔——从 Excel 复制过来是制表符，
    从聊天窗口复制过来是逗号，两种都得认，否则用户会以为功能坏了。

    首行如果是表头（含"工号"）直接跳过：没人会读说明，但每个人都会粘表头。
    """
    rows: list[dict[str, str]] = []
    errors: list[str] = []

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"[,，\t]+", line)]
        if line_no == 1 and parts and "工号" in parts[0]:
            continue
        if len(parts) < 2:
            errors.append(f"第 {line_no} 行：至少需要「工号,姓名」两列")
            continue

        emp_no, name = parts[0], parts[1]
        if not emp_no.isalnum():
            errors.append(f"第 {line_no} 行：工号 {emp_no} 只能包含字母和数字")
            continue
        if not name:
            errors.append(f"第 {line_no} 行：姓名为空")
            continue

        dept = parts[2] if len(parts) > 2 and parts[2] else None
        pwd = parts[3] if len(parts) > 3 and parts[3] else DEFAULT_IMPORT_PASSWORD
        weak = auth.validate_password_strength(pwd)
        if weak:
            errors.append(f"第 {line_no} 行（{emp_no}）：{weak}")
            continue

        rows.append({"emp_no": emp_no, "name": name, "dept": dept or "", "pwd": pwd})
        if len(rows) > MAX_IMPORT_ROWS:
            errors.append(f"一次最多导入 {MAX_IMPORT_ROWS} 行，请分批")
            break

    return rows, errors


async def _users_page(
    request: Request,
    user: dict,
    error: str | None = None,
    notice: str | None = None,
    skipped: list[str] | None = None,
) -> HTMLResponse:
    users = await db.list_users(limit=500)
    return render(
        request,
        "admin_users.html",
        {
            "user": user,
            "users": users,
            "error": error,
            "notice": notice,
            "skipped": skipped or [],
            "default_password": DEFAULT_IMPORT_PASSWORD,
        },
    )


@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request) -> HTMLResponse:
    user, deny = await require_admin(request)
    if deny:
        return deny
    return await _users_page(request, user)


@router.post("/admin/users/import")
async def admin_import_users(request: Request):
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    raw = form.get("roster")
    text = raw if isinstance(raw, str) else ""
    if not text.strip():
        return await _users_page(request, user, error="名单是空的，把「工号,姓名,部门」粘进来")

    rows, errors = _parse_import(text)
    if not rows:
        return await _users_page(request, user, error="；".join(errors) or "没有可导入的行")

    created = 0
    skipped: list[str] = []
    for row in rows:
        _, ok = await db.create_user_if_absent(
            emp_no=row["emp_no"],
            name=row["name"],
            dept=row["dept"] or None,
            pwd_hash=auth.hash_password(row["pwd"]),
            actor=user,
            ip=_ip(request),
        )
        if ok:
            created += 1
        else:
            skipped.append(row["emp_no"])

    notice = f"导入完成：新增 {created} 个账号。"
    if skipped:
        # 明确说"已存在的不动"，否则管理员会以为导入失败
        notice += f"跳过 {len(skipped)} 个已存在的工号（密码未被重置）：{'、'.join(skipped[:10])}"
        if len(skipped) > 10:
            notice += " 等"
    logger.info("管理员 %s 导入账号：新增 %d，跳过 %d", user["emp_no"], created, len(skipped))

    return await _users_page(request, user, notice=notice, skipped=skipped, error="；".join(errors) or None)


@router.post("/admin/users/{user_id}/status")
async def admin_set_user_status(request: Request, user_id: str):
    """
    停用 / 启用账号。

    停用**不自动释放**名下未完成的预约（待答项 3 的默认口径）。
    这里不偷偷替管理员做决定，而是回到列表页告诉他"此人手上还有 N 单"，
    由人去「超期与未取」处置——那才是有人负责的处置方式。
    """
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    target = _clean(form.get("status"), 20)
    if target not in ("active", "disabled"):
        return await _users_page(request, user, error="状态只能是 active / disabled")
    if user_id == user["id"]:
        # 把自己停用会立刻锁死后台入口，且没有任何人能被恢复它
        return await _users_page(request, user, error="不能停用自己的账号")

    try:
        await db.set_user_status(user_id, target, actor=user, ip=_ip(request))
    except ApiError as exc:
        return await _users_page(request, user, error=exc.message)

    notice = "账号状态已更新。"
    if target == "disabled":
        pending = await db.count_active_bookings_of_user(user_id)
        if pending:
            notice += f"该员工名下还有 {pending} 条未完成的预约，需要在「超期与未取」里处置。"
    return await _users_page(request, user, notice=notice)


@router.post("/admin/users/{user_id}/reset")
async def admin_reset_password(request: Request, user_id: str):
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    target = await db.fetch_user(user_id)
    if target is None:
        return await _users_page(request, user, error="账号不存在")

    try:
        await db.reset_user_password(
            user_id, auth.hash_password(DEFAULT_IMPORT_PASSWORD), actor=user, ip=_ip(request)
        )
    except ApiError as exc:
        return await _users_page(request, user, error=exc.message)

    logger.info("管理员 %s 重置了 %s 的密码", user["emp_no"], target["emp_no"])
    # 密码明文只在这一句提示里出现，不写日志——日志会被更多人看到
    return await _users_page(
        request, user,
        notice=f"已把 {target['emp_no']} 的密码重置为 {DEFAULT_IMPORT_PASSWORD}，对方下次登录需强制改密。",
    )


# =============================================================================
# R07 操作日志
# =============================================================================

# 字段中文名。日志要给人看，直接甩 status: booked → cancelled 等于没翻译。
_FIELD_LABELS = {
    "status": "状态",
    "asset_no": "资产编号",
    "name": "名称",
    "model": "型号",
    "location": "位置",
    "note": "备注",
    "device_id": "设备",
    "slot_date": "日期",
    "slot_start": "时段起点",
    "slot_end": "时段终点",
    "remark": "备注",
    "picked_at": "取走时间",
    "returned_at": "归还时间",
    "active_flag": "占用标记",
    "deleted_at": "删除时间",
    "user_id": "员工",
    "dept": "部门",
    "role": "角色",
    "must_change_pwd": "强制改密",
    "locked_until": "锁定至",
}

_STATUS_LABELS = {
    "booked": "待取用", "picked": "使用中", "returned": "已归还",
    "cancelled": "已取消", "expired": "已过期",
    "available": "可借", "borrowed": "借出中", "offline": "已下架",
    "active": "启用", "disabled": "停用",
}

# 不进「变更」列的字段。
# 前两个是实现细节：active_flag 只有 'a' 和 NULL 两种值，写出来是「占用标记 a → —」，
# 管理员看不出这跟他有什么关系；reason 已经在标记页单独展示。
# 后四个是 UUID 与创建时间：UUID 摊在表格里没人读，创建时间在这行左边已经有一列。
_LOG_HIDDEN = {"reason", "active_flag", "id", "created_at", "device_id", "user_id"}

# 动作码 → 人话。日志页是给管理员看的，不是给开发者看的。
_ACTION_LABELS = {
    "booking.create": "创建预约",
    "booking.cancel": "取消预约",
    "booking.pickup": "登记取走",
    "booking.return": "登记归还",
    "booking.expire": "自动过期",
    "booking.force_close": "强制结束",
    "device.create": "新增设备",
    "device.update": "编辑设备",
    "device.status": "上下架",
    "device.delete": "删除设备",
    "user.create": "新建账号",
    "user.disable": "停用账号",
    "user.enable": "启用账号",
    "user.reset_password": "重置密码",
    "prompt.publish": "发布提示词",
    "kb.upload": "上传制度",
    "kb.disable": "停用制度文档",
    "kb.enable": "启用制度文档",
    "kb.delete": "删除制度文档",
    "kb.rebuild": "重建检索索引",
    "audit.flag": "标记对话",
    "audit.reflag": "修改标记",
    "audit.unflag": "撤销标记",
}


def _pretty(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, str) and value in _STATUS_LABELS:
        return _STATUS_LABELS[value]
    # 库里存的是 2026-11-04T19:48:00+08:00。整串塞进表格既不换行也读不动，
    # 压成「11-04 19:48」：年份在看日志这件事上几乎从来不是变量。
    if isinstance(value, str) and len(value) >= 16 and value[4] == "-" and value[10] == "T":
        return value[5:16].replace("T", " ")
    return str(value)


def _diff_brief(before_json: str | None, after_json: str | None) -> str:
    """
    把 before/after 快照压缩成一行"哪几个字段变了"。

    为什么不直接把 JSON 摊在表格里：一张日志页 300 行，每行一段 JSON，
    管理员只会放弃阅读。而"状态：待取用 → 已取消"是一眼就能懂的。

    只展示有变化的字段；没有变化（比如强制结束后 status 又变回来）就如实说
    "无字段变化"，不假装有内容。
    """
    try:
        before = json.loads(before_json) if before_json else None
        after = json.loads(after_json) if after_json else None
    except (ValueError, TypeError):
        return "（快照无法解析）"

    if not after:
        return "—"

    if not before:
        # 新建：列出最能说明"这条是什么"的字段
        keys = ["asset_no", "name", "slot_date", "slot_start", "emp_no", "status"]
        parts = [f"{_FIELD_LABELS.get(k, k)} {_pretty(after[k])}" for k in keys if k in after]
        return "建立：" + " · ".join(parts) if parts else "建立"

    changes: list[str] = []
    for key, new_value in after.items():
        if key in _LOG_HIDDEN:
            continue
        old_value = before.get(key)
        if old_value != new_value:
            changes.append(
                f"{_FIELD_LABELS.get(key, key)} {_pretty(old_value)} → {_pretty(new_value)}"
            )
    return "；".join(changes) if changes else "无字段变化"


@router.get("/admin/logs", response_class=HTMLResponse)
async def admin_logs(request: Request) -> HTMLResponse:
    """
    日志查询。支持按对象类型、对象 id、动作筛选。

    target_id 是这里最有用的一个筛选：管理员从"这条预约不对劲"出发，
    直接把 booking_id 粘进来，就能拿到它的完整轨迹（R07 验收口径）。
    """
    user, deny = await require_admin(request)
    if deny:
        return deny

    qp = request.query_params
    target_type = qp.get("target_type") or None
    target_id = qp.get("target_id") or None
    action = qp.get("action") or None

    rows = await db.list_op_logs(
        target_type=target_type if target_type in ("booking", "device", "user", "prompt", "run", "kb") else None,
        target_id=target_id.strip() if target_id and target_id.strip() else None,
        action=action.strip() if action and action.strip() else None,
        limit=300,
    )
    for row in rows:
        row["brief"] = _diff_brief(row.get("before_json"), row.get("after_json"))
        row["action_label"] = _ACTION_LABELS.get(row.get("action"), row.get("action") or "—")

    return render(
        request,
        "admin_logs.html",
        {
            "user": user,
            "logs": rows,
            "filters": {"target_type": target_type or "", "target_id": target_id or "", "action": action or ""},
        },
    )


# =============================================================================
# 提示词模板（批次 3）
# -----------------------------------------------------------------------------
# 这一页要解决的不是"能改"，而是"敢改"。
# 敢改需要三样东西，缺一样管理员就会退回去改代码：
#   1) 改之前能看见效果 —— 预览按钮，用真实日期渲染一遍；
#   2) 改坏了能退回去   —— 版本历史 + 一键回滚，回滚也是追加而不是覆盖；
#   3) 改错了能查到是谁 —— 每次发布都进 op_logs，动作码 prompt.publish。
# 只做第 1 条，管理页就是个危险的文本框；三条齐了，它才是个可以交给别人用的工具。
# =============================================================================


async def _prompt_versions(tpl_key: str, limit: int = 20) -> list[dict[str, Any]]:
    """版本列表。内容不整段传给模板——20 个版本 × 2500 字会把页面撑爆。"""
    rows = await db.list_prompt_versions(tpl_key, limit=limit)
    for r in rows:
        content = r.get("content") or ""
        first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
        r["preview"] = first_line[:70]
        r["chars"] = len(content)
        r["lines"] = content.count("\n") + 1 if content else 0
    return rows


async def _prompt_edit_page(
    request: Request,
    user: dict,
    tpl_key: str,
    *,
    content: str | None = None,
    note: str = "",
    error: str | None = None,
    notice: str | None = None,
    warnings: list[str] | None = None,
    preview: str | None = None,
    active_version: int | None = None,
) -> HTMLResponse:
    active = await db.active_prompt(tpl_key)
    versions = await _prompt_versions(tpl_key)
    body = content if content is not None else (active["content"] if active else prompts.DEFAULT_TEMPLATE)
    return render(
        request,
        "admin_prompts.html",
        {
            "user": user,
            "tpl_key": tpl_key,
            "tpl_label": prompts.PROMPT_KEY_LABELS[tpl_key],
            "active": active,
            "versions": versions,
            "content": body,
            "note": note,
            "error": error,
            "notice": notice,
            "warnings": warnings or [],
            "preview": preview,
            "placeholders": prompts.PLACEHOLDERS,
            "max_chars": prompts.MAX_TEMPLATE_CHARS,
            "active_version": active_version if active_version is not None else (active["version"] if active else None),
        },
    )


def _is_known_prompt_key(tpl_key: str) -> bool:
    return tpl_key in prompts.PROMPT_KEY_LABELS


@router.get("/admin/prompts", response_class=HTMLResponse)
async def admin_prompts_index(request: Request) -> HTMLResponse:
    """模板清单。目前只有系统提示词一条，但结构按"会有多条"来写。"""
    user, deny = await require_admin(request)
    if deny:
        return deny

    items: list[dict[str, Any]] = []
    for key, label in prompts.PROMPT_KEY_LABELS.items():
        active = await db.active_prompt(key)
        versions = await db.list_prompt_versions(key, limit=1)
        items.append(
            {
                "key": key,
                "label": label,
                "active": active,
                "version_count": len(await db.list_prompt_versions(key, limit=200)),
                "last": versions[0] if versions else None,
            }
        )
    return render(request, "admin_prompts_index.html", {"user": user, "items": items})


@router.get("/admin/prompts/{tpl_key}", response_class=HTMLResponse)
async def admin_prompt_edit(request: Request, tpl_key: str) -> HTMLResponse:
    user, deny = await require_admin(request)
    if deny:
        return deny
    if not _is_known_prompt_key(tpl_key):
        return render(request, "error.html", {"user": user, "code": 404, "message": "没有这个提示词模板"}, 404)

    qp = request.query_params
    notice = None
    if qp.get("ok") == "publish":
        notice = f"已发布 v{qp.get('v', '?')}，下一次提问立即生效（无需重启）"
    elif qp.get("ok") == "rollback":
        notice = f"已按 v{qp.get('from', '?')} 的内容生成 v{qp.get('v', '?')}，下次提问生效"

    return await _prompt_edit_page(request, user, tpl_key, notice=notice)


@router.post("/admin/prompts/{tpl_key}/preview", response_class=HTMLResponse)
async def admin_prompt_preview(request: Request, tpl_key: str) -> HTMLResponse:
    """
    预览：用**真实的**日期提示渲染一遍，管理员看到的就是模型看到的那段文字。
    用固定示例日期会让"今天是几号"看起来永远正确，反而掩盖了占位符拼错的问题。
    """
    user, deny = await require_admin(request)
    if deny:
        return deny
    if not _is_known_prompt_key(tpl_key):
        return render(request, "error.html", {"user": user, "code": 404, "message": "没有这个提示词模板"}, 404)

    form = await request.form()
    content = str(form.get("content") or "")
    note = _clean(form.get("note"), 120)

    errors, warnings = prompts.validate_template(content)
    if errors:
        # 有错误也照样渲染：管理员需要看到"错在哪一行、渲染出来什么后果"，
        # 而不是一个红条加一个空预览框。
        return await _prompt_edit_page(
            request, user, tpl_key, content=content, note=note,
            error="；".join(errors), warnings=warnings,
            preview=prompts.system_prompt(build_today_hint(), content),
        )

    return await _prompt_edit_page(
        request, user, tpl_key, content=content, note=note,
        warnings=warnings,
        notice="这是预览，尚未发布",
        preview=prompts.system_prompt(build_today_hint(), content),
    )


@router.post("/admin/prompts/{tpl_key}/save")
async def admin_prompt_save(request: Request, tpl_key: str):
    user, deny = await require_admin(request)
    if deny:
        return deny
    if not _is_known_prompt_key(tpl_key):
        return render(request, "error.html", {"user": user, "code": 404, "message": "没有这个提示词模板"}, 404)

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    content = str(form.get("content") or "")
    note = _clean(form.get("note"), 120)

    errors, warnings = prompts.validate_template(content)
    if errors:
        return await _prompt_edit_page(
            request, user, tpl_key, content=content, note=note,
            error="；".join(errors), warnings=warnings,
        )

    try:
        result = await db.publish_prompt(
            tpl_key=tpl_key, content=content, note=note or None, actor=user, ip=_ip(request)
        )
    except ApiError as exc:
        return await _prompt_edit_page(
            request, user, tpl_key, content=content, note=note,
            error=exc.message, warnings=warnings,
        )

    logger.info("管理员 %s 发布提示词 %s v%s", user.get("emp_no"), tpl_key, result["version"])
    return RedirectResponse(f"/admin/prompts/{tpl_key}?ok=publish&v={result['version']}", status_code=302)


@router.post("/admin/prompts/{tpl_key}/rollback")
async def admin_prompt_rollback(request: Request, tpl_key: str):
    """
    回滚 = 复制旧内容成新版本。

    页面上写的是"回滚到 v3"，而库里发生的是"发布了 v9（内容同 v3）"。
    这个差别必须让管理员看见，否则他下次看到版本号跳到 9 会以为谁又改了一版。
    """
    user, deny = await require_admin(request)
    if deny:
        return deny
    if not _is_known_prompt_key(tpl_key):
        return render(request, "error.html", {"user": user, "code": 404, "message": "没有这个提示词模板"}, 404)

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    raw = form.get("version")
    try:
        version = int(str(raw))
    except (TypeError, ValueError):
        return await _prompt_edit_page(request, user, tpl_key, error="版本号不正确")

    try:
        result = await db.rollback_prompt(tpl_key=tpl_key, version=version, actor=user, ip=_ip(request))
    except ApiError as exc:
        return await _prompt_edit_page(request, user, tpl_key, error=exc.message)

    logger.info("管理员 %s 回滚提示词 %s 到 v%s（新版本 v%s）",
                user.get("emp_no"), tpl_key, version, result["version"])
    return RedirectResponse(
        f"/admin/prompts/{tpl_key}?ok=rollback&v={result['version']}&from={version}", status_code=302
    )


# =============================================================================
# 对话审计（批次 4）
# -----------------------------------------------------------------------------
# 与「操作日志」的分工必须先说清楚，否则一定会有人问"为什么不合并"：
#   操作日志 = 人对系统做了什么（谁取消了谁的预约），供追责
#   对话审计 = 模型对系统做了什么（问了什么、调了什么工具、有没有被拦下），供复盘
# 前者是业务事实，后者是行为样本。混在一起，两边都会被对方的噪音淹没。
#
# 这一页的排序也是刻意的：**先给"待确认"和"失败"，再给正常回答**。
# 正常回答是绝大多数，按时间倒序会把它铺满整屏，真正要看的异常沉到底部。
# =============================================================================

_RUN_STATUS_LABELS = {
    "running": "进行中",
    "answered": "已回答",
    "awaiting_confirm": "待确认",
    "max_rounds": "轮次超限",
    "failed": "失败",
}

_STEP_STATUS_LABELS = {
    "ok": "成功",
    "error": "失败",
    "interrupted": "已中断",
    "skipped": "已跳过",
}

_TOOL_LABELS = {
    "search_policy": "制度检索",
    "find_available_slots": "查空闲时段",
    "get_device_info": "查设备信息",
    "get_my_bookings": "查我的预约",
    "validate_booking": "校验预约",
    "request_booking": "发起预约",
    "cancel_booking": "取消预约",
}

_RUN_STATUS_TONE = {
    "answered": "ok",
    "awaiting_confirm": "warn",
    "max_rounds": "warn",
    "failed": "danger",
    "running": "mute",
}

# 待确认动作的最终结局。confirmed 才算"真的写进去了"，
# 其余四种都意味着这次写意图没有落地——审计要能一眼分清。
_PENDING_STATUS_LABELS = {
    "pending": "等用户点击",
    "confirmed": "已确认（落单）",
    "rejected": "用户拒绝",
    "expired": "已过期",
    "failed": "确认时失败",
}

_PENDING_STATUS_TONE = {
    "confirmed": "ok",
    "pending": "warn",
    "rejected": "mute",
    "expired": "mute",
    "failed": "danger",
}


def _pretty_json(raw: str | None) -> str:
    """把存下来的 JSON 参数排版成人能读的样子；解析不了就原样返回。"""
    if not raw:
        return ""
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except (ValueError, TypeError):
        return raw


def _ms_text(ms: Any) -> str:
    """
    毫秒转人话。

    审计表里全是 `total_ms` 这种裸数字，一眼看不出好坏；
    "1.8s" 和 "1800" 对管理员的信息量完全不同。0 和 None 都显示破折号，
    因为 0ms 只出现在脚本化的假模型里，真实调用不会是 0。
    """
    try:
        value = int(ms)
    except (TypeError, ValueError):
        return "—"
    if value <= 0:
        return "—"
    return f"{value / 1000:.1f}s" if value >= 1000 else f"{value}ms"


async def _audit_page(
    request: Request,
    user: dict,
    *,
    keyword: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
    only_flagged: bool = False,
) -> HTMLResponse:
    runs = await db.list_runs(
        keyword=keyword or None,
        status=status if status in _RUN_STATUS_LABELS else None,
        date_from=date_from or None,
        date_to=date_to or None,
        only_flagged=only_flagged,
        limit=100,
    )
    for r in runs:
        r["status_label"] = _RUN_STATUS_LABELS.get(r.get("status"), r.get("status") or "—")
        r["status_tone"] = _RUN_STATUS_TONE.get(r.get("status"), "mute")
        r["elapsed_text"] = _ms_text(r.get("total_ms"))

    summary = await db.audit_summary(days=7)
    return render(
        request,
        "admin_audit.html",
        {
            "user": user,
            "runs": runs,
            "summary": summary,
            "filters": {
                "keyword": keyword,
                "status": status,
                "date_from": date_from,
                "date_to": date_to,
                "only_flagged": only_flagged,
            },
        },
    )


@router.get("/admin/audit", response_class=HTMLResponse)
async def admin_audit(request: Request) -> HTMLResponse:
    user, deny = await require_admin(request)
    if deny:
        return deny

    qp = request.query_params
    return await _audit_page(
        request,
        user,
        keyword=(qp.get("q") or "").strip()[:60],
        status=(qp.get("status") or "").strip(),
        date_from=(qp.get("from") or "").strip(),
        date_to=(qp.get("to") or "").strip(),
        only_flagged=qp.get("flagged") == "1",
    )


@router.get("/admin/audit/{run_id}", response_class=HTMLResponse)
async def admin_audit_detail(request: Request, run_id: str) -> HTMLResponse:
    user, deny = await require_admin(request)
    if deny:
        return deny

    run = await db.fetch_run(run_id)
    if run is None:
        return render(request, "error.html", {"user": user, "code": 404, "message": "没有这条对话记录"}, 404)

    steps = await db.list_steps(run_id)
    for s in steps:
        s["tool_label"] = _TOOL_LABELS.get(s.get("tool_name"), s.get("tool_name") or "—")
        s["status_label"] = _STEP_STATUS_LABELS.get(s.get("status"), s.get("status") or "—")
        s["args_pretty"] = _pretty_json(s.get("args_json"))
        s["is_write"] = (s.get("tool_name") or "") in ("request_booking", "cancel_booking")

    pendings = await db.list_pendings_by_run(run_id)

    # 会话上下文：用 trace_id 认出"这一问"和"这一答"。
    # 不靠时间戳，因为同一秒可能有两轮；trace_id 是唯一能准确定位的字段。
    transcript: list[dict[str, Any]] = []
    if run.get("session_id"):
        msgs = await db.list_messages(run["session_id"], limit=200)
        for m in msgs:
            m["is_current"] = bool(run.get("trace_id")) and m.get("trace_id") == run.get("trace_id")
            transcript.append(m)

    run["status_label"] = _RUN_STATUS_LABELS.get(run.get("status"), run.get("status") or "—")
    run["status_tone"] = _RUN_STATUS_TONE.get(run.get("status"), "mute")
    run["elapsed_text"] = _ms_text(run.get("total_ms"))

    for p in pendings:
        p["status_label"] = _PENDING_STATUS_LABELS.get(p.get("status"), p.get("status") or "—")
        p["status_tone"] = _PENDING_STATUS_TONE.get(p.get("status"), "mute")

    return render(
        request,
        "admin_audit_detail.html",
        {
            "user": user,
            "run": run,
            "steps": steps,
            "pendings": pendings,
            "transcript": transcript,
            "tool_labels": _TOOL_LABELS,
        },
    )


@router.post("/admin/audit/{run_id}/flag")
async def admin_audit_flag(request: Request, run_id: str):
    """
    标记 / 撤销「需要跟进」。

    标记不是把对话删掉或者改状态——原始轨迹永远不动。它只是人的一句批注，
    所以放在 audit_flags 而不是往 agent_runs 上加列。
    """
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    if await db.fetch_run(run_id) is None:
        return render(request, "error.html", {"user": user, "code": 404, "message": "没有这条对话记录"}, 404)

    reason = _clean(form.get("reason"), 120)
    clearing = bool(form.get("clear"))
    try:
        if clearing or not reason:
            await db.unflag_run(run_id, actor=user, ip=_ip(request))
        else:
            await db.flag_run(run_id, reason, actor=user, ip=_ip(request))
    except ApiError as exc:
        return render(request, "error.html", {"user": user, "code": exc.status, "message": exc.message}, exc.status)

    return RedirectResponse(f"/admin/audit/{run_id}", status_code=302)


# =============================================================================
# 制度知识库（批次 5）
# -----------------------------------------------------------------------------
# 这一页解决的是 P0：此前 warmup() 只在进程启动时跑一次，管理员改了制度文件，
# 用户问到的还是旧条文，除非重启服务。制度每年都要改，这条路径必然会被走到。
#
# 页面上的三个动作，各自对应一个真实场景：
#   上传   新制度发布，把文件交给系统；
#   停用   旧制度作废，保留原件但不再被检索（不是删除）；
#   重建   绕过网页直接改了磁盘文件后，让索引重新读一遍。
#
# 为什么"停用"是主操作、"删除"是次要操作：
#   制度文档是有版本、有生效期的。作废时正确动作是"不再命中"，不是"销毁原件"。
#   删除只对上传文件开放（内置文档随代码走 git，网页上删掉会和仓库产生静默分歧）。
# =============================================================================

_KB_OK_NOTICES = {
    "upload": "上传成功，索引已重建，下一次提问就会用到新制度",
    "enable": "已重新启用，索引已重建",
    "disable": "已停用，索引已重建，这份文档不再参与检索",
    "delete": "已删除（文件已移到 data/kb/.trash），索引已重建",
    "rebuild": "索引已按磁盘现状重建",
}


async def _kb_rows() -> list[dict[str, Any]]:
    """
    页面用的文档清单 = **磁盘上的文件** + 登记表里的状态。

    以磁盘为准，不以数据库为准：数据库只是备注，文件才是制度本体。
    这样即使某次重建失败、元数据没同步上，页面也不会假装"文档不存在"。
    """
    rows = {r["doc_key"]: r for r in await db.list_kb_documents()}
    disk = await rag.scan()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for d in disk:
        r = rows.get(d["doc_key"]) or {}
        out.append(
            {
                "doc_key": d["doc_key"],
                "display_name": d["display_name"],
                "origin": d["origin"],
                "status": r.get("status") or "active",
                "chunks": int(r.get("chunks") or 0),
                "size_bytes": d["size_bytes"],
                "file_mtime": d["file_mtime"],
                "on_disk": True,
            }
        )
        seen.add(d["doc_key"])

    # 登记过但磁盘上已经没有的：不隐藏。管理员需要知道"这份文档没了"，
    # 而不是发现检索结果少了一段却找不到原因。
    for key, r in rows.items():
        if key not in seen:
            out.append(
                {
                    "doc_key": key,
                    "display_name": r["display_name"],
                    "origin": r["origin"],
                    "status": r["status"],
                    "chunks": int(r["chunks"] or 0),
                    "size_bytes": int(r["size_bytes"] or 0),
                    "file_mtime": r["file_mtime"],
                    "on_disk": False,
                }
            )

    out.sort(key=lambda x: (x["status"] != "active", x["origin"] != "builtin", x["display_name"]))
    return out


def _kb_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


_KB_REASON_LABELS = {
    "startup": "服务启动",
    "manual": "手动重建",
    "upload": "上传后重建",
    "delete": "删除后重建",
    "enable": "启用后重建",
    "disable": "停用后重建",
}


def _kb_reason_label(reason: str | None) -> str:
    """
    重建原因转人话。

    原始 reason 是 `delete:uploads/xxx.md` 这种给日志用的串，
    直接铺在页面上有两个问题：英文动作码看不懂，而且**会把刚删掉的文档名再显示一遍**，
    看起来像没删成功。日志里保留原串用于追溯，页面上只给人看的部分翻译过来。
    """
    if not reason:
        return "—"
    head = reason.split(":", 1)[0]
    return _KB_REASON_LABELS.get(head, reason)


async def _kb_page(
    request: Request,
    user: dict[str, Any],
    *,
    error: str | None = None,
    notice: str | None = None,
    preview_key: str | None = None,
) -> HTMLResponse:
    rows = await _kb_rows()
    st = rag.status()
    last = dict(st.get("last_build") or {})
    last["label"] = _kb_reason_label(last.get("reason"))
    chunks = await rag.preview(preview_key) if preview_key else []
    preview_doc = next((r for r in rows if r["doc_key"] == preview_key), None) if preview_key else None
    return render(
        request,
        "admin_policies.html",
        {
            "user": user,
            "docs": rows,
            "stats": {
                "ready": st["ready"],
                "chunks": st["chunks"],
                "embed_backend": st["embed_backend"],
                "error": st["error"],
                "last": last,
                "active_docs": sum(1 for r in rows if r["status"] == "active" and r["on_disk"]),
                "disabled_docs": sum(1 for r in rows if r["status"] == "disabled"),
                "missing_docs": sum(1 for r in rows if not r["on_disk"]),
            },
            "error": error,
            "notice": notice,
            "preview_key": preview_key,
            "preview_doc": preview_doc,
            "preview_chunks": chunks,
            "max_kb": rag.MAX_UPLOAD_BYTES // 1024,
            "builtin_dir": st["builtin_dir"],
            "upload_dir": st["upload_dir"],
        },
    )


def _kb_rebuild_msg(res: dict[str, Any]) -> str | None:
    """重建结果转一句话。成功返回 None，失败返回给人看的原因。"""
    if res.get("ok"):
        return None
    kept = "检索仍在用上一次的索引" if res.get("kept_previous") else "当前没有可用索引，问答会返回 503"
    return f"索引重建失败：{res.get('error')}（{kept}）"


async def _rebuild_after_change(user: dict[str, Any], request: Request, reason: str) -> dict[str, Any]:
    """
    变更后立即重建，并留痕。

    日志用 write_op_log（独立事务）而不是 db 层的 _log：
    文件写入、删文件都不在数据库事务里，硬凑成"原子"只是假象。
    这里如实记"重建发生在什么原因之后"，才是有用的信息。
    """
    res = await rag.rebuild(reason)
    await db.write_op_log(
        actor=user,
        action="kb.rebuild",
        target_type="kb",
        target_id=reason,
        after={
            "ok": bool(res.get("ok")),
            "chunks": res.get("chunks"),
            "docs": res.get("docs"),
            "cache_hit": res.get("cache_hit"),
            "error": res.get("error"),
        },
        ip=_ip(request),
    )
    return res


@router.get("/admin/policies", response_class=HTMLResponse)
async def admin_policies(request: Request) -> HTMLResponse:
    user, deny = await require_admin(request)
    if deny:
        return deny

    qp = request.query_params
    ok = qp.get("ok")
    return await _kb_page(
        request,
        user,
        notice=_KB_OK_NOTICES.get(ok or ""),
        preview_key=(qp.get("preview") or "").strip() or None,
    )


@router.post("/admin/policies/upload")
async def admin_kb_upload(request: Request):
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    upload = form.get("file")
    if upload is None or isinstance(upload, str):
        return await _kb_page(request, user, error="请选择要上传的文件")

    # starlette 的 UploadFile 会先把大文件落到临时目录，所以先看 size 再读内容，
    # 别等把 200MB 读进内存才告诉用户"太大了"。
    if getattr(upload, "size", None) and upload.size > rag.MAX_UPLOAD_BYTES:
        return await _kb_page(
            request, user,
            error=f"文件 {upload.size // 1024}KB，超过上限 {rag.MAX_UPLOAD_BYTES // 1024}KB",
        )

    try:
        saved = await rag.save_upload(upload.filename or "", await upload.read())
    except ApiError as exc:
        return await _kb_page(request, user, error=exc.message)
    except OSError as exc:
        return await _kb_page(request, user, error=f"文件保存失败：{exc}")

    # 先记"谁传了什么"，再重建。顺序很重要：万一重建失败，这份文件也已经
    # 落到磁盘上了，日志里必须有它，否则会出现一个"查不到来源"的制度文件。
    await db.write_op_log(
        actor=user,
        action="kb.upload",
        target_type="kb",
        target_id=saved["doc_key"],
        after={"display_name": saved["display_name"], "size_bytes": saved["size_bytes"], "chars": saved["chars"]},
        ip=_ip(request),
    )

    res = await _rebuild_after_change(user, request, f"upload:{saved['doc_key']}")
    logger.info("管理员 %s 上传制度文档 %s", user.get("emp_no"), saved["doc_key"])

    if not res.get("ok"):
        # 文件已经落盘、也登记了，只是没进索引。如实说明，别让人以为上传失败又传一遍。
        return await _kb_page(
            request, user,
            error=f"《{saved['display_name']}》已保存，但{_kb_rebuild_msg(res)}",
        )
    return RedirectResponse("/admin/policies?ok=upload", status_code=302)


@router.post("/admin/policies/status")
async def admin_kb_status(request: Request):
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    doc_key = _clean(form.get("doc_key"), 160)
    action = _clean(form.get("action"), 20)
    if not doc_key or action not in ("enable", "disable"):
        return await _kb_page(request, user, error="参数不完整，请刷新页面后重试")

    if action == "disable":
        # 停用最后一份文档会让检索彻底空转（问答全 503），所以先拦。
        # 放在这里而不是 rebuild 里，是因为状态一旦改了、重建却失败，库和索引会打架。
        active = [r for r in await _kb_rows() if r["status"] == "active" and r["on_disk"]]
        if len(active) <= 1 and any(r["doc_key"] == doc_key for r in active):
            return await _kb_page(request, user, error="至少要保留一份启用的制度文档；要换文档请先上传新的再停用旧的")

    try:
        changed = await db.set_kb_document_status(doc_key, "active" if action == "enable" else "disabled", actor=user, ip=_ip(request))
    except ApiError as exc:
        return await _kb_page(request, user, error=exc.message)

    if not changed.get("changed"):
        return await _kb_page(request, user, notice="状态没有变化，无需重建")

    res = await _rebuild_after_change(user, request, f"{action}:{doc_key}")
    logger.info("管理员 %s %s 制度文档 %s", user.get("emp_no"), "启用" if action == "enable" else "停用", doc_key)
    if not res.get("ok"):
        return await _kb_page(request, user, error=f"状态已改，但{_kb_rebuild_msg(res)}")
    return RedirectResponse(f"/admin/policies?ok={action}", status_code=302)


@router.post("/admin/policies/delete")
async def admin_kb_delete(request: Request):
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    doc_key = _clean(form.get("doc_key"), 160)
    if not doc_key:
        return await _kb_page(request, user, error="参数不完整，请刷新页面后重试")

    # 先移文件再删登记：文件移不走就什么都别动（最坏情况是"记录还在"，可重来）；
    # 反过来先删登记，万一移文件失败，就成了一份谁也看不见的孤儿文件。
    try:
        moved = await rag.trash_upload(doc_key)
        deleted = await db.delete_kb_document(doc_key, actor=user, ip=_ip(request))
    except ApiError as exc:
        return await _kb_page(request, user, error=exc.message)
    except OSError as exc:
        return await _kb_page(request, user, error=f"文件移动失败：{exc}")

    res = await _rebuild_after_change(user, request, f"delete:{doc_key}")
    logger.info("管理员 %s 删除制度文档 %s（移到 %s）", user.get("emp_no"), doc_key, moved)
    if not res.get("ok"):
        return await _kb_page(request, user, error=f"已删除《{deleted['display_name']}》，但{_kb_rebuild_msg(res)}")
    return RedirectResponse("/admin/policies?ok=delete", status_code=302)


@router.post("/admin/policies/rebuild")
async def admin_kb_rebuild(request: Request):
    user, deny = await require_admin(request)
    if deny:
        return deny

    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return render(request, "error.html", {"user": user, "code": 403, "message": "页面已过期，请刷新后重试"}, 403)

    res = await _rebuild_after_change(user, request, "manual")
    logger.info("管理员 %s 手动重建索引：ok=%s", user.get("emp_no"), res.get("ok"))
    if not res.get("ok"):
        return await _kb_page(request, user, error=_kb_rebuild_msg(res))
    return RedirectResponse("/admin/policies?ok=rebuild", status_code=302)

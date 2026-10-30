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

import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth, booking_service, db
from app.auth import current_user
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
    return RedirectResponse(f"/login?next={nxt}", status_code=302)


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


@router.post("/logout")
async def logout(request: Request) -> RedirectResponse:
    form = await request.form()
    if not auth.csrf_ok(request, form.get(auth.CSRF_FIELD)):
        return RedirectResponse("/login", status_code=302)
    resp = RedirectResponse("/login", status_code=302)
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
    user, redirect = await guard(request)
    if redirect:
        return redirect
    if user.get("role") != "admin":
        return render(request, "error.html", {"user": user, "code": 403, "message": "这个页面只有管理员可以访问"}, 403)

    today = booking_service.now().date().isoformat()
    st = await db.stats(today)
    return render(request, "admin_dashboard.html", {"user": user, "stats": st, "today": today})


@router.get("/admin/devices", response_class=HTMLResponse)
async def admin_devices(request: Request) -> HTMLResponse:
    user, redirect = await guard(request)
    if redirect:
        return redirect
    if user.get("role") != "admin":
        return render(request, "error.html", {"user": user, "code": 403, "message": "这个页面只有管理员可以访问"}, 403)

    data = await booking_service.list_devices_with_availability(booking_service.now().date().isoformat())
    return render(request, "admin_devices.html", {"user": user, "devices": data["devices"]})


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
    return render(request, "my_bookings.html", {"user": user, "bookings": rows, "now_date": booking_service.now().date().isoformat()})


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
            await booking_service.cancel(user, booking_id)
        elif action == "pickup":
            await booking_service.pickup(user, booking_id)
        elif action == "return":
            await booking_service.give_back(user, booking_id)
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

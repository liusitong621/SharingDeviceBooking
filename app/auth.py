"""
认证与授权：密码哈希、JWT 签发/校验、CSRF、FastAPI 依赖项。

四个设计决定，都值得写清楚：

1) **JWT 放 HttpOnly Cookie，不放 localStorage。**
   本项目是服务端渲染 + 表单提交，Cookie 是唯一自然的选择；
   放 localStorage 等于把令牌暴露给任何一段 XSS。

2) **权限以数据库为准，不以 token 里的 role 为准。**
   token 有效期 8 小时，期间管理员被降权、账号被停用，
   如果只看 claim，这些变更要等 8 小时才生效。
   所以每次请求多查一次 users 表（SQLite 单条查询 < 1ms），换即时生效。

3) **登录失败信息不区分"工号不存在"和"密码错"。**
   否则登录页就成了工号枚举器。统一返回 INVALID_CREDENTIALS。

4) **CSRF 用双提交 Cookie。**
   表单里带 _csrf 隐藏字段，与 sdb_csrf Cookie 比对。
   SameSite=Lax 已经挡掉了绝大多数跨站表单，这一层是纵深防御。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, Request, Response

from app import db
from app.errors import ApiError

logger = logging.getLogger("app.auth")

# Cookie 与字段名
TOKEN_COOKIE = "sdb_token"
CSRF_COOKIE = "sdb_csrf"
CSRF_FIELD = "_csrf"

TOKEN_TTL_HOURS = 8
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 10

CSRF_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")


# =============================================================================
# 密码：标准库 scrypt，不引第三方库
# -----------------------------------------------------------------------------
# 存成自描述格式 `scrypt$n$r$p$salt$hash`：
#   - 参数跟着哈希一起存，将来调高 N 不会让老密码失效；
#   - 用 hmac.compare_digest 比较，避免逐字节比较泄露长度信息。
# =============================================================================

_SCRYPT_N = 2 ** 14      # 16384，约 16MB 内存、30~60ms，登录场景完全可接受
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str) -> str:
    """生成密码哈希。每次都用新盐，同一密码两次哈希结果不同。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(dk)}"


def verify_password(password: str, encoded: str) -> bool:
    """校验密码。格式不认识就一律当失败，不抛异常（避免脏数据把登录打挂）。"""
    try:
        algo, n, r, p, salt_b64, hash_b64 = encoded.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64d(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(_b64d(hash_b64)),
        )
        return hmac.compare_digest(dk, _b64d(hash_b64))
    except (ValueError, TypeError):
        return False


def validate_password_strength(password: str) -> str | None:
    """返回错误文案；通过返回 None。规则刻意保守，只挡最差的几种。"""
    if len(password) < 8:
        return "密码至少 8 位"
    if len(password) > 64:
        return "密码不能超过 64 位"
    if password.isdigit():
        return "密码不能是纯数字"
    if password.lower() in {"12345678", "password", "admin123", "qwertyui"}:
        return "密码过于常见，请换一个"
    return None


# =============================================================================
# JWT
# =============================================================================


def _secret() -> str:
    """
    签名密钥：优先环境变量，其次落盘到 data/.jwt_secret。

    落盘是必须的——否则每次重启都换密钥，所有人都被登出，
    开发时看起来像"改了代码就掉登录"的玄学 bug。
    """
    env = os.getenv("JWT_SECRET")
    if env:
        return env
    path = db.DB_DIR / ".jwt_secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(48)
    db.DB_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    path.chmod(0o600)
    logger.warning("未配置 JWT_SECRET，已生成随机密钥并写入 %s", path)
    return secret


def create_token(user: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["id"],
        "emp_no": user["emp_no"],
        "role": user["role"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=TOKEN_TTL_HOURS)).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


# =============================================================================
# 当前用户
# =============================================================================


async def user_from_request(request: Request) -> dict[str, Any] | None:
    """从 Cookie 解析用户；任何一步不成立都返回 None（不是抛错）。"""
    token = request.cookies.get(TOKEN_COOKIE)
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    user = await db.fetch_user(payload.get("sub"))
    if user is None or user.get("status") != "active":
        return None
    return user


async def current_user(request: Request) -> dict[str, Any]:
    """API 依赖：未登录抛 401。"""
    user = await user_from_request(request)
    if user is None:
        raise ApiError("UNAUTHORIZED")
    return user


async def admin_user(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    """API 依赖：必须是管理员，否则 403。"""
    if user.get("role") != "admin":
        raise ApiError("FORBIDDEN", "需要管理员权限")
    return user


# =============================================================================
# 登录 / 登出
# =============================================================================


async def authenticate(emp_no: str, password: str) -> dict[str, Any]:
    """
    校验工号密码，返回用户；失败抛对应错误码。

    顺序很重要：先判断停用、再判断锁定、最后才校验密码。
    这样"账号被停用"不会被误报成"密码错误"，用户知道该找谁。
    """
    user = await db.fetch_user_by_emp_no(emp_no)
    if user is None:
        # 不做恒定时延防护（内网系统，且工号本身不是秘密）；暴露到公网前需补
        raise ApiError("INVALID_CREDENTIALS")

    if user["status"] != "active":
        raise ApiError("ACCOUNT_DISABLED")

    locked_until = user.get("locked_until")
    if locked_until:
        try:
            until = datetime.fromisoformat(locked_until)
        except ValueError:
            until = None
        if until and until > datetime.now().astimezone():
            remain = int((until - datetime.now().astimezone()).total_seconds() // 60) + 1
            raise ApiError("ACCOUNT_LOCKED", f"请 {remain} 分钟后重试")

    if not verify_password(password, user["pwd_hash"]):
        await db.record_login_failure(user["id"], MAX_FAILED_ATTEMPTS, LOCK_MINUTES)
        raise ApiError("INVALID_CREDENTIALS")

    await db.record_login_success(user["id"])
    return user


def attach_session(response: Response, user: dict[str, Any], request: Request) -> None:
    """把 JWT 与 CSRF token 写进 Cookie。"""
    secure = os.getenv("COOKIE_SECURE", "0") == "1"
    max_age = TOKEN_TTL_HOURS * 3600

    response.set_cookie(
        TOKEN_COOKIE,
        create_token(user),
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )

    # CSRF token 必须能被页面 JS/表单读到，因此不能 HttpOnly
    existing = request.cookies.get(CSRF_COOKIE)
    csrf = existing if existing and CSRF_RE.match(existing) else secrets.token_urlsafe(24)
    response.set_cookie(
        CSRF_COOKIE,
        csrf,
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(TOKEN_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


# =============================================================================
# CSRF
# =============================================================================


def csrf_token_for(request: Request) -> str:
    """给模板用的当前 CSRF token（没有就现生成，由响应写入 Cookie）。"""
    existing = request.cookies.get(CSRF_COOKIE)
    if existing and CSRF_RE.match(existing):
        return existing
    return secrets.token_urlsafe(24)


def csrf_ok(request: Request, form_token: str | None) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not form_token:
        return False
    return hmac.compare_digest(cookie, form_token)

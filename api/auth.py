"""
认证模块：JWT + API Key + 限额检查

认证优先级：
  1. Authorization: Bearer <JWT>  → 解码 JWT 获取 user_id
  2. X-API-Key: ner_xxxxx        → 哈希后查 api_keys 表 → 获取关联 user_id
  3. 两者都没有 → 401 Unauthorized
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, Request
from jose import JWTError, jwt
import logging
# 屏蔽 passlib 检测 bcrypt 版本时捕获并打印的无害错误 (trapped error reading bcrypt version)
logging.getLogger('passlib').setLevel(logging.ERROR)

from passlib.context import CryptContext

from . import db

# ─── 密码处理 ──────────────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# ─── JWT ───────────────────────────────────────────────────────────────────────

def _get_secret_key() -> str:
    return os.environ.get("JWT_SECRET_KEY", "change-this-secret-key-in-production")


def _get_expire_hours() -> int:
    return int(os.environ.get("JWT_EXPIRE_HOURS", "24"))


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(hours=_get_expire_hours()))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, _get_secret_key(), algorithm="HS256")


def decode_access_token(token: str) -> Optional[dict]:
    try:
        payload = jwt.decode(token, _get_secret_key(), algorithms=["HS256"])
        return payload
    except JWTError:
        return None


# ─── API Key ───────────────────────────────────────────────────────────────────

def generate_api_key() -> str:
    """生成 ner_ 前缀的 API Key（32字节随机 hex）。"""
    return "ner_" + secrets.token_hex(32)


def hash_api_key(key: str) -> str:
    """SHA-256 哈希 API Key。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def get_api_key_prefix(key: str) -> str:
    """获取 API Key 前缀（前 12 字符）用于展示。"""
    return key[:12]


# ─── FastAPI 依赖注入 ──────────────────────────────────────────────────────────

async def get_current_user(request: Request) -> dict:
    """
    从请求中解析当前用户。支持两种认证方式：
    1. Authorization: Bearer <JWT>
    2. X-API-Key: <key>
    """
    # 如果禁用了认证，直接返回一个 mock 的 admin 用户，避免接口 401 报错
    if os.environ.get("AUTH_ENABLED", "true").lower() == "false":
        return {
            "id": 1,
            "username": "admin",
            "is_admin": True,
            "limit_5h": 0,
            "limit_weekly": 0,
            "is_active": True
        }

    # 尝试 Bearer Token
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
        payload = decode_access_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Token 无效或已过期")

        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Token 格式错误")

        user = db.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=401, detail="用户不存在")
        if not user.get("is_active"):
            raise HTTPException(status_code=403, detail="账户已被禁用")

        return user

    # 尝试 API Key
    api_key = request.headers.get("X-API-Key")
    if api_key:
        key_hash = hash_api_key(api_key)
        key_record = db.get_api_key_by_hash(key_hash)
        if not key_record:
            raise HTTPException(status_code=401, detail="API Key 无效")
        if not key_record.get("is_active"):
            raise HTTPException(status_code=401, detail="API Key 已停用")

        # 检查过期
        expires_at = key_record.get("expires_at")
        if expires_at:
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires_at:
                raise HTTPException(status_code=401, detail="API Key 已过期")

        # 检查关联用户
        if not key_record.get("user_active"):
            raise HTTPException(status_code=403, detail="API Key 关联的用户已被禁用")

        # 返回关联用户信息
        user = db.get_user_by_id(key_record["user_id"])
        if not user:
            raise HTTPException(status_code=401, detail="API Key 关联的用户不存在")
        return user

    raise HTTPException(status_code=401, detail="需要认证。请使用 Bearer Token 或 X-API-Key")


async def get_optional_user(request: Request) -> Optional[dict]:
    """可选认证：有认证信息则解析，没有则返回 None。"""
    auth_header = request.headers.get("Authorization")
    api_key = request.headers.get("X-API-Key")
    if not auth_header and not api_key:
        return None
    return await get_current_user(request)


async def require_admin(request: Request) -> dict:
    """要求管理员权限。"""
    user = await get_current_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def check_rate_limit(user: dict) -> None:
    """检查用户调用限额。0 表示无限制。超限抛 429。"""
    user_id = user["id"]

    # 5小时限额
    limit_5h = user.get("limit_5h", 0)
    if limit_5h > 0:
        used_5h = db.get_usage_count_5h(user_id)
        if used_5h >= limit_5h:
            raise HTTPException(
                status_code=429,
                detail=f"5小时调用次数已达上限 ({used_5h}/{limit_5h})，请稍后再试",
            )

    # 周限额
    limit_weekly = user.get("limit_weekly", 0)
    if limit_weekly > 0:
        used_weekly = db.get_usage_count_weekly(user_id)
        if used_weekly >= limit_weekly:
            raise HTTPException(
                status_code=429,
                detail=f"本周调用次数已达上限 ({used_weekly}/{limit_weekly})，请稍后再试",
            )

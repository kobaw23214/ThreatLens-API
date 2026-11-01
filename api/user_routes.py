"""
用户认证路由（/auth/*）和管理员路由（/admin/*）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from . import db
from . import features
from .auth import (
    check_rate_limit,
    create_access_token,
    generate_api_key,
    get_api_key_prefix,
    get_current_user,
    hash_api_key,
    hash_password,
    require_admin,
    verify_password,
)
from .schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyInfo,
    CacheEntryListResponse,
    CacheSettings,
    CacheSettingsUpdate,
    CacheStats,
    ChangePasswordRequest,
    LoginRequest,
    LoginResponse,
    UsageStats,
    UserCreate,
    UserInfo,
    UserUpdate,
)

# ─── 公开 + 登录用户路由 ──────────────────────────────────────────────────────

import os

auth_router = APIRouter(prefix="/auth", tags=["认证"])


@auth_router.get("/config")
async def get_auth_config():
    """公开接口：返回当前认证配置与功能模块开关，供前端判断登录页与可见功能。"""
    auth_enabled = os.environ.get("AUTH_ENABLED", "true").lower() != "false"
    return {"auth_enabled": auth_enabled, "features": features.as_dict()}


@auth_router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest):
    """用户登录，返回 JWT。"""
    user = db.get_user_by_username(req.username)
    if not user:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.get("is_active"):
        raise HTTPException(status_code=403, detail="账户已被禁用")

    token = create_access_token({"user_id": user["id"], "username": user["username"]})
    return LoginResponse(
        access_token=token,
        user=UserInfo(
            id=user["id"],
            username=user["username"],
            is_admin=user["is_admin"],
            limit_5h=user["limit_5h"],
            limit_weekly=user["limit_weekly"],
            is_active=user["is_active"],
            created_at=user.get("created_at"),
            updated_at=user.get("updated_at"),
        ),
    )


@auth_router.get("/me")
async def get_me(request: Request):
    """获取当前用户信息 + 使用统计。"""
    user = await get_current_user(request)
    usage = db.get_usage_stats(user["id"])
    return {
        "user": UserInfo(
            id=user["id"],
            username=user["username"],
            is_admin=user["is_admin"],
            limit_5h=user["limit_5h"],
            limit_weekly=user["limit_weekly"],
            is_active=user["is_active"],
            created_at=user.get("created_at"),
            updated_at=user.get("updated_at"),
        ),
        "usage": UsageStats(**usage) if usage else None,
    }


@auth_router.post("/change-password")
async def change_password(req: ChangePasswordRequest, request: Request):
    """修改当前用户密码。"""
    user = await get_current_user(request)
    if not verify_password(req.old_password, user["password_hash"]):
        raise HTTPException(status_code=400, detail="旧密码错误")

    new_hash = hash_password(req.new_password)
    db.update_user(user["id"], password_hash=new_hash)
    return {"message": "密码修改成功"}


# ─── API Key 管理（登录用户） ─────────────────────────────────────────────────

@auth_router.get("/api-keys", response_model=List[ApiKeyInfo])
async def list_my_api_keys(request: Request):
    """列出当前用户的所有 API Key。"""
    user = await get_current_user(request)
    keys = db.list_api_keys(user["id"])
    return [ApiKeyInfo(**k) for k in keys]


@auth_router.post("/api-keys", response_model=ApiKeyCreated)
async def create_my_api_key(req: ApiKeyCreate, request: Request):
    """创建一个新的 API Key。"""
    user = await get_current_user(request)

    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    key_prefix = get_api_key_prefix(raw_key)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=req.expires_days)).isoformat()

    record = db.create_api_key(
        user_id=user["id"],
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=req.name,
        expires_at=expires_at,
    )
    if not record:
        raise HTTPException(status_code=500, detail="创建 API Key 失败")

    return ApiKeyCreated(
        id=record["id"],
        key=raw_key,
        key_prefix=key_prefix,
        name=req.name,
        expires_at=record.get("expires_at"),
    )


@auth_router.delete("/api-keys/{key_id}")
async def delete_my_api_key(key_id: int, request: Request):
    """删除当前用户的 API Key。"""
    user = await get_current_user(request)
    deleted = db.delete_api_key(key_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="API Key 不存在")
    return {"message": "API Key 已删除"}


# ─── 管理员路由 ───────────────────────────────────────────────────────────────

admin_router = APIRouter(prefix="/admin", tags=["管理员"])


@admin_router.get("/users")
async def admin_list_users(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=1000),
):
    """管理员：列出所有用户。"""
    await require_admin(request)
    return db.list_users(page, page_size)


@admin_router.post("/users", response_model=UserInfo)
async def admin_create_user(req: UserCreate, request: Request):
    """管理员：创建新用户。"""
    await require_admin(request)

    pw_hash = hash_password(req.password)
    user = db.create_user(
        username=req.username,
        password_hash=pw_hash,
        is_admin=False,
        limit_5h=req.limit_5h,
        limit_weekly=req.limit_weekly,
    )
    if not user:
        raise HTTPException(status_code=409, detail=f"用户名 '{req.username}' 已存在")
    return UserInfo(**user)


@admin_router.put("/users/{user_id}", response_model=UserInfo)
async def admin_update_user(user_id: int, req: UserUpdate, request: Request):
    """管理员：更新用户信息（限额、状态、密码）。"""
    await require_admin(request)

    kwargs = {}
    if req.password is not None:
        kwargs["password_hash"] = hash_password(req.password)
    if req.limit_5h is not None:
        kwargs["limit_5h"] = req.limit_5h
    if req.limit_weekly is not None:
        kwargs["limit_weekly"] = req.limit_weekly
    if req.is_active is not None:
        kwargs["is_active"] = req.is_active

    user = db.update_user(user_id, **kwargs)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return UserInfo(**user)


@admin_router.delete("/users/{user_id}")
async def admin_delete_user(user_id: int, request: Request):
    """管理员：删除用户。"""
    await require_admin(request)
    deleted = db.delete_user(user_id)
    if not deleted:
        raise HTTPException(status_code=400, detail="用户不存在或无法删除（管理员不可删除）")
    return {"message": "用户已删除"}


@admin_router.get("/users/{user_id}/api-keys", response_model=List[ApiKeyInfo])
async def admin_list_user_keys(user_id: int, request: Request):
    """管理员：查看某用户的 API Key。"""
    await require_admin(request)
    keys = db.list_api_keys(user_id)
    return [ApiKeyInfo(**k) for k in keys]


@admin_router.post("/users/{user_id}/api-keys", response_model=ApiKeyCreated)
async def admin_create_user_key(user_id: int, req: ApiKeyCreate, request: Request):
    """管理员：为某用户创建 API Key。"""
    await require_admin(request)

    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    key_prefix = get_api_key_prefix(raw_key)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=req.expires_days)).isoformat()

    record = db.create_api_key(
        user_id=user_id,
        key_hash=key_hash,
        key_prefix=key_prefix,
        name=req.name,
        expires_at=expires_at,
    )
    if not record:
        raise HTTPException(status_code=500, detail="创建 API Key 失败")

    return ApiKeyCreated(
        id=record["id"],
        key=raw_key,
        key_prefix=key_prefix,
        name=req.name,
        expires_at=record.get("expires_at"),
    )


@admin_router.get("/usage-stats")
async def admin_usage_stats(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """管理员：全局使用统计。"""
    await require_admin(request)
    return db.get_all_usage_stats(page, page_size)


# ─── 缓存复用管理 ─────────────────────────────────────────────────────────────

# 缓存分类：ner / attack 走 tasks 表（按模型 model_type 过滤），ti 走 ti_tasks 表；
# 不传 category 时保持旧行为（仅提取缓存全量）。
_CACHE_CATEGORIES = ("ner", "attack", "ti")


def _resolve_cache_category(request: Request, category: Optional[str]):
    """把 category 解析为 (is_ti, model_names|None)。

    model_names 来自 app.state.model_types（启动时由 main 写入的 {模型名: model_type}）：
    attack → model_type == 'attack'；ner → 其余（默认 'ner'，与 /models 接口口径一致）。
    category=None → (False, None)，即不过滤的全量提取缓存。
    """
    if category is None:
        return False, None
    if category not in _CACHE_CATEGORIES:
        raise HTTPException(
            status_code=422,
            detail=f"未知缓存分类: {category}（可选: {', '.join(_CACHE_CATEGORIES)}）",
        )
    if category == "ti":
        return True, None
    model_types = getattr(request.app.state, "model_types", None) or {}
    if category == "attack":
        names = [n for n, t in model_types.items() if t == "attack"]
    else:
        names = [n for n, t in model_types.items() if t != "attack"]
    return False, names


def _ti_store():
    from .ti_expand import store
    return store


@admin_router.get("/cache/settings", response_model=CacheSettings)
async def admin_get_cache_settings(request: Request):
    """管理员：读取缓存复用全局策略。"""
    await require_admin(request)
    return CacheSettings(**db.get_cache_settings())


@admin_router.put("/cache/settings", response_model=CacheSettings)
async def admin_update_cache_settings(req: CacheSettingsUpdate, request: Request):
    """管理员：更新缓存复用全局策略（TTL / 命中刷新 / 总开关）。"""
    await require_admin(request)
    updated = db.update_cache_settings(
        enabled=req.enabled,
        ttl_seconds=req.ttl_seconds,
        refresh_on_hit=req.refresh_on_hit,
    )
    return CacheSettings(**updated)


@admin_router.get("/cache/stats", response_model=CacheStats)
async def admin_cache_stats(
    request: Request,
    category: Optional[str] = Query(None, description="缓存分类: ner / attack / ti；不传=提取缓存全量"),
):
    """管理员：缓存概览统计（可复用 / 已过期数量等），可按分类过滤。"""
    await require_admin(request)
    is_ti, model_names = _resolve_cache_category(request, category)
    if is_ti:
        return CacheStats(**_ti_store().get_ti_cache_stats())
    return CacheStats(**db.get_cache_stats(model_names=model_names))


@admin_router.get("/cache/entries", response_model=CacheEntryListResponse)
async def admin_list_cache_entries(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    only_active: bool = Query(True, description="仅列出未过期的可复用任务"),
    category: Optional[str] = Query(None, description="缓存分类: ner / attack / ti；不传=提取缓存全量"),
):
    """管理员：分页查看参与缓存复用的任务列表，可按分类过滤。"""
    await require_admin(request)
    is_ti, model_names = _resolve_cache_category(request, category)
    if is_ti:
        result = _ti_store().list_ti_cache_entries(page, page_size, only_active=only_active)
    else:
        result = db.list_cache_entries(page, page_size, only_active=only_active,
                                       model_names=model_names)
    # task_id / labels 需转为可序列化形式
    items = []
    for it in result["items"]:
        it = dict(it)
        it["task_id"] = str(it["task_id"])
        items.append(it)
    return CacheEntryListResponse(total=result["total"], items=items)


@admin_router.delete("/cache/entries/{task_id}")
async def admin_invalidate_cache_entry(task_id: str, request: Request):
    """管理员：让单条任务的缓存失效（不删除任务本身）。提取 / TI 任务均可。"""
    await require_admin(request)
    ok = db.invalidate_cache_entry(task_id)
    if not ok:
        ok = _ti_store().invalidate_ti_cache_entry(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="任务不存在或本就未参与缓存")
    return {"message": "该任务缓存已失效"}


@admin_router.post("/cache/clear-expired")
async def admin_clear_expired_cache(
    request: Request,
    category: Optional[str] = Query(None, description="缓存分类: ner / attack / ti；不传=提取缓存全量"),
):
    """管理员：清理所有已过期缓存（置为不可复用，不删除任务），可按分类限定范围。"""
    await require_admin(request)
    is_ti, model_names = _resolve_cache_category(request, category)
    if is_ti:
        affected = _ti_store().clear_ti_cache(expired_only=True)
    else:
        affected = db.clear_expired_cache(model_names=model_names)
    return {"message": f"已清理 {affected} 条过期缓存", "affected": affected}


@admin_router.post("/cache/clear-all")
async def admin_clear_all_cache(
    request: Request,
    category: Optional[str] = Query(None, description="缓存分类: ner / attack / ti；不传=提取缓存全量"),
):
    """管理员：清空全部缓存复用（所有任务置为不可复用，不删除任务），可按分类限定范围。"""
    await require_admin(request)
    is_ti, model_names = _resolve_cache_category(request, category)
    if is_ti:
        affected = _ti_store().clear_ti_cache(expired_only=False)
    else:
        affected = db.clear_all_cache(model_names=model_names)
    return {"message": f"已清空 {affected} 条缓存", "affected": affected}

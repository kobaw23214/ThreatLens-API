from __future__ import annotations

from pydantic import BaseModel, Field, HttpUrl
from typing import Any, Dict, List, Literal, Optional
from datetime import datetime


class AttackAutoRoute(BaseModel):
    model: str = Field(..., min_length=1)
    metric: Literal["english_ratio", "chinese_ratio"]
    operator: Literal["gte"] = "gte"
    threshold: float = Field(..., ge=0.0, le=1.0)


class AttackAutoRoutingConfig(BaseModel):
    version: int = Field(1, ge=1)
    routes: List[AttackAutoRoute] = Field(..., min_length=1)
    fallback_model: str = Field(..., min_length=1)


class CallbackConfig(BaseModel):
    """回调配置"""
    url: str = Field(..., description="回调地址，必须是 http/https URL")
    method: str = Field("POST", description="HTTP 方法：POST/PUT")
    headers: Optional[Dict[str, str]] = Field(None, description="自定义请求头")
    timeout: int = Field(30, ge=1, le=300, description="超时时间（秒），范围 1-300")


# ─── 分块相关 schema (long-text-chunking-ner) ────────────────────────────────

class ChunkInfo(BaseModel):
    """
    单个 Chunk 的运行时信息,出现在 chunk_metadata.chunks[] 里。
    """
    chunk_index: int = Field(..., ge=0, description="块序号,从 0 起单调递增 1")
    char_start: int = Field(..., ge=0, description="在原文中的起始字符偏移(闭区间)")
    char_end: int = Field(..., ge=0, description="在原文中的结束字符偏移(开区间)")
    char_length: int = Field(..., ge=0, description="char_end - char_start")
    token_count: Optional[int] = Field(
        None, ge=0,
        description="tokenizer 编码后的 token 数;UIE 等不支持分块的模型为 null",
    )
    status: str = Field(
        "done", pattern="^(done|failed)$",
        description="单块推理状态,done 或 failed",
    )
    error: Optional[str] = Field(
        None,
        description="status=failed 时的错误摘要,长度不超过 200",
    )


class ChunkMetadata(BaseModel):
    """
    一次请求的全部分块元数据,持久化到 task_results.chunk_metadata。
    """
    model_config = {"protected_namespaces": ()}  # 允许 model_max_tokens 字段名

    chunking_enabled: bool = Field(
        ...,
        description="本次请求是否真的执行了分块;false 表示走降级路径或未开启",
    )
    model_max_tokens: Optional[int] = Field(
        None,
        description="当前模型的 tokenizer 硬上限(gliner_multi-v2.1=384, gliner-x-large=1024)",
    )
    labels_token_count: Optional[int] = Field(
        None,
        description="本次 labels 按 <<ENT>>label<<SEP>> 编码后的 token 总数",
    )
    effective_max_tokens: Optional[int] = Field(
        None,
        description="单块 token 上限 = model_max_tokens − labels_token_count − safety_margin",
    )
    overlap: int = Field(0, ge=0, description="相邻 Chunk 的字符重叠数")
    safety_margin: int = Field(0, ge=0, description="effective 的 token 安全余量")
    threshold: float = Field(
        0.5, ge=0.0, le=1.0,
        description="送入 model.predict_entities 的 threshold",
    )
    chunk_count: int = Field(..., ge=0, description="Chunk 总数")
    chunks: List[ChunkInfo] = Field(default_factory=list, description="每个 Chunk 的明细")
    attack_auto_routing: Optional[Dict[str, Any]] = Field(
        None,
        description="attack-auto 的语言字符统计、命中条件、实际子模型和配置 hash",
    )


class ExtractRequest(BaseModel):
    model_config = {"protected_namespaces": ()}  # 允许 model_name 字段名

    text: str = Field(
        ...,
        min_length=1,
        max_length=2_000_000,  # pydantic 硬上限;实际运行时由 main.py 读 MAX_TEXT_LENGTH env 做 413 检查
        description="待提取实体的文本。使用 /extract/upload 端点时该字段被文件内容替代",
    )
    labels: List[str] = Field(..., min_length=1, description="实体类型列表（原始标签，可为中文）")
    label_mapping: Optional[Dict[str, str]] = Field(
        None,
        description="标签映射字典，将原始标签映射为标准标签，例如 {'人物': 'PERSON', '地点': 'LOCATION'}",
    )
    model_name: Optional[str] = Field(None, description="指定模型名称，留空使用第一个可用模型")
    reuse_cache: bool = Field(False, description="是否启用结果复用（根据文本和标签查找历史结果）")
    information_json: Dict = Field(default_factory=dict, description="用户自定义信息，用于消息绑定，会在结果中原封不动返回")
    callback: Optional[CallbackConfig] = Field(None, description="回调配置（可选）")

    # ─── 分块相关 (long-text-chunking-ner) ──────────────────────────────
    chunk_enabled: Optional[bool] = Field(
        None,
        description="是否启用分块,留空使用环境变量 CHUNK_ENABLED 的值",
    )
    chunk_overlap: Optional[int] = Field(
        None, ge=0, le=512,
        description="相邻 Chunk 的字符重叠数,留空使用 CHUNK_OVERLAP 默认 32",
    )
    chunk_safety_margin: Optional[int] = Field(
        None, ge=0, le=64,
        description="effective_max_tokens 的安全余量(token 数),留空使用 CHUNK_SAFETY_MARGIN 默认 16",
    )
    threshold: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="predict_entities 的 threshold,留空使用 CHUNK_THRESHOLD 默认 0.5",
    )


class ExtractResponse(BaseModel):
    """Stage 1 响应：提交任务"""
    code: int = 200
    status: str = "成功"
    msg: str = "任务已加入队列"
    task_id: str
    cached: bool = False  # 是否使用了缓存结果
    cached_from: Optional[str] = None  # 缓存来源的 task_id


class EntityResult(BaseModel):
    text: str
    label: str
    raw_label: Optional[str] = None   # 映射前的原始标签
    start: int
    end: int
    score: float

    # ─── 实体标记上下文 (entity-marking) ────────────────────────────────
    marked_context: Optional[str] = Field(
        None,
        description="该实体所在的上下文片段,实体本身用 ⟦label⟧ 包裹;无原文或越界时为 null",
    )

    # ─── ATT&CK 富化字段 (TRAM / attack-llm 专用,其它模型为 null) ─────────
    technique_id: Optional[str] = Field(
        None, description="ATT&CK 技术编号(如 T1059.003);仅 attack 类模型有值",
    )
    technique_name: Optional[str] = Field(
        None, description="ATT&CK 技术名称(来自 techniques.json 富化)",
    )
    tactics: Optional[List[str]] = Field(
        None, description="该技术所属战术 shortname 列表(如 ['execution'])",
    )
    technique_url: Optional[str] = Field(
        None, description="ATT&CK 官方技术页面 URL",
    )
    is_subtechnique: Optional[bool] = Field(
        None, description="是否为子技术(带 .xxx)",
    )
    parent_technique: Optional[str] = Field(
        None, description="父技术编号(子技术才有,如 T1059.003 → T1059)",
    )

    # ─── 分块相关 (long-text-chunking-ner) ──────────────────────────────
    chunk_index: Optional[int] = Field(
        None, ge=0,
        description="产出该实体的 Chunk 序号;历史任务 / 未分块时为 null",
    )
    chunk_char_start: Optional[int] = Field(
        None,
        description="该实体所属 Chunk 在原文中的起始字符偏移",
    )
    chunk_char_end: Optional[int] = Field(
        None,
        description="该实体所属 Chunk 在原文中的结束字符偏移",
    )


class TaskResult(BaseModel):
    """Stage 2 响应：查询结果"""
    model_config = {"protected_namespaces": ()}  # 允许 model_name 字段名

    status: int  # 1=成功, -1=错误
    msg: str
    task_id: str
    model_name: str
    result: Optional[List[EntityResult]] = None
    error_message: Optional[str] = None
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    latency_ms: Optional[float] = None
    information_json: Dict = Field(default_factory=dict)  # 用户自定义信息
    mark_text: Optional[str] = Field(
        None,
        description="整篇文本,每个实体用 ⟦label⟧...⟦/label⟧ 包裹;任务未成功时为 null",
    )
    chunk_metadata: Optional[ChunkMetadata] = Field(
        None,
        description="分块元数据;pending / running / 历史任务时为 null",
    )


class ModelStatsItem(BaseModel):
    model_config = {"protected_namespaces": ()}  # 允许 model_name 字段名

    model_name: str
    total_requests: int
    successful_requests: int
    failed_requests: int
    avg_latency_ms: Optional[float]
    success_rate: float


class StatsResponse(BaseModel):
    models: List[ModelStatsItem]
    total_requests: int
    queue_size: int


class CallbackFailedLog(BaseModel):
    """回调失败记录"""
    id: int
    task_id: str
    callback_url: str
    callback_method: str
    callback_headers: Optional[Dict[str, str]] = None  # 实际发送的完整请求头
    request_body: Optional[Dict] = None                # 实际发送的请求体
    result_data: Optional[Dict] = None                 # 兼容旧字段
    error_message: str
    http_status_code: Optional[int] = None
    failed_at: datetime
    retry_count: int
    last_retry_at: Optional[datetime] = None
    status: str
    created_at: datetime
    callback_kind: Optional[str] = None  # 回调来源：ner / attack / ti / unknown


class CallbackFailedListResponse(BaseModel):
    """回调失败记录列表响应"""
    total: int
    page: int
    page_size: int
    items: List[CallbackFailedLog]


class CallbackSuccessLog(BaseModel):
    """回调成功日志"""
    id: int
    task_id: str
    callback_url: str
    callback_method: str
    http_status_code: int
    response_time_ms: float
    request_headers: Optional[Dict[str, str]] = None
    request_body: Optional[Dict] = None
    response_headers: Optional[Dict[str, str]] = None
    response_body: Optional[str] = None
    created_at: datetime
    callback_kind: Optional[str] = None  # 回调来源：ner / attack / ti / unknown


class CallbackSuccessListResponse(BaseModel):
    """回调成功日志列表响应"""
    total: int
    page: int
    page_size: int
    items: List[CallbackSuccessLog]


class CallbackStats(BaseModel):
    """回调统计"""
    total_callbacks: int
    success_callbacks: int
    failed_callbacks: int
    pending_retries: int
    success_rate: float
    avg_response_time_ms: Optional[float] = None
    failed_by_reason: Dict[str, int] = Field(default_factory=dict)  # 失败原因分布


class CallbackTestRequest(BaseModel):
    """回调测试请求"""
    url: str = Field(..., description="要测试的回调 URL")
    method: str = Field("POST", description="HTTP 方法")
    headers: Optional[Dict[str, str]] = Field(None, description="自定义请求头")
    timeout: int = Field(10, ge=1, le=60, description="超时时间（秒）")


class CallbackTestResponse(BaseModel):
    """回调测试响应"""
    success: bool
    status_code: Optional[int] = None
    response_time_ms: Optional[float] = None
    error_message: Optional[str] = None
    response_body: Optional[str] = None


# ─── 用户认证相关 ─────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50, description="用户名")
    password: str = Field(..., min_length=1, description="密码")


class UserInfo(BaseModel):
    id: int
    username: str
    is_admin: bool
    limit_5h: int
    limit_weekly: int
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserInfo


class UserCreate(BaseModel):
    username: str = Field(..., min_length=2, max_length=50, description="用户名")
    password: str = Field(..., min_length=6, description="密码（至少6位）")
    limit_5h: int = Field(2000, ge=0, description="5小时调用次数限额（0=无限制）")
    limit_weekly: int = Field(100000, ge=0, description="每周调用次数限额（0=无限制）")


class UserUpdate(BaseModel):
    password: Optional[str] = Field(None, min_length=6, description="新密码")
    limit_5h: Optional[int] = Field(None, ge=0, description="5小时调用次数限额")
    limit_weekly: Optional[int] = Field(None, ge=0, description="每周调用次数限额")
    is_active: Optional[bool] = Field(None, description="是否启用")


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, description="旧密码")
    new_password: str = Field(..., min_length=6, description="新密码（至少6位）")


class ApiKeyCreate(BaseModel):
    name: str = Field("", max_length=100, description="Key 用途描述")
    expires_days: int = Field(30, ge=1, le=365, description="过期天数（1-365）")


class ApiKeyInfo(BaseModel):
    id: int
    key_prefix: str
    name: str
    expires_at: Optional[datetime] = None
    is_active: bool
    created_at: Optional[datetime] = None


class ApiKeyCreated(BaseModel):
    """创建 API Key 后的响应——包含完整的原始 Key（仅此一次可见）"""
    id: int
    key: str = Field(..., description="完整 API Key（仅此一次显示，请妥善保管）")
    key_prefix: str
    name: str
    expires_at: Optional[datetime] = None


class UsageStats(BaseModel):
    limit_5h: int
    used_5h: int
    limit_weekly: int
    used_weekly: int



# ─── 缓存复用策略管理 (cache management) ─────────────────────────────────────

class CacheSettings(BaseModel):
    """缓存复用全局策略"""
    enabled: bool = Field(..., description="全局缓存复用开关")
    ttl_seconds: int = Field(..., ge=0, description="缓存有效期（秒），默认 28800（8 小时）")
    refresh_on_hit: bool = Field(..., description="命中缓存时是否刷新过期时间（滑动过期）")


class CacheSettingsUpdate(BaseModel):
    """更新缓存策略，所有字段可选，仅更新提供的字段"""
    enabled: Optional[bool] = Field(None, description="全局缓存复用开关")
    ttl_seconds: Optional[int] = Field(None, ge=0, le=2_592_000, description="缓存有效期（秒），范围 0-2592000（30 天）")
    refresh_on_hit: Optional[bool] = Field(None, description="命中缓存时是否刷新过期时间")


class CacheStats(BaseModel):
    """缓存概览统计"""
    active: int = Field(..., description="当前未过期、可复用的任务数")
    expired: int = Field(..., description="已过期、不再复用的任务数")
    soonest_expire: Optional[datetime] = Field(None, description="最近的过期时间")
    latest_expire: Optional[datetime] = Field(None, description="最远的过期时间")


class CacheEntry(BaseModel):
    """缓存条目（参与复用的任务）。

    提取缓存（NER / ATT&CK）填 model_name / input_text_preview / labels / entity_count；
    联网解析缓存（TI）填 platform / seed_type / seed_value，model_name 为 None。
    """
    task_id: str
    task_hash: Optional[str] = None
    model_name: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    cache_expires_at: Optional[datetime] = None
    expired: bool = False
    input_text_preview: Optional[str] = None
    labels: Optional[List[str]] = None
    entity_count: Optional[int] = None
    platform: Optional[str] = None
    seed_type: Optional[str] = None
    seed_value: Optional[str] = None


class CacheEntryListResponse(BaseModel):
    total: int
    items: List[CacheEntry]

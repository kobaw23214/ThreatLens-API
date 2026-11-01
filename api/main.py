import asyncio
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

# .env 必须在所有本地模块 import 之前加载（models.py 顶层读取 MODELS_ROOT 等环境变量）
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .db import close_db, get_stats, get_task, get_task_result, list_tasks, init_db, insert_task, find_cached_task, get_model_overview
from .models import load_models
from .features import attack_enabled, ner_enabled, ti_enabled
from .manuals import get_manual, list_manual_groups
from .queue_manager import QueueManager
from .callback_manager import CallbackManager
from .chunking import ChunkConfig
from .eml_parser import DEFAULT_EML_LABELS, DEFAULT_EML_MODEL, flatten_eml_text, parse_mail
from .marking import build_marked_payload
from .auth import get_current_user, require_admin, check_rate_limit, hash_password
from .user_routes import auth_router, admin_router
from .ti_expand.routes import router as ti_router, setup_ti
from .schemas import (
    AttackAutoRoutingConfig,
    EntityResult,
    ExtractRequest,
    ExtractResponse,
    ModelStatsItem,
    StatsResponse,
    TaskResult,
    CallbackFailedLog,
    CallbackFailedListResponse,
    CallbackSuccessLog,
    CallbackSuccessListResponse,
    CallbackStats,
    CallbackTestRequest,
    CallbackTestResponse,
)

queue_manager: Optional[QueueManager] = None
callback_manager: Optional[CallbackManager] = None
ti_manager = None  # TiQueueManager，lifespan 中初始化
cache_cleaner = None
loaded_models: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global queue_manager, callback_manager, ti_manager, cache_cleaner, loaded_models

    # 1. 初始化数据库
    init_db(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )

    # 1.5 初始化管理员用户
    from .db import ensure_admin_user
    admin_password = os.environ.get("ADMIN_DEFAULT_PASSWORD", "12345679")
    ensure_admin_user(hash_password(admin_password))

    # 2. 加载模型
    names = [m.strip() for m in os.environ.get("MODELS_ENABLED", "").split(",") if m.strip()]
    loaded_models = load_models(names)
    print(f"\n已加载模型: {list(loaded_models.keys())}\n")
    # 模型名 → model_type 映射，供 admin 缓存管理按 NER / ATT&CK 分类过滤
    app.state.model_types = {
        name: getattr(model, "model_type", "ner") for name, model in loaded_models.items()
    }

    # 3. 启动回调管理器（如果启用）
    callback_enabled = os.environ.get("CALLBACK_ENABLED", "true").lower() == "true"
    if callback_enabled:
        callback_worker_threads = int(os.environ.get("CALLBACK_WORKER_THREADS", 4))
        callback_queue_size = int(os.environ.get("CALLBACK_QUEUE_SIZE", 1000))
        callback_failed_queue_size = int(os.environ.get("CALLBACK_FAILED_QUEUE_SIZE", 500))
        callback_timeout = int(os.environ.get("CALLBACK_TIMEOUT", 30))
        allowed_hosts = os.environ.get("CALLBACK_ALLOWED_HOSTS", "*")
        block_private_ip = os.environ.get("CALLBACK_BLOCK_PRIVATE_IP", "true").lower() == "true"
        
        callback_manager = CallbackManager(
            callback_worker_threads,
            callback_queue_size,
            callback_failed_queue_size,
            callback_timeout,
            allowed_hosts,
            block_private_ip,
        )
        callback_manager.start()
        print(f"回调管理器已启动")

    # 4. 启动任务队列
    worker_threads = int(os.environ.get("WORKER_THREADS", 4))
    max_queue_size = int(os.environ.get("MAX_QUEUE_SIZE", 100))
    queue_manager = QueueManager(max_queue_size, worker_threads)
    if callback_manager:
        queue_manager.set_callback_manager(callback_manager)
    await queue_manager.start(loaded_models)

    # 4.5 启动 TI 联网情报队列（微步/奇安信，每平台串行 worker）
    # 功能模块开关:TI 关闭时完全不构造管理器/worker(ti_manager 保持 None,
    # shutdown 与 /events 处的引用均已做 None 判断)
    if ti_enabled():
        from .ti_expand.config import load_config as load_ti_config
        from .ti_expand.manager import TiQueueManager
        ti_cfg = load_ti_config()
        ti_manager = TiQueueManager(ti_cfg, callback_manager=callback_manager)
        setup_ti(ti_manager, ti_cfg)
        # TI 队列/worker 状态变化 → 复用 NER 队列的 SSE 广播通道推给前端
        ti_manager.set_status_hook(queue_manager.broadcast_status)
        if ti_cfg.enabled:
            await ti_manager.start()
            print("TI 联网情报队列已启动")

    # 5. 启动后台缓存清理任务（物理删除过期缓存任务行）
    cache_cleanup_enabled = os.environ.get("CACHE_CLEANUP_ENABLED", "true").lower() == "true"
    if cache_cleanup_enabled:
        from .cache_cleaner import CacheCleaner
        cache_cleanup_interval = int(os.environ.get("CACHE_CLEANUP_INTERVAL", 3600))
        cache_cleaner = CacheCleaner(cache_cleanup_interval)
        cache_cleaner.start()
        print(f"缓存清理任务已启动，间隔 {cache_cleaner.interval} 秒")

    yield

    # 关闭
    if cache_cleaner:
        await cache_cleaner.stop()
    if ti_manager:
        await ti_manager.shutdown()
    queue_manager.shutdown()
    if callback_manager:
        callback_manager.shutdown()
    close_db()


app = FastAPI(title="NER API", version="1.0.0", lifespan=lifespan)

# 挂载用户认证 / 管理员路由
app.include_router(auth_router)
app.include_router(admin_router)
# 功能模块开关:TI 关闭时不挂载路由(全部 /ti/* 返回 404)
if ti_enabled():
    app.include_router(ti_router)

_cors_origins = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]
# 如果开启了全部放权 (*)，则根据 FastAPI/CORS 规范，不能将 allow_credentials 设为 True，否则启动会报错。
# 我们的服务不依赖 Cookie 等凭证，因此如果是星号则自动将其设为 False 即可。
_allow_credentials = True
if "*" in _cors_origins:
    _allow_credentials = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=_allow_credentials,
)


# ─── 工具函数 ────────────────────────────────────────────────────────────────

def _env_bool(key: str, default: bool) -> bool:
    """解析 .env 中的布尔值,只认 true/false 字面量(大小写不敏感)。"""
    raw = os.environ.get(key)
    if raw is None:
        return default
    s = str(raw).strip().lower()
    if s == "true":
        return True
    if s == "false":
        return False
    return default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return float(str(raw).strip())
    except (ValueError, TypeError):
        return default


def _model_feature_on(model) -> bool:
    """模型实例所属功能模块(NER/ATT&CK)的开关是否打开。"""
    if getattr(model, "model_type", "ner") == "attack":
        return attack_enabled()
    return ner_enabled()


def build_chunk_cfg(req: ExtractRequest) -> ChunkConfig:
    """
    合并请求字段与环境变量得到最终 ChunkConfig。

    优先级:请求体字段显式提供(非 None)时用请求值,否则回落到环境变量默认值。

    环境变量默认值(对应 Requirement 3.1):
      CHUNK_ENABLED       = true
      CHUNK_OVERLAP       = 32
      CHUNK_SAFETY_MARGIN = 16
      CHUNK_THRESHOLD     = 0.5
    """
    return ChunkConfig(
        enabled=(
            req.chunk_enabled
            if req.chunk_enabled is not None
            else _env_bool("CHUNK_ENABLED", True)
        ),
        overlap=(
            req.chunk_overlap
            if req.chunk_overlap is not None
            else _env_int("CHUNK_OVERLAP", 32)
        ),
        safety_margin=(
            req.chunk_safety_margin
            if req.chunk_safety_margin is not None
            else _env_int("CHUNK_SAFETY_MARGIN", 16)
        ),
        threshold=(
            req.threshold
            if req.threshold is not None
            else _env_float("CHUNK_THRESHOLD", 0.5)
        ),
    )


def compute_task_hash(
    text: str,
    labels: List[str],
    model_name: Optional[str] = None,
    chunk_cfg: Optional[ChunkConfig] = None,
    model_cache_version: Optional[str] = None,
) -> str:
    """
    计算任务内容哈希。

    哈希输入 = text || json.dumps(sorted(labels)) || model_name || json.dumps(chunk_cfg, sort_keys=True)

    model_name 与 chunk_cfg 作为可选参数,保持对旧调用点的向后兼容;
    新代码都传全 4 个参数。任何参数变化都会产生不同 hash,避免错误命中缓存
    (Requirement 9.3)。

    Returns:
        32 位 MD5 十六进制字符串。
    """
    # 标签排序确保相同标签不同顺序产生相同哈希
    sorted_labels = sorted(labels)
    content = f"{text}||{json.dumps(sorted_labels, ensure_ascii=False)}"
    if model_name is not None:
        content += f"||{model_name}"
    if chunk_cfg is not None:
        content += "||" + json.dumps(
            {
                "e": bool(chunk_cfg.enabled),
                "o": int(chunk_cfg.overlap),
                "s": int(chunk_cfg.safety_margin),
                "t": float(chunk_cfg.threshold),
            },
            sort_keys=True,
        )
    if model_cache_version is not None:
        content += f"||cache-version:{model_cache_version}"
    return hashlib.md5(content.encode("utf-8")).hexdigest()


# ─── 路由 ────────────────────────────────────────────────────────────────────

# ─── 文件上传支持 (extract/upload) ─────────────────────────────────────────

# 允许的文件扩展名(全部视为纯文本类文件,最终统一解码成 str)
_ALLOWED_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst",
    ".log", ".csv", ".tsv",
    ".json", ".jsonl", ".ndjson",
    ".xml", ".yaml", ".yml", ".ini", ".cfg", ".toml",
    ".html", ".htm",
    ".srt", ".vtt",
}

# 允许的 PDF 扩展名。PDF 是二进制格式,走独立的"提取文字层"分支,
# 不参与文本编码解码,也不做二进制特征检测(否则会被误拦)。
_ALLOWED_PDF_EXTENSIONS = {".pdf"}

# 对外暴露的全部可上传扩展名(文本 + PDF),用于校验与错误提示。
_ALLOWED_UPLOAD_EXTENSIONS = _ALLOWED_TEXT_EXTENSIONS | _ALLOWED_PDF_EXTENSIONS

# 单文件大小上限(字节)。默认 10 MB,可通过环境变量 UPLOAD_MAX_BYTES 覆盖。
_UPLOAD_MAX_BYTES = int(os.environ.get("UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))

# 二进制特征样本字节数:读取头部这么多字节扫描 NULL 字节 / 控制字符判断是否为二进制
_BINARY_SNIFF_BYTES = 4096


def _looks_binary(data: bytes) -> bool:
    """
    启发式检测:文件是否为二进制(非文本)。
      - 头部出现 NULL 字节 → 几乎可以确认是二进制
      - 头部非文本控制字符 (<32 且非 tab/lf/cr) 占比 > 30% → 视为二进制
    该检查只看头 _BINARY_SNIFF_BYTES 字节,时间复杂度 O(1)。
    """
    if not data:
        return False
    sample = data[:_BINARY_SNIFF_BYTES]
    if b"\x00" in sample:
        return True
    text_like = {9, 10, 13}  # tab, LF, CR
    nonprintable = sum(1 for b in sample if b < 32 and b not in text_like)
    return nonprintable / max(1, len(sample)) > 0.30


def _decode_text_bytes(data: bytes) -> str:
    """
    按常见编码依次尝试解码文件内容。失败时抛 ValueError。
    优先级:UTF-8 (BOM 感知) → UTF-16 (BOM 感知) → GB18030 → Latin-1 最后兜底。
    """
    # UTF-8 / UTF-8 BOM
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # UTF-16 BOM
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass

    # GB18030 (覆盖 GBK / GB2312)
    try:
        return data.decode("gb18030")
    except UnicodeDecodeError:
        pass

    # 最后的兜底:latin-1 永远不会抛异常,但可能乱码 → 调用方应把这当作可疑
    try:
        return data.decode("latin-1")
    except UnicodeDecodeError as exc:
        raise ValueError(f"无法识别文件编码: {exc}") from exc


def _extract_pdf_text(data: bytes) -> str:
    """
    从 PDF 字节流提取文字层,返回纯文本。

    使用 PyMuPDF(import fitz)。只提取原生文字层,不做 OCR:
      - 电子版 / 可复制文字的 PDF → 正常提取
      - 扫描件 / 纯图片 PDF(无文字层) → 提取结果为空,由调用方报错提示

    失败时抛 ValueError(损坏文件 / 加密无法读取 / 非法 PDF)。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底提示
        raise ValueError(
            "服务端未安装 PDF 解析库 PyMuPDF,无法处理 PDF。"
            "请在部署环境执行 pip install pymupdf"
        ) from exc

    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            # 加密且需要密码的 PDF 无法在无密码情况下提取
            if doc.needs_pass:
                raise ValueError("PDF 已加密,需要密码,无法提取文本")
            parts = [page.get_text() for page in doc]
    except ValueError:
        raise
    except Exception as exc:  # PyMuPDF 抛的多为底层异常,统一转 ValueError
        raise ValueError(f"PDF 解析失败,文件可能已损坏或不是有效的 PDF: {exc}") from exc

    return "\n".join(parts)


def _validate_and_read_upload(upload: UploadFile, raw_bytes: bytes) -> str:
    """
    校验上传文件并解码为文本。失败抛 HTTPException(400)。
    校验维度:
      1. 文件非空且大小 <= _UPLOAD_MAX_BYTES
      2. 扩展名在白名单(文本格式 或 .pdf)
      3. PDF → 提取文字层(PyMuPDF);其余 → 二进制特征检测 + 文本解码
      4. 提取/解码后内容非空白

    返回:统一的纯文本字符串(PDF 提取出的文本 / 文本文件解码后的内容)。
    """
    filename = upload.filename or ""
    size = len(raw_bytes)

    if size == 0:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if size > _UPLOAD_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"上传文件过大 ({size} 字节),上限 {_UPLOAD_MAX_BYTES} 字节",
        )

    ext = Path(filename).suffix.lower()
    if ext not in _ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"不支持的文件类型 '{ext or '无扩展名'}'。"
                f"支持的格式: PDF 及纯文本格式 "
                f"({', '.join(sorted(_ALLOWED_UPLOAD_EXTENSIONS))})"
            ),
        )

    # ── PDF 分支:提取文字层,不走二进制检测和编码解码 ──────────────────
    if ext in _ALLOWED_PDF_EXTENSIONS:
        try:
            text = _extract_pdf_text(raw_bytes)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if not text.strip():
            raise HTTPException(
                status_code=400,
                detail=(
                    "未能从 PDF 中提取到文字。该 PDF 可能是扫描件或纯图片(无文字层),"
                    "暂不支持 OCR 识别,请上传包含可复制文字的 PDF。"
                ),
            )
        return text

    # ── 文本文件分支:二进制特征检测 + 多编码解码 ──────────────────────
    if _looks_binary(raw_bytes):
        raise HTTPException(
            status_code=400,
            detail="文件内容看起来不是纯文本(检测到二进制特征),请上传真实文本文件",
        )

    try:
        text = _decode_text_bytes(raw_bytes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"文件解码失败: {exc}") from exc

    # 解码后仍可能是空串
    if not text.strip():
        raise HTTPException(status_code=400, detail="文件内容为空白")

    return text


def _parse_json_field(raw: Optional[str], field: str, default):
    """
    multipart/form-data 表单字段解析 JSON。None / 空字符串 → default。
    """
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"表单字段 {field} 不是合法 JSON: {exc.msg} (pos {exc.pos})",
        ) from exc


async def _submit_extract_task(req: ExtractRequest, user_id: Optional[int] = None, source: str = "text") -> ExtractResponse:
    """
    /extract 和 /extract/upload 共享的核心:校验模型 → 构建 chunk_cfg / task_hash →
    缓存命中直接复用,否则入队并返回 202。
    """
    if not loaded_models:
        raise HTTPException(status_code=503, detail="没有可用模型，请检查 MODELS_ENABLED 配置")

    model_name = req.model_name or next(iter(loaded_models))
    if model_name not in loaded_models:
        raise HTTPException(status_code=400, detail=f"模型 '{model_name}' 未加载")

    # 功能模块开关守卫:模型所属功能被关闭时拒绝提交(风格同 TI 的 _require_ready)
    if not _model_feature_on(loaded_models[model_name]):
        if getattr(loaded_models[model_name], "model_type", "ner") == "attack":
            raise HTTPException(status_code=503, detail="ATT&CK 攻击技术映射功能未启用（ATTACK_ENABLED=false）")
        raise HTTPException(status_code=503, detail="NER 实体识别功能未启用（NER_ENABLED=false）")

    chunk_cfg = build_chunk_cfg(req)
    model_cache_version = getattr(loaded_models[model_name], "cache_version", None)
    task_hash = compute_task_hash(
        req.text, req.labels, model_name, chunk_cfg, model_cache_version,
    )

    callback_config = None
    if req.callback:
        callback_config = req.callback.dict()

    # 全局缓存复用策略（管理员可在缓存管理页调整）
    from .db import get_cache_settings, refresh_cache_expiry, update_task_done
    cache_settings = get_cache_settings()
    # 本次任务是否参与缓存复用：请求显式开启 且 全局开关打开
    cache_active = bool(req.reuse_cache) and cache_settings["enabled"]
    cache_ttl_seconds = cache_settings["ttl_seconds"] if cache_active else None

    # 缓存复用
    if cache_active:
        cached = find_cached_task(task_hash, model_name)
        if cached:
            # 命中策略：刷新原任务过期时间为 NOW() + ttl（滑动过期）
            if cache_settings["refresh_on_hit"]:
                refresh_cache_expiry(str(cached["task_id"]), cache_settings["ttl_seconds"])

            task_id = str(uuid.uuid4())
            insert_task(
                task_id, model_name, req.text, req.labels,
                req.label_mapping, task_hash, req.information_json, callback_config,
                user_id=user_id, source=source, cache_hit=True,
            )

            cached_chunk_metadata = cached.get("chunk_metadata")
            # 复用产生的新任务同样写入 TTL，使其后续也可被复用
            update_task_done(
                task_id,
                cached["entities"],
                cached.get("latency_ms", 0),
                chunk_metadata=cached_chunk_metadata,
                cache_ttl_seconds=cache_settings["ttl_seconds"],
            )

            if callback_config and callback_manager:
                cached_mark_text, cached_marked_entities = build_marked_payload(
                    req.text, cached["entities"]
                )
                result_data = {
                    "status": 1,
                    "msg": "任务执行成功（缓存）",
                    "task_id": task_id,
                    "model_name": model_name,
                    "result": cached_marked_entities,
                    "error_message": None,
                    "created_at": None,
                    "finished_at": None,
                    "latency_ms": cached.get("latency_ms", 0),
                    "information_json": req.information_json,
                    "mark_text": cached_mark_text,
                    "chunk_metadata": cached_chunk_metadata,
                }
                callback_manager.enqueue_callback(task_id, callback_config, result_data)

            return ExtractResponse(
                code=200,
                status="成功",
                msg="使用历史缓存结果",
                task_id=task_id,
                cached=True,
                cached_from=str(cached["task_id"]),
            )

    # 新建任务
    task_id = str(uuid.uuid4())
    insert_task(
        task_id, model_name, req.text, req.labels,
        req.label_mapping, task_hash, req.information_json, callback_config,
        user_id=user_id, source=source,
    )

    queued = await queue_manager.enqueue(
        task_id, req.text, req.labels, model_name, req.label_mapping, callback_config,
        chunk_cfg=chunk_cfg.to_dict(),
        cache_ttl_seconds=cache_ttl_seconds,
    )
    if not queued:
        raise HTTPException(status_code=503, detail="队列已满，请稍后重试")

    return ExtractResponse(
        code=200,
        status="成功",
        msg="任务已加入队列",
        task_id=task_id,
    )


@app.post("/extract", response_model=ExtractResponse, status_code=202)
async def extract(req: ExtractRequest, request: Request):
    """提交实体提取任务，立即返回 task_id。支持结果复用和回调。"""
    # ── 认证 + 限额检查 ──────────────────────────────────────────────────
    user = await get_current_user(request)
    check_rate_limit(user)
    from .db import log_usage
    log_usage(user["id"], "/extract")

    # ── 文本长度限制 (MAX_TEXT_LENGTH env，默认 50000) ──────────────────────
    max_text_len = int(os.environ.get("MAX_TEXT_LENGTH", "50000"))
    if len(req.text) > max_text_len:
        raise HTTPException(
            status_code=413,
            detail=(
                f"text 长度 {len(req.text):,} 字符超过上限 {max_text_len:,}。"
                f"大文本请改用 POST /extract/upload 上传文件"
                f"(支持 .txt/.md/.csv/.json 等文本格式及 .pdf,最大 {_UPLOAD_MAX_BYTES // 1024 // 1024} MB)。"
                f"如需调整上限,可在 .env 中修改 MAX_TEXT_LENGTH。"
            ),
        )
    return await _submit_extract_task(req, user_id=user["id"], source="text")


@app.post("/extract/upload", response_model=ExtractResponse, status_code=202)
async def extract_upload(
    request: Request,
    file: UploadFile = File(..., description="文本文件或 PDF。支持 .txt/.md/.csv/.json 等文本格式及 .pdf(提取文字层),最大 10MB。"),
    labels: str = Form(..., description="实体标签 JSON 数组字符串,如 '[\"人物\",\"地点\"]'"),
    model_name: Optional[str] = Form(None, description="模型名称,留空自动选择"),
    label_mapping: Optional[str] = Form(None, description="标签映射 JSON 字符串"),
    reuse_cache: bool = Form(False, description="是否启用缓存复用"),
    information_json: Optional[str] = Form(None, description="自定义信息 JSON 字符串"),
    callback: Optional[str] = Form(None, description="回调配置 JSON 字符串"),
    chunk_enabled: Optional[bool] = Form(None),
    chunk_overlap: Optional[int] = Form(None),
    chunk_safety_margin: Optional[int] = Form(None),
    threshold: Optional[float] = Form(None),
):
    """
    通过 multipart/form-data 上传文本文件进行实体提取。

    行为:
      - 文件必填:如果没传 file 字段会直接 422
      - 文件格式必须是纯文本(.txt/.md/.csv/.json 等,见 _ALLOWED_TEXT_EXTENSIONS)
      - 文件内容会替代 JSON 接口中的 text 字段
      - 其他字段(labels / model_name / label_mapping 等)通过 Form 字段携带,
        其中 labels / label_mapping / information_json / callback 是 JSON 字符串

    兼容性:
      /extract 接口不变,调用方不上传文件时仍走 JSON 接口。
    """
    # ── 认证 + 限额检查 ──────────────────────────────────────────────────
    user = await get_current_user(request)
    check_rate_limit(user)
    from .db import log_usage
    log_usage(user["id"], "/extract/upload")

    # 先读取原始字节,再做所有校验,避免半途抛异常时文件指针未关闭
    raw_bytes = await file.read()
    try:
        text_content = _validate_and_read_upload(file, raw_bytes)
    finally:
        await file.close()

    # 解析 multipart 中的 JSON 字符串字段
    labels_list = _parse_json_field(labels, "labels", [])
    if not isinstance(labels_list, list) or not all(isinstance(x, str) for x in labels_list) or not labels_list:
        raise HTTPException(status_code=422, detail="labels 必须是非空字符串数组")

    label_mapping_obj = _parse_json_field(label_mapping, "label_mapping", None)
    information_json_obj = _parse_json_field(information_json, "information_json", {}) or {}
    callback_obj = _parse_json_field(callback, "callback", None)

    # 组装 ExtractRequest(复用现有 pydantic 校验:越界参数直接 422)
    try:
        req = ExtractRequest(
            text=text_content,
            labels=labels_list,
            label_mapping=label_mapping_obj,
            model_name=model_name,
            reuse_cache=reuse_cache,
            information_json=information_json_obj,
            callback=callback_obj,
            chunk_enabled=chunk_enabled,
            chunk_overlap=chunk_overlap,
            chunk_safety_margin=chunk_safety_margin,
            threshold=threshold,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"请求参数无效: {exc}") from exc

    return await _submit_extract_task(req, user_id=user["id"], source="upload")


# ─── 邮件解析 + IOC 提取 (extract/eml) ───────────────────────────────────────
_ALLOWED_EML_EXTENSIONS = {".eml", ".msg"}


@app.post("/extract/eml", response_model=ExtractResponse, status_code=202)
async def extract_eml(
    request: Request,
    file: UploadFile = File(..., description="邮件文件(.eml / .msg),最大 10MB"),
    labels: Optional[str] = Form(None, description="实体标签 JSON 数组字符串,留空使用内置 IOC 标签集(邮箱/域名/URL/IP/哈希/CVE 等)"),
    model_name: Optional[str] = Form(None, description=f"模型名称,默认 {DEFAULT_EML_MODEL}"),
    reuse_cache: bool = Form(False, description="是否启用缓存复用"),
    information_json: Optional[str] = Form(None, description="自定义信息 JSON 字符串"),
    callback: Optional[str] = Form(None, description="回调配置 JSON 字符串"),
):
    """
    上传邮件文件(.eml / .msg):.eml 用 mail-parser、.msg 用 extract-msg
    解析成同结构的 JSON,再把关键字段
    (头部 / Received 链路 / 正文 / 附件名)展平为文本,走异步队列做 IOC 提取。

    与 /extract/upload 的差异:
      - 仅接受 .eml / .msg;labels 可留空(内置 IOC 标签集);默认模型为 ioc-finder
      - 解析出的完整邮件 JSON(附件 payload 已剥离,保留 sha256/size)存入
        information_json.eml,随 /tasks/{id}/result 一并返回
    """
    # ── 认证 + 限额检查 ──────────────────────────────────────────────────
    user = await get_current_user(request)
    check_rate_limit(user)
    from .db import log_usage
    log_usage(user["id"], "/extract/eml")

    # 功能模块开关:IOC 邮件解析归 NER,关闭时拒绝
    if not ner_enabled():
        raise HTTPException(status_code=503, detail="NER 实体识别功能未启用（NER_ENABLED=false）")

    raw_bytes = await file.read()
    try:
        filename = file.filename or ""
        size = len(raw_bytes)
        if size == 0:
            raise HTTPException(status_code=400, detail="上传文件为空")
        if size > _UPLOAD_MAX_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"上传文件过大 ({size} 字节),上限 {_UPLOAD_MAX_BYTES} 字节",
            )
        ext = Path(filename).suffix.lower()
        if ext not in _ALLOWED_EML_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型 '{ext or '无扩展名'}',仅支持 .eml / .msg 邮件文件",
            )
        try:
            eml_json = parse_mail(raw_bytes, filename=filename, ext=ext)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        text_content = flatten_eml_text(eml_json)
    finally:
        await file.close()

    # labels 留空 → 内置 IOC 标签集;显式传入则与 /extract/upload 同样校验
    labels_list = _parse_json_field(labels, "labels", None) if labels else None
    if labels_list is None:
        labels_list = list(DEFAULT_EML_LABELS)
    if not isinstance(labels_list, list) or not all(isinstance(x, str) for x in labels_list) or not labels_list:
        raise HTTPException(status_code=422, detail="labels 必须是非空字符串数组")

    information_json_obj = _parse_json_field(information_json, "information_json", {}) or {}
    # 邮件解析 JSON 随任务结果返回(information_json 会在结果中原样回传)
    information_json_obj["eml"] = eml_json
    callback_obj = _parse_json_field(callback, "callback", None)

    try:
        req = ExtractRequest(
            text=text_content,
            labels=labels_list,
            model_name=model_name or DEFAULT_EML_MODEL,
            reuse_cache=reuse_cache,
            information_json=information_json_obj,
            callback=callback_obj,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"请求参数无效: {exc}") from exc

    return await _submit_extract_task(req, user_id=user["id"], source="eml")


@app.get("/tasks")
async def list_tasks_api(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    model_name: Optional[str] = None,
    model_names: List[str] = Query(default=[]),
    status: Optional[str] = None,
    user_id: Optional[int] = Query(None, description="过滤特定用户的任务（仅管理员可用）"),
):
    """分页查询任务列表。"""
    current_user = await get_current_user(request)
    if not current_user.get("is_admin"):
        target_user_id = current_user["id"]
    else:
        target_user_id = user_id

    if page < 1:       page = 1
    if page_size < 1:  page_size = 10
    if page_size > 100: page_size = 100
    return list_tasks(
        page, page_size,
        model_name or None,
        model_names or None,
        status or None,
        user_id=target_user_id
    )


@app.get("/tasks/{task_id}/result")
async def get_task_result_detail(task_id: str, request: Request):
    """获取任务的完整结果和实体统计，用于前端图表展示。"""
    await get_current_user(request)
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    
    task_status = task["status"]
    
    if task_status != "done":
        # 任务未完成
        if task_status == "failed":
            status_code = -1
            msg = f"任务执行失败: {task.get('error_msg', '未知错误')}"
        elif task_status == "running":
            status_code = 0
            msg = "任务执行中"
        else:  # pending
            status_code = 0
            msg = "任务等待中"
        
        return {
            "status": status_code,
            "msg": msg,
            "task_id": task_id,
            "entities": [],
            "entity_stats": {},
            "input_text": task.get("input_text"),
            "mark_text": None,
            "labels": task.get("labels"),
            "information_json": task.get("information_json") or {},
            "chunk_metadata": None,
        }

    # 任务已完成
    result_row = get_task_result(task_id)
    entities = result_row["entities"] if result_row else []
    input_text = task.get("input_text")
    mark_text, marked_entities = build_marked_payload(input_text, entities)
    return {
        "status": 1,
        "msg": "任务执行成功",
        "task_id": task_id,
        "model_name": task["model_name"],
        "latency_ms": task.get("latency_ms"),
        "entities": marked_entities,
        "entity_stats": result_row["entity_stats"] if result_row else {},
        "label_mapping": task.get("label_mapping"),
        "input_text": input_text,
        "mark_text": mark_text,
        "labels": task.get("labels"),
        "information_json": task.get("information_json") or {},
        "chunk_metadata": result_row.get("chunk_metadata") if result_row else None,
    }


@app.get("/tasks/{task_id}")
async def get_task_status(task_id: str, request: Request, action: Optional[str] = None):
    """查询任务状态和提取结果，或删除任务。"""
    await get_current_user(request)
    print(f"[DEBUG] 收到请求 - task_id: {task_id}, action: {action}")  # 调试日志
    
    # 如果有 action=delete 参数，执行删除操作
    if action == "delete":
        from .db import delete_task as db_delete_task
        print(f"[DEBUG] 尝试删除任务: {task_id}")  # 调试日志
        deleted = db_delete_task(task_id)
        print(f"[DEBUG] 删除结果: {deleted}")  # 调试日志
        if not deleted:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"message": "任务已删除", "task_id": task_id}
    
    # 否则返回任务状态
    print(f"[DEBUG] 查询任务状态: {task_id}")  # 调试日志
    row = get_task(task_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")

    result = None
    mark_text = None
    if row.get("entities"):
        input_text = row.get("input_text") or ""
        mark_text, marked_entities = build_marked_payload(input_text, row["entities"])
        result = [EntityResult(**e) for e in marked_entities]

    # 根据任务状态设置 status 和 msg
    task_status = row["status"]
    if task_status == "done":
        status_code = 1
        msg = "任务执行成功"
    elif task_status == "failed":
        status_code = -1
        msg = f"任务执行失败: {row.get('error_msg', '未知错误')}"
    elif task_status == "running":
        status_code = 0
        msg = "任务执行中"
    else:  # pending
        status_code = 0
        msg = "任务等待中"

    return TaskResult(
        status=status_code,
        msg=msg,
        task_id=str(row["task_id"]),
        model_name=row["model_name"],
        result=result,
        error_message=row.get("error_msg"),
        created_at=row.get("created_at"),
        finished_at=row.get("finished_at"),
        latency_ms=row.get("latency_ms"),
        information_json=row.get("information_json") or {},
        mark_text=mark_text,
        chunk_metadata=row.get("chunk_metadata"),
    )


@app.get("/stats", response_model=StatsResponse)
async def stats(request: Request, user_id: Optional[int] = Query(None, description="过滤特定用户（仅管理员可用）")):
    """返回各模型的聚合统计数据。"""
    current_user = await get_current_user(request)
    if not current_user.get("is_admin"):
        target_user_id = current_user["id"]
    else:
        target_user_id = user_id

    rows = get_stats(user_id=target_user_id)
    model_stats: List[ModelStatsItem] = []
    total = 0
    for row in rows:
        # 功能模块开关:不返回被关闭调用台的模型统计(未加载模型按 ner 计,与 overview 同口径)
        m = loaded_models.get(row["model_name"])
        is_attack = m is not None and getattr(m, "model_type", "ner") == "attack"
        if is_attack and not attack_enabled():
            continue
        if not is_attack and not ner_enabled():
            continue
        t = int(row["total_requests"])
        s = int(row["successful_requests"])
        f = int(row["failed_requests"])
        total += t
        model_stats.append(
            ModelStatsItem(
                model_name=row["model_name"],
                total_requests=t,
                successful_requests=s,
                failed_requests=f,
                avg_latency_ms=float(row["avg_latency_ms"]) if row["avg_latency_ms"] else None,
                success_rate=round(s / t, 4) if t > 0 else 0.0,
            )
        )
    return StatsResponse(
        models=model_stats,
        total_requests=total,
        queue_size=queue_manager.queue.qsize() if queue_manager else 0,
    )


# ─── 模型统计增强（NER / ATT&CK 概览） ───────────────────────────────────────
# 前端 10s 轮询，聚合 SQL 走短 TTL 缓存防全表扫描压力
_overview_cache: dict = {}  # key=f"{user_id}:{days}" -> (monotonic_ts, payload)
_OVERVIEW_CACHE_TTL = 10.0


@app.get("/stats/models/overview")
async def stats_models_overview(
    request: Request,
    days: int = Query(30, ge=1, le=365, description="趋势与标签分布的时间窗口（天）"),
    user_id: Optional[int] = Query(None, description="过滤特定用户（仅管理员可用）"),
):
    """
    NER / ATT&CK 统计增强：按模型类型分组的概览卡、按天趋势（成功/失败/缓存命中）、
    模型调用分布、来源渠道分布、实体标签分布、auto 路由实际子模型分布。
    """
    import time as _time

    current_user = await get_current_user(request)
    if not current_user.get("is_admin"):
        target_user_id = current_user["id"]
    else:
        target_user_id = user_id

    cache_key = f"{target_user_id}:{days}"
    now = _time.monotonic()
    hit = _overview_cache.get(cache_key)
    if hit and now - hit[0] < _OVERVIEW_CACHE_TTL:
        return hit[1]

    # 模型类型映射：与前端 splitModels 同口径——非 attack 一律归 ner 组
    type_map = {
        name: ("attack" if getattr(m, "model_type", "ner") == "attack" else "ner")
        for name, m in loaded_models.items()
    }

    def group_of(model_name: str) -> str:
        return type_map.get(model_name, "ner")

    raw = get_model_overview(user_id=target_user_id, days=days)

    # 功能模块开关:剔除被关闭调用台的模型数据(保留 ner/attack 分组结构,只过滤内容)
    if not (ner_enabled() and attack_enabled()):
        def _keep(model_name: str) -> bool:
            return attack_enabled() if group_of(model_name) == "attack" else ner_enabled()
        raw["models"] = [r for r in raw["models"] if _keep(r["model_name"])]
        raw["routed"] = [r for r in raw["routed"] if _keep(r["model_name"])]
        raw["daily"] = [r for r in raw["daily"] if _keep(r["model_name"])]
        raw["sources"] = [r for r in raw["sources"] if _keep(r["model_name"])]
        raw["labels"] = [r for r in raw["labels"] if _keep(r["model_name"])]

    groups: dict = {
        g: {
            "summary": {
                "today_calls": 0, "today_success": 0, "today_failed": 0,
                "today_cache_hits": 0, "total_requests": 0, "success_rate": 0.0,
                "avg_latency_ms": None, "cache_hit_rate": 0.0,
            },
            "daily_trend": [], "model_dist": [], "source_dist": [],
            "label_dist": [], "models": [],
        }
        for g in ("ner", "attack")
    }
    # summary 加权重算用的临时累加器
    acc = {g: {"succ": 0, "cache": 0, "lat_ms": 0.0} for g in groups}

    routed_by_model: dict = {}
    for r in raw["routed"]:
        routed_by_model.setdefault(r["model_name"], []).append(
            {"routed_model": r["routed_model"], "count": int(r["count"])}
        )

    # ── 每模型明细 + summary / model_dist ────────────────────────────────
    for row in raw["models"]:
        gname = group_of(row["model_name"])
        g = groups[gname]
        total = int(row["total_requests"])
        succ = int(row["successful_requests"])
        cache_hits = int(row["cache_hits"])
        avg_lat = float(row["avg_latency_ms"]) if row["avg_latency_ms"] else None
        g["models"].append({
            "model_name": row["model_name"],
            "total_requests": total,
            "successful_requests": succ,
            "failed_requests": int(row["failed_requests"]),
            "success_rate": round(succ / total, 4) if total else 0.0,
            "cache_hits": cache_hits,
            "cache_hit_rate": round(cache_hits / total, 4) if total else 0.0,
            "avg_latency_ms": avg_lat,
            "p95_latency_ms": float(row["p95_latency_ms"]) if row["p95_latency_ms"] else None,
            "avg_entities": round(float(row["avg_entities"]), 1) if row["avg_entities"] else None,
            "avg_input_len": round(float(row["avg_input_len"])) if row["avg_input_len"] else None,
            "today_calls": int(row["today_calls"]),
            "last_called_at": row["last_called_at"].isoformat() if row["last_called_at"] else None,
            "routed": sorted(routed_by_model.get(row["model_name"], []),
                             key=lambda x: -x["count"]),
        })
        g["model_dist"].append({"model_name": row["model_name"], "count": total})
        s = g["summary"]
        s["today_calls"] += int(row["today_calls"])
        s["today_success"] += int(row["today_success"])
        s["today_failed"] += int(row["today_failed"])
        s["today_cache_hits"] += int(row["today_cache_hits"])
        s["total_requests"] += total
        a = acc[gname]
        a["succ"] += succ
        a["cache"] += cache_hits
        if avg_lat is not None:
            a["lat_ms"] += avg_lat * succ  # 按成功数加权

    for gname, g in groups.items():
        s, a = g["summary"], acc[gname]
        s["success_rate"] = round(a["succ"] / s["total_requests"], 4) if s["total_requests"] else 0.0
        s["cache_hit_rate"] = round(a["cache"] / s["total_requests"], 4) if s["total_requests"] else 0.0
        s["avg_latency_ms"] = round(a["lat_ms"] / a["succ"], 1) if a["succ"] else None
        g["model_dist"].sort(key=lambda x: -x["count"])

    # ── 按天趋势 ─────────────────────────────────────────────────────────
    trend_acc = {g: {} for g in groups}
    for r in raw["daily"]:
        d = trend_acc[group_of(r["model_name"])].setdefault(
            r["day"], {"day": r["day"], "success": 0, "fail": 0, "cache": 0})
        d["success"] += int(r["success"])
        d["fail"] += int(r["fail"])
        d["cache"] += int(r["cache"])
    for gname, by_day in trend_acc.items():
        groups[gname]["daily_trend"] = [by_day[k] for k in sorted(by_day)]

    # ── 来源渠道分布 ─────────────────────────────────────────────────────
    src_acc = {g: {} for g in groups}
    for r in raw["sources"]:
        gname = group_of(r["model_name"])
        src_acc[gname][r["source"]] = src_acc[gname].get(r["source"], 0) + int(r["count"])
    for gname, by_src in src_acc.items():
        groups[gname]["source_dist"] = [
            {"source": k, "count": v} for k, v in sorted(by_src.items(), key=lambda x: -x[1])
        ]

    # ── 实体标签分布（近 N 天，Top 15）──────────────────────────────────
    lab_acc = {g: {} for g in groups}
    for r in raw["labels"]:
        gname = group_of(r["model_name"])
        lab_acc[gname][r["label"]] = lab_acc[gname].get(r["label"], 0) + int(r["count"])
    for gname, by_lab in lab_acc.items():
        groups[gname]["label_dist"] = [
            {"label": k, "count": v}
            for k, v in sorted(by_lab.items(), key=lambda x: -x[1])[:15]
        ]

    payload = {"window_days": days, "groups": groups}
    _overview_cache[cache_key] = (now, payload)
    return payload


@app.get("/models")
async def get_models():
    """返回已加载模型及实例声明的调用台类型。"""
    # 功能模块开关:不返回被关闭调用台的模型
    models_info = [
        {"name": name, "type": getattr(model, "model_type", "ner")}
        for name, model in loaded_models.items()
        if _model_feature_on(model)
    ]
    return {
        "models": [m["name"] for m in models_info],
        "models_info": models_info,
    }


# ─── Auto 路由配置管理 ────────────────────────────────────────────────────────

@app.get("/admin/auto-routing")
async def get_auto_routing(request: Request):
    """返回当前 auto_routing.json 配置内容及已加载模型列表（管理员）。"""
    await require_admin(request)
    # 功能模块开关:auto 路由归 NER,关闭时拒绝
    if not ner_enabled():
        raise HTTPException(status_code=503, detail="NER 实体识别功能未启用（NER_ENABLED=false）")
    from pathlib import Path as _Path
    import json as _json
    routing_path = _Path(__file__).parent / "auto_routing.json"
    try:
        with open(routing_path, "r", encoding="utf-8") as f:
            config = _json.load(f)
    except FileNotFoundError:
        config = {"routes": []}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取路由配置失败: {exc}")

    models_info = []
    for name, model in loaded_models.items():
        model_type = getattr(model, "model_type", "ner")
        models_info.append({"name": name, "type": model_type})

    return {
        "config": config,
        "models_info": models_info,
    }


@app.post("/admin/auto-routing")
async def save_auto_routing(body: dict, request: Request):
    """
    保存 auto_routing.json 并热重载 auto 模型（管理员）。

    body: {
        "routes": [
            {"model": "ioc-finder", "priority": 90, "labels": {"ipv4s": ["IP", "IP地址"], ...}},
            {"model": "gliner-x-large", "priority": 10, "labels": {}}
        ]
    }
    - labels 支持新版对象形态 {canonical类型: [别名,...]} 或旧版数组形态 [标签,...]；
      labels 为空对象 {} 即兜底模型(处理所有未匹配标签)。
    - priority 为 0~100 的整数，各 route 之间必须唯一(数值越大优先级越高)。
    """
    await require_admin(request)
    # 功能模块开关:auto 路由归 NER,关闭时拒绝
    if not ner_enabled():
        raise HTTPException(status_code=503, detail="NER 实体识别功能未启用（NER_ENABLED=false）")
    from pathlib import Path as _Path
    import json as _json

    # 基本校验
    if "routes" not in body:
        raise HTTPException(status_code=422, detail="缺少 routes 字段")
    if not isinstance(body["routes"], list):
        raise HTTPException(status_code=422, detail="routes 必须是数组")
    seen_priorities: dict = {}
    for r in body["routes"]:
        if not isinstance(r, dict) or "model" not in r or "labels" not in r:
            raise HTTPException(status_code=422, detail="每条 route 必须包含 model 和 labels 字段")
        # priority 校验: 0~100 整数, 各 route 唯一
        if "priority" not in r:
            raise HTTPException(status_code=422, detail=f"route '{r.get('model')}' 缺少 priority 字段")
        try:
            priority = int(r["priority"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"route '{r.get('model')}' 的 priority 必须是整数")
        if not (0 <= priority <= 100):
            raise HTTPException(status_code=422, detail=f"route '{r.get('model')}' 的 priority 必须在 0~100 之间")
        if priority in seen_priorities:
            raise HTTPException(
                status_code=422,
                detail=f"priority {priority} 重复: '{seen_priorities[priority]}' 与 '{r.get('model')}' 不能相同",
            )
        seen_priorities[priority] = r.get("model")
        labels = r["labels"]
        if isinstance(labels, dict):
            # 新版结构: {canonical类型: [别名,...]}
            for canonical, aliases in labels.items():
                if not isinstance(canonical, str) or not canonical.strip():
                    raise HTTPException(status_code=422, detail="labels 的键(canonical 类型)必须是非空字符串")
                if not isinstance(aliases, list):
                    raise HTTPException(status_code=422, detail=f"labels['{canonical}'] 的别名必须是数组")
        elif not isinstance(labels, list):
            raise HTTPException(status_code=422, detail="labels 必须是对象 {类型: [别名...]} 或数组")

    routing_path = _Path(__file__).parent / "auto_routing.json"
    # 读取现有文件以保留 _comment 字段
    try:
        with open(routing_path, "r", encoding="utf-8") as f:
            existing = _json.load(f)
        comment = existing.get("_comment", "")
    except Exception:
        comment = ""

    new_config = {}
    if comment:
        new_config["_comment"] = comment
    new_config["routes"] = body["routes"]

    try:
        with open(routing_path, "w", encoding="utf-8") as f:
            _json.dump(new_config, f, ensure_ascii=False, indent=2)
            f.write("\n")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"写入路由配置失败: {exc}")

    # 热重载 auto 模型
    auto_model = loaded_models.get("auto")
    fallback_models = None
    if auto_model is not None and hasattr(auto_model, "reload_routing"):
        try:
            fallback_models = auto_model.reload_routing(loaded_models)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"路由配置已写入，但热重载失败: {exc}")

    return {
        "message": "路由配置已保存并热重载",
        "fallback_models": fallback_models,
        "route_count": len(body["routes"]),
    }


@app.get("/admin/attack-auto-routing")
async def get_attack_auto_routing(request: Request):
    """返回 ATT&CK Auto 语言比例路由配置。"""
    await require_admin(request)
    # 功能模块开关:ATT&CK 关闭时拒绝
    if not attack_enabled():
        raise HTTPException(status_code=503, detail="ATT&CK 攻击技术映射功能未启用（ATTACK_ENABLED=false）")
    from .attack_auto_router import load_config

    try:
        config = load_config()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"读取 ATT&CK Auto 路由失败: {exc}")
    models_info = [
        {"name": name, "type": getattr(model, "model_type", "ner")}
        for name, model in loaded_models.items()
        if getattr(model, "model_type", "") == "attack" and name != "attack-auto"
    ]
    attack_auto = loaded_models.get("attack-auto")
    return {
        "config": config,
        "models_info": models_info,
        "active": attack_auto is not None,
        "config_hash": getattr(attack_auto, "cache_version", None),
    }


@app.post("/admin/attack-auto-routing")
async def save_attack_auto_routing(body: AttackAutoRoutingConfig, request: Request):
    """原子保存并热重载 ATT&CK Auto 语言比例路由。"""
    await require_admin(request)
    # 功能模块开关:ATT&CK 关闭时拒绝
    if not attack_enabled():
        raise HTTPException(status_code=503, detail="ATT&CK 攻击技术映射功能未启用（ATTACK_ENABLED=false）")
    from .attack_auto_router import AttackAutoModel, CONFIG_PATH, validate_config

    try:
        config = validate_config(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    expected = {
        "english_ratio": "tram",
        "chinese_ratio": "qwen3-attack-retriever",
    }
    actual = {route["metric"]: route["model"] for route in config["routes"]}
    if actual != expected:
        raise HTTPException(status_code=422, detail="路由必须是 english_ratio→tram、chinese_ratio→qwen3-attack-retriever")
    if config["fallback_model"] != "qwen3-attack-retriever":
        raise HTTPException(status_code=422, detail="兜底模型必须是 qwen3-attack-retriever")
    try:
        AttackAutoModel.validate_targets(config, loaded_models)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    attack_auto = loaded_models.get("attack-auto")
    if attack_auto is None or not isinstance(attack_auto, AttackAutoModel):
        raise HTTPException(status_code=409, detail="attack-auto 未加载，请确认 ATTACK_MODELS_ENABLED 包含 auto")

    previous = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.is_file() else None
    temporary = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    try:
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, CONFIG_PATH)
        fingerprint = attack_auto.apply_config(config, loaded_models)
    except Exception as exc:
        if temporary.exists():
            temporary.unlink()
        if previous is not None:
            restore = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".restore")
            restore.write_text(previous, encoding="utf-8")
            os.replace(restore, CONFIG_PATH)
        raise HTTPException(status_code=500, detail=f"保存 ATT&CK Auto 路由失败: {exc}")
    return {
        "message": "ATT&CK Auto 路由已保存并热重载",
        "config": config,
        "config_hash": fingerprint,
        "fallback_model": config["fallback_model"],
    }


@app.get("/server-info")
async def get_server_info():
    """返回后端服务器信息，供前端构建回调地址使用。"""
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", 8036))
    # 0.0.0.0 对外不可用，替换为 localhost
    if host == "0.0.0.0":
        host = "127.0.0.1"
    return {
        "host": host,
        "port": port,
        "base_url": f"http://{host}:{port}",
    }


@app.get("/events")
async def sse_events():
    """SSE 实时推送队列状态和 worker 状态。"""

    async def generate():
        client_q: asyncio.Queue = asyncio.Queue(maxsize=20)
        queue_manager.add_sse_client(client_q)

        def full_status() -> dict:
            # 构建完整状态（NER 队列 + 回调队列 + TI 联网情报队列）
            status = queue_manager.get_status()
            if callback_manager:
                status.update(callback_manager.get_status())
            if ti_manager:
                status["ti_queue"] = ti_manager.get_status()
            return status

        # 立即推送当前状态
        yield f"data: {json.dumps(full_status())}\n\n"

        try:
            while True:
                try:
                    await asyncio.wait_for(client_q.get(), timeout=30.0)
                    # 广播触发时以最新全量状态为准（NER 推送的 data 不再单独合并）
                    yield f"data: {json.dumps(full_status())}\n\n"
                except asyncio.TimeoutError:
                    yield 'data: {"heartbeat":true}\n\n'
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            queue_manager.remove_sse_client(client_q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── 回调失败管理接口 ─────────────────────────────────────────────────────────

def _with_callback_kind(result: Dict) -> Dict:
    """给回调日志列表每行补 callback_kind：ti（联网情报）/ attack / ner / unknown（任务已删除或模型已下线）。"""
    for item in result.get("items", []):
        if item.get("ti_platform"):
            item["callback_kind"] = "ti"
        elif item.get("model_name"):
            m = loaded_models.get(item["model_name"])
            if m is None:
                item["callback_kind"] = "unknown"
            else:
                item["callback_kind"] = "attack" if getattr(m, "model_type", "ner") == "attack" else "ner"
        else:
            item["callback_kind"] = "unknown"
    return result


@app.get("/callback/failed", response_model=CallbackFailedListResponse)
async def list_callback_failed(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
    task_id: Optional[str] = Query(None, description="任务ID筛选"),
    start_time: Optional[str] = Query(None, description="开始时间，ISO格式"),
    end_time: Optional[str] = Query(None, description="结束时间，ISO格式"),
    user_id: Optional[int] = Query(None, description="过滤特定用户（仅管理员可用）"),
    kind: Optional[str] = Query(None, description="回调来源类型：ner / attack / ti / unknown"),
):
    """查询回调失败记录列表"""
    current_user = await get_current_user(request)
    if not current_user.get("is_admin"):
        target_user_id = current_user["id"]
    else:
        target_user_id = user_id

    from .db import list_callback_failed_logs
    
    if page < 1:       page = 1
    if page_size < 1:  page_size = 10
    if page_size > 100: page_size = 100
    
    kind_models = None
    all_models = list(loaded_models.keys())
    if kind in ("ner", "attack"):
        want = kind
        kind_models = [
            name for name, m in loaded_models.items()
            if (getattr(m, "model_type", "ner") == "attack") == (want == "attack")
        ]
    elif kind not in ("ti", "unknown", None):
        kind = None

    return _with_callback_kind(
        list_callback_failed_logs(page, page_size, status, task_id, start_time, end_time,
                                  user_id=target_user_id, kind=kind,
                                  kind_models=kind_models, all_models=all_models)
    )


@app.get("/callback/failed/{log_id}", response_model=CallbackFailedLog)
async def get_callback_failed_detail(log_id: int, request: Request):
    """查询单个回调失败记录详情"""
    await get_current_user(request)
    from .db import get_callback_failed_log
    
    log = get_callback_failed_log(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="失败记录不存在")
    
    return log


# 注意：字面路由 /callback/retry/batch 必须注册在参数路由 /callback/retry/{log_id} 之前，
# 否则同方法的参数路由会先匹配到（log_id="batch"）导致 422
@app.post("/callback/retry/batch")
async def retry_callback_batch(failed_log_ids: List[int], request: Request):
    """批量重试回调"""
    await get_current_user(request)
    from .db import get_callback_failed_log
    
    if not callback_manager:
        raise HTTPException(status_code=503, detail="回调功能未启用")
    
    success_count = 0
    failed_count = 0
    
    for log_id in failed_log_ids:
        log = get_callback_failed_log(log_id)
        if not log:
            failed_count += 1
            continue
        
        callback_config = {
            "url": log["callback_url"],
            "method": log["callback_method"],
            "headers": log.get("callback_headers"),
            "timeout": int(os.environ.get("CALLBACK_TIMEOUT", 30)),
        }
        
        success = callback_manager.retry_failed_callback(
            log_id,
            str(log["task_id"]),
            callback_config,
            log.get("result_data"),
        )
        
        if success:
            success_count += 1
        else:
            failed_count += 1
    
    return {
        "message": f"已重新加入 {success_count} 个回调任务",
        "success_count": success_count,
        "failed_count": failed_count
    }


@app.post("/callback/retry/{log_id}")
async def retry_callback(log_id: int, request: Request):
    """手动重试回调"""
    await get_current_user(request)
    from .db import get_callback_failed_log
    
    if not callback_manager:
        raise HTTPException(status_code=503, detail="回调功能未启用")
    
    log = get_callback_failed_log(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="失败记录不存在")
    
    # 构建回调配置
    callback_config = {
        "url": log["callback_url"],
        "method": log["callback_method"],
        "headers": log.get("callback_headers"),
        "timeout": int(os.environ.get("CALLBACK_TIMEOUT", 30)),
    }
    
    # 使用重试专用方法：成功更新原记录为 resolved，失败更新原记录错误信息
    success = callback_manager.retry_failed_callback(
        log_id,
        str(log["task_id"]),
        callback_config,
        log.get("result_data"),
    )
    
    if not success:
        raise HTTPException(status_code=503, detail="回调队列已满，请稍后重试")
    
    return {
        "message": "回调重试已加入队列",
        "failed_log_id": log_id,
        "task_id": str(log["task_id"])
    }


@app.post("/callback/resolve/{log_id}")
async def resolve_callback_failed(log_id: int, request: Request):
    """标记回调失败记录为已解决"""
    await get_current_user(request)
    from .db import update_callback_failed_log_resolved
    
    updated = update_callback_failed_log_resolved(log_id)
    if not updated:
        raise HTTPException(status_code=404, detail="失败记录不存在")
    
    return {
        "message": "已标记为已解决",
        "failed_log_id": log_id
    }


@app.delete("/callback/failed/{log_id}")
async def delete_callback_failed(log_id: int, request: Request):
    """删除回调失败记录"""
    await get_current_user(request)
    from .db import delete_callback_failed_log
    
    deleted = delete_callback_failed_log(log_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="失败记录不存在")
    
    return {
        "message": "失败记录已删除",
        "failed_log_id": log_id
    }


@app.post("/callback/failed/delete/batch")
async def delete_callback_failed_batch(failed_log_ids: List[int], request: Request):
    """批量删除回调失败记录"""
    await get_current_user(request)
    from .db import delete_callback_failed_logs_batch
    
    deleted_count = delete_callback_failed_logs_batch(failed_log_ids)
    return {
        "message": f"已删除 {deleted_count} 条失败记录",
        "deleted_count": deleted_count
    }


@app.post("/callback/failed/clear")
async def clear_callback_failed(request: Request):
    """清空全部回调失败记录（非管理员仅清空自己任务的记录）"""
    current_user = await get_current_user(request)
    target_user_id = None if current_user.get("is_admin") else current_user["id"]
    
    from .db import clear_callback_failed_logs
    deleted_count = clear_callback_failed_logs(user_id=target_user_id)
    return {
        "message": f"已清空 {deleted_count} 条失败记录",
        "deleted_count": deleted_count
    }


# ─── 回调成功日志 ─────────────────────────────────────────────────────────────

@app.get("/callback/success", response_model=CallbackSuccessListResponse)
async def list_callback_success(
    request: Request,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页数量"),
    task_id: Optional[str] = Query(None, description="任务ID筛选"),
    start_time: Optional[str] = Query(None, description="开始时间，ISO格式"),
    end_time: Optional[str] = Query(None, description="结束时间，ISO格式"),
    user_id: Optional[int] = Query(None, description="过滤特定用户（仅管理员可用）"),
    kind: Optional[str] = Query(None, description="回调来源类型：ner / attack / ti / unknown"),
):
    """查询回调成功日志列表"""
    current_user = await get_current_user(request)
    if not current_user.get("is_admin"):
        target_user_id = current_user["id"]
    else:
        target_user_id = user_id

    from .db import list_callback_success_logs
    
    kind_models = None
    all_models = list(loaded_models.keys())
    if kind in ("ner", "attack"):
        want = kind
        kind_models = [
            name for name, m in loaded_models.items()
            if (getattr(m, "model_type", "ner") == "attack") == (want == "attack")
        ]
    elif kind not in ("ti", "unknown", None):
        kind = None

    items, total = list_callback_success_logs(page, page_size, task_id, start_time, end_time,
                                              user_id=target_user_id, kind=kind,
                                              kind_models=kind_models, all_models=all_models)
    return CallbackSuccessListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=_with_callback_kind({"items": items})["items"],
    )


@app.get("/callback/success/{log_id}")
async def get_callback_success_detail(log_id: int, request: Request):
    """查询回调成功日志详情"""
    await get_current_user(request)
    from .db import get_callback_success_log
    
    log = get_callback_success_log(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="日志不存在")
    
    return log


@app.delete("/callback/success/{log_id}")
async def delete_callback_success(log_id: int, request: Request):
    """删除回调成功日志"""
    await get_current_user(request)
    from .db import delete_callback_success_log
    
    deleted = delete_callback_success_log(log_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="日志不存在")
    
    return {
        "message": "成功日志已删除",
        "success_log_id": log_id
    }


@app.post("/callback/success/delete/batch")
async def delete_callback_success_batch(success_log_ids: List[int], request: Request):
    """批量删除回调成功日志"""
    await get_current_user(request)
    from .db import delete_callback_success_logs_batch
    
    deleted_count = delete_callback_success_logs_batch(success_log_ids)
    return {
        "message": f"已删除 {deleted_count} 条成功日志",
        "deleted_count": deleted_count
    }


@app.post("/callback/success/clear")
async def clear_callback_success(request: Request):
    """清空全部回调成功日志（非管理员仅清空自己任务的记录）"""
    current_user = await get_current_user(request)
    target_user_id = None if current_user.get("is_admin") else current_user["id"]
    
    from .db import clear_callback_success_logs
    deleted_count = clear_callback_success_logs(user_id=target_user_id)
    return {
        "message": f"已清空 {deleted_count} 条成功日志",
        "deleted_count": deleted_count
    }


# ─── 回调统计 ─────────────────────────────────────────────────────────────────

@app.get("/callback/pending")
async def list_callback_pending(request: Request):
    """获取回调队列中等待发送的条目快照(内存队列,不做分页)。"""
    await get_current_user(request)
    if not callback_manager:
        raise HTTPException(status_code=503, detail="回调功能未启用")

    items = callback_manager.list_pending()
    return {
        "total": len(items),
        "max_size": callback_manager.callback_queue.maxsize,
        "items": items,
    }


@app.get("/callback/stats", response_model=CallbackStats)
async def get_callback_statistics(request: Request, user_id: Optional[int] = Query(None, description="过滤特定用户（仅管理员可用）")):
    """获取回调统计信息"""
    current_user = await get_current_user(request)
    if not current_user.get("is_admin"):
        target_user_id = current_user["id"]
    else:
        target_user_id = user_id

    from .db import get_callback_stats
    
    return get_callback_stats(user_id=target_user_id)


# ─── 默认回调接收端点（用于测试）─────────────────────────────────────────────

@app.post("/callback/default")
async def default_callback_receiver(data: dict):
    """
    默认回调接收端点（用于测试）
    
    这个端点用于接收回调测试，会在控制台打印接收到的数据。
    前端启用回调时，如果不填写 URL，会自动使用这个端点。
    """
    import logging
    logger = logging.getLogger(__name__)
    
    # 在控制台打印接收到的回调数据
    logger.info("=" * 80)
    logger.info("📥 收到回调数据:")
    logger.info("-" * 80)
    logger.info(json.dumps(data, indent=2, ensure_ascii=False))
    logger.info("=" * 80)
    
    # 返回成功响应
    return {
        "success": True,
        "message": "回调接收成功",
        "received_at": data.get("finished_at"),
        "task_id": data.get("task_id"),
    }


# ─── 回调测试工具 ─────────────────────────────────────────────────────────────

@app.post("/callback/test", response_model=CallbackTestResponse)
async def test_callback_url(body: CallbackTestRequest, request: Request):
    """测试回调 URL 连通性"""
    await get_current_user(request)
    import time
    import httpx
    
    url = body.url
    method = body.method.upper()
    headers = body.headers or {}
    timeout = body.timeout
    
    # 添加默认请求头
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("User-Agent", "NER-API-Callback-Test/1.0")
    
    # 测试数据
    test_data = {
        "test": True,
        "message": "这是一个回调测试请求",
        "timestamp": time.time(),
    }
    
    try:
        start_time = time.time()
        with httpx.Client(timeout=timeout) as client:
            response = client.request(
                method=method,
                url=url,
                json=test_data,
                headers=headers,
            )
            response_time_ms = (time.time() - start_time) * 1000
            
            return CallbackTestResponse(
                success=200 <= response.status_code < 300,
                status_code=response.status_code,
                response_time_ms=round(response_time_ms, 2),
                error_message=None if 200 <= response.status_code < 300 else f"HTTP {response.status_code}",
                response_body=response.text[:500] if response.text else None,
            )
    
    except httpx.TimeoutException:
        return CallbackTestResponse(
            success=False,
            status_code=None,
            response_time_ms=None,
            error_message=f"连接超时 ({timeout}s)",
            response_body=None,
        )
    
    except httpx.ConnectError as e:
        return CallbackTestResponse(
            success=False,
            status_code=None,
            response_time_ms=None,
            error_message=f"连接失败: {str(e)}",
            response_body=None,
        )
    
    except Exception as e:
        return CallbackTestResponse(
            success=False,
            status_code=None,
            response_time_ms=None,
            error_message=f"未知错误: {str(e)}",
            response_body=None,
        )


# ─── 手册接口 ─────────────────────────────────────────────────────────────────

@app.get("/manuals")
async def list_manuals():
    """返回分组后的手册清单（公开），供前端构建导航。"""
    return {"groups": list_manual_groups()}


@app.get("/manuals/{key}")
async def get_manual_content(key: str):
    """返回指定手册的 Markdown 原文（公开）。"""
    entry = get_manual(key)
    if entry is None or not entry.exists():
        raise HTTPException(status_code=404, detail=f"手册 '{key}' 不存在")
    try:
        content = entry.read()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"读取手册失败: {exc}")
    return {"key": entry.key, "title": entry.title, "content": content}


# ─── 前端静态文件（容器化部署时生效）─────────────────────────────────────────
_dist = Path(__file__).parent.parent / "web" / "dist"

@app.get("/iframe/{task_id}")
async def serve_iframe(task_id: str):
    index_path = _dist / "index.html"
    if index_path.is_file():
        from fastapi.responses import FileResponse
        return FileResponse(index_path)
    raise HTTPException(status_code=404, detail="Frontend build not found")

if _dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")

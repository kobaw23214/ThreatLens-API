"""ThreatLens NER demo server.

Self-contained GLiNER entity-extraction endpoint for the public demo
(Hugging Face Spaces / any Docker host). Deliberately excludes the full
product's database, LLM and TI-enrichment dependencies.

POST /api/extract  {"text": "...", "labels": ["IP 地址", ...]}  -> entities
GET  /api/health
GET  /             demo page (static/index.html)
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from gliner import GLiNER
from pydantic import BaseModel, Field

MODEL_ID = os.environ.get("MODEL_ID", "urchade/gliner_multi-v2.1")
MAX_TEXT_CHARS = 8000
MAX_LABELS = 20

# 与主项目口径一致的威胁情报实体标签(中英混用,gliner_multi 均可零样本识别)
DEFAULT_LABELS = [
    "IP 地址", "域名", "URL", "邮箱",
    "文件哈希", "CVE 编号", "恶意软件", "威胁组织",
    "文件路径", "注册表键", "攻击技术", "操作系统",
]

print(f"[demo] loading GLiNER model: {MODEL_ID} ...")
model = GLiNER.from_pretrained(MODEL_ID)
print("[demo] model ready")

app = FastAPI(title="ThreatLens NER demo")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExtractRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_CHARS)
    labels: list[str] | None = None
    threshold: float = Field(0.5, ge=0.1, le=0.9)


@app.get("/api/health")
def health():
    return {"ok": True, "model": MODEL_ID}


@app.post("/api/extract")
def extract(req: ExtractRequest):
    labels = [l.strip() for l in (req.labels or DEFAULT_LABELS) if l.strip()]
    if not labels:
        raise HTTPException(400, "labels 为空")
    if len(labels) > MAX_LABELS:
        raise HTTPException(400, f"labels 最多 {MAX_LABELS} 个")

    raw = model.predict_entities(req.text, labels, threshold=req.threshold)

    # 抑制重叠:保留置信度更高的跨度
    raw.sort(key=lambda e: -e["score"])
    taken: list[tuple[int, int]] = []
    entities = []
    for e in raw:
        s, t = e["start"], e["end"]
        if any(s < b and a < t for a, b in taken):
            continue
        taken.append((s, t))
        entities.append({
            "text": e["text"],
            "label": e["label"],
            "start": s,
            "end": t,
            "score": round(float(e["score"]), 3),
        })
    entities.sort(key=lambda e: e["start"])

    return {
        "model": MODEL_ID,
        "count": len(entities),
        "entities": entities,
        "labels": labels,
    }


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))


app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

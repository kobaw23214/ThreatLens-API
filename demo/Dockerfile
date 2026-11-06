# ThreatLens NER 在线演示(Hugging Face Spaces Docker 兼容)
# 免 DB / 免 LLM / 免 TI 密钥:只保留 GLiNER 实体抽取核心能力。
# HF Spaces 要求监听 0.0.0.0:${PORT:-7860}。
FROM python:3.11-slim

WORKDIR /app

# torch CPU 版显著减小镜像;gliner 拉取 transformers 依赖
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir fastapi "uvicorn[standard]" "gliner>=0.2.0" tiktoken "transformers==4.53.2" sentencepiece protobuf

COPY app.py .
COPY static ./static

ENV MODEL_ID=urchade/gliner_multi-v2.1 \
    PORT=7860

EXPOSE 7860

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]

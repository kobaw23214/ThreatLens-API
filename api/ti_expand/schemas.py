"""TI 联网情报接口的请求/响应模型与种子校验。"""
from __future__ import annotations

import ipaddress
import re
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$"
)
_HASH_RE = re.compile(r"^([a-fA-F0-9]{32}|[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$")


class TiExpandRequest(BaseModel):
    seed_type: str = Field(..., description="种子类型：ip / domain / file")
    seed_value: str = Field(..., min_length=1, max_length=256, description="种子值")
    platforms: Optional[List[str]] = Field(
        None, description="目标平台子集：threatbook / qianxin；缺省为全部"
    )
    reuse_cache: bool = Field(False, description="是否启用结果缓存复用（TTL 内直接命中）")
    callback: Optional[Dict] = Field(None, description="回调配置（与 /extract 同构）")
    information_json: Optional[Dict] = Field(
        None, description="用户自定义信息（随结果/回调原样返回，与 /extract 同构）"
    )

    @field_validator("seed_type")
    @classmethod
    def _check_seed_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("ip", "domain", "file"):
            raise ValueError("seed_type 必须是 ip / domain / file")
        return v

    @field_validator("seed_value")
    @classmethod
    def _check_seed_value(cls, v: str) -> str:
        return v.strip()

    @field_validator("platforms")
    @classmethod
    def _check_platforms(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        out = []
        for p in v:
            p = p.strip().lower()
            if p not in ("threatbook", "qianxin"):
                raise ValueError(f"未知平台: {p}（可选 threatbook / qianxin）")
            if p not in out:
                out.append(p)
        if not out:
            raise ValueError("platforms 不能为空数组")
        return out

    def validate_seed_value_format(self) -> Optional[str]:
        """按类型校验种子值格式，返回错误信息；合法返回 None。"""
        v = self.seed_value
        if self.seed_type == "ip":
            try:
                ipaddress.ip_address(v)
            except ValueError:
                return f"非法 IP 地址: {v}"
        elif self.seed_type == "domain":
            if not _DOMAIN_RE.match(v):
                return f"非法域名: {v}"
        elif self.seed_type == "file":
            if not _HASH_RE.match(v):
                return f"非法样本哈希（支持 MD5/SHA1/SHA256）: {v}"
        return None


class TiExpandTaskItem(BaseModel):
    task_id: str
    platform: str
    cached: bool = False


class TiExpandResponse(BaseModel):
    tasks: List[TiExpandTaskItem]

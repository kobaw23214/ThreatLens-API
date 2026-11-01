"""TI 采集适配器抽象与统一结果结构。

统一结果（平台原生内容，不做业务加工）：
  sections: 平台页面区块，按页面原生顺序，区块名沿用平台原名叫法：
      {name, format: "kv",    items: {键: 值 | [值...]}}     判定/基础信息类
      {name, format: "table", columns: [...], rows: [[...]]} 列表类（相关样本/子域名…）
      {name, format: "text",  content: "原文"}               文字描述类
  raw:        响应拦截得到的平台原始 JSON（完整保真，可为 None）
  diagnostics: {channel, duration_ms, selector_version, artifacts, ...}
"""
from __future__ import annotations

from typing import Dict

# 种子类型
SEED_TYPES = ("ip", "domain", "file")


class TiAdapterError(Exception):
    """采集失败的结构化异常。

    reason 取值：
      not_ready        采集器未就绪（selector 未配置 / 功能未实现）
      session_expired  登录态失效
      auth_failed      凭据错误（不自动重试，防锁号）
      need_manual      需要人工介入（验证码/滑块）
      selector_broken  页面改版导致 selector 失效
      query_failed     其他查询失败
    """

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


def empty_result(channel: str) -> Dict:
    return {"sections": [], "raw": None,
            "diagnostics": {"channel": channel, "artifacts": []}}


class BaseTiAdapter:
    """平台采集适配器基类。一个平台一个实例，同平台调用由 manager 保证串行。"""

    platform: str = ""
    channel: str = "web"

    async def query(self, seed_type: str, seed_value: str) -> Dict:
        """查询一个种子，返回统一结果结构。失败抛 TiAdapterError。"""
        raise NotImplementedError

    async def keepalive(self) -> None:
        """会话保活（探测/刷新登录态）。缺省无操作。"""
        return None

    async def close(self) -> None:
        """释放资源（浏览器 context 等）。"""

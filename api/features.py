# -*- coding: utf-8 -*-
"""
功能模块开关(NER / ATT&CK / TI 联网情报)。

三个独立布尔环境变量,缺省(env 中不写)均为开启:
  NER_ENABLED     实体识别(含 IOC 提取、邮件解析、auto 路由)
  ATTACK_ENABLED  ATT&CK 攻击技术映射(含 attack-auto 路由)
  TI_ENABLED      联网情报解析(与 ti_expand 模块共用同一变量)

解析风格与 main.py 的 _env_bool 一致:只认 true/false 字面量(大小写不敏感)。
本模块只依赖 os,可被任意模块在 import 期安全引用。
"""
import os


def _env_bool(key: str, default: bool = True) -> bool:
    """解析环境变量布尔值,只认 true/false 字面量(大小写不敏感)。"""
    raw = os.environ.get(key)
    if raw is None:
        return default
    s = str(raw).strip().lower()
    if s == "true":
        return True
    if s == "false":
        return False
    return default


def ner_enabled() -> bool:
    return _env_bool("NER_ENABLED", True)


def attack_enabled() -> bool:
    return _env_bool("ATTACK_ENABLED", True)


def ti_enabled() -> bool:
    return _env_bool("TI_ENABLED", True)


def as_dict() -> dict:
    """对外暴露的开关快照,供 /auth/config 等接口返回给前端。"""
    return {"ner": ner_enabled(), "attack": attack_enabled(), "ti": ti_enabled()}

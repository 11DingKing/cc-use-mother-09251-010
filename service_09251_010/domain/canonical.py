"""规范化序列化与输入指纹。

所有指纹都基于同一份规范化 JSON：键排序、无多余空白、非 ASCII 原样保留，
带时区时间统一为 ISO 8601。这样不同进程、不同语言边界重算都得到同一哈希。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def _normalize(obj: Any) -> Any:
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            raise ValueError("指纹计算只接受带时区的时间")
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, dict):
        return {str(k): _normalize(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> bytes:
    """返回规范化 JSON 字节串。"""
    return json.dumps(
        _normalize(obj),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(obj: Any) -> str:
    return sha256_hex(canonical_json(obj))

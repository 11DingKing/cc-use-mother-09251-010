"""可替换端口：时钟与标识生成器。

领域与应用服务不直接读取系统时间或随机源，而是通过这两个端口注入，
以便在测试中稳定复现状态变化（README 约定的端口与适配器边界）。
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    """时钟端口：返回当前 UTC 时刻。"""

    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    """标识端口：为给定前缀生成全局唯一标识。"""

    def new_id(self, prefix: str) -> str: ...


class SystemClock:
    """生产时钟：读取系统 UTC 时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SystemIds:
    """生产标识：UUID4。"""

    def new_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"


class SequentialIds:
    """测试标识：按前缀递增，便于断言。"""

    def __init__(self) -> None:
        self._counters: dict[str, itertools.count] = {}

    def new_id(self, prefix: str) -> str:
        counter = self._counters.setdefault(prefix, itertools.count(1))
        return f"{prefix}_{next(counter):06d}"

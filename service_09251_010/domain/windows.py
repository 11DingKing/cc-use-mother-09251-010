"""节日窗口与时刻工具。

所有证据时刻统一归一到 UTC 存储与比较；节日窗口以“本地时区 + 本地日期”
声明，跨时区地区（如新疆、海外对标城市）各自按本地日界切桶，
避免把同一物理时刻错误地并入相邻节假日。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def parse_instant(text: object) -> datetime:
    """解析带时区偏移的 ISO8601 时刻并归一到 UTC；拒绝无时区输入。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("时刻必须是非空字符串")
    raw = text.strip()
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"无法解析的时刻: {text!r}") from exc
    if moment.tzinfo is None:
        raise ValueError(f"时刻必须携带时区偏移: {text!r}")
    return moment.astimezone(UTC)


def format_instant(moment: datetime) -> str:
    """把 UTC 时刻格式化为稳定的 ISO8601 文本（毫秒精度，Z 结尾）。"""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_local(text: str) -> datetime:
    """解析窗口边界：必须是不带时区的本地日期或日期时间。"""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析的本地时间: {text!r}") from exc
    if moment.tzinfo is not None:
        raise ValueError("窗口边界必须是不带时区偏移的本地时间")
    return moment


@dataclass(frozen=True)
class Bucket:
    """一个时间桶：UTC 半开区间 + 窗口时区下的本地起始时刻标签。"""

    index: int
    start_utc: datetime
    end_utc: datetime
    label: str

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "label": self.label,
            "start": format_instant(self.start_utc),
            "end": format_instant(self.end_utc),
        }


@dataclass(frozen=True)
class HolidayWindow:
    """节日窗口：IANA 时区 + 本地起止（结束端排除）。"""

    tz: str
    start_local: str
    end_local: str

    def __post_init__(self) -> None:
        try:
            ZoneInfo(self.tz)
        except Exception as exc:  # zoneinfo.ZoneInfoNotFoundError 及其父类
            raise ValueError(f"未知时区: {self.tz!r}") from exc
        if self.end_utc() <= self.start_utc():
            raise ValueError("窗口结束必须晚于开始")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def start_utc(self) -> datetime:
        return parse_local(self.start_local).replace(tzinfo=self.zone).astimezone(UTC)

    def end_utc(self) -> datetime:
        return parse_local(self.end_local).replace(tzinfo=self.zone).astimezone(UTC)

    def contains(self, instant: datetime) -> bool:
        return self.start_utc() <= instant.astimezone(UTC) < self.end_utc()

    def buckets(self, bucket_seconds: int) -> list[Bucket]:
        """把窗口切成等长 UTC 时间桶（末桶可能较短），标签取本地起始时刻。"""
        if bucket_seconds <= 0:
            raise ValueError("bucket_seconds 必须为正整数秒")
        buckets: list[Bucket] = []
        end = self.end_utc()
        cursor = self.start_utc()
        step = timedelta(seconds=bucket_seconds)
        index = 0
        while cursor < end:
            bucket_end = min(cursor + step, end)
            label = cursor.astimezone(self.zone).isoformat(timespec="minutes")
            buckets.append(Bucket(index=index, start_utc=cursor, end_utc=bucket_end, label=label))
            cursor = bucket_end
            index += 1
        return buckets

    def to_spec(self) -> dict:
        return {"tz": self.tz, "start": self.start_local, "end": self.end_local}

    @classmethod
    def from_spec(cls, spec: object) -> "HolidayWindow":
        if not isinstance(spec, dict):
            raise ValueError("窗口必须是包含 tz/start/end 的对象")
        return cls(
            tz=str(spec.get("tz") or ""),
            start_local=str(spec.get("start") or ""),
            end_local=str(spec.get("end") or ""),
        )

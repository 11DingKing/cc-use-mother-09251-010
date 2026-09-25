"""时区、节日窗口与时间分桶。

业务方按事发地时区声明节日窗口（例如 America/New_York 的本地午夜），
内部全部存 UTC；分桶时再回到复盘时区取整，DST 切换也能正确归属。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .errors import ValidationError

BUCKET_HOUR = "hour"
BUCKET_DAY = "day"
_BUCKETS = (BUCKET_HOUR, BUCKET_DAY)


def get_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:  # ZoneInfoNotFoundError 等
        raise ValidationError(f"未知时区: {tz_name}") from exc


def parse_instant(value: datetime | str, *, default_tz: ZoneInfo | None = None) -> datetime:
    """解析为带时区 datetime（输入为字符串时）。朴素时间挂 default_tz。"""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"无法解析时间: {value!r}") from exc
    if dt.tzinfo is None:
        if default_tz is None:
            raise ValidationError(f"时间 {value!r} 缺少时区偏移")
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(timezone.utc)


def local_window_to_utc(start: str | datetime, end: str | datetime, tz_name: str) -> tuple[datetime, datetime]:
    """把事发地时区的本地窗口边界转成 UTC 闭开区间 [start, end)。"""
    zone = get_zone(tz_name)
    start_utc = parse_instant(start, default_tz=zone)
    end_utc = parse_instant(end, default_tz=zone)
    if end_utc <= start_utc:
        raise ValidationError("节日窗口结束时间必须晚于开始时间")
    return start_utc, end_utc


def _floor_local(dt_utc: datetime, zone: ZoneInfo, unit: str) -> datetime:
    local = dt_utc.astimezone(zone)
    if unit == BUCKET_HOUR:
        return local.replace(minute=0, second=0, microsecond=0)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def bucket_key(dt_utc: datetime, tz_name: str, unit: str) -> str:
    """事件在复盘时区下所属桶的本地 ISO 标签。"""
    if unit not in _BUCKETS:
        raise ValidationError(f"不支持的分桶粒度: {unit}")
    return _floor_local(dt_utc, get_zone(tz_name), unit).isoformat()


def iter_buckets(window_start: datetime, window_end: datetime, tz_name: str, unit: str):
    """枚举覆盖 [window_start, window_end) 的桶，产出 (本地标签, 起始UTC, 结束UTC)。

    步进在绝对时间轴上进行，DST 春跳/秋回也不会产生不存在或重叠的本地时刻。
    """
    if unit not in _BUCKETS:
        raise ValidationError(f"不支持的分桶粒度: {unit}")
    zone = get_zone(tz_name)
    step = timedelta(hours=1) if unit == BUCKET_HOUR else timedelta(days=1)
    cursor = _floor_local(window_start.astimezone(zone), zone, unit)
    cursor_utc = cursor.astimezone(timezone.utc)
    while cursor_utc < window_end:
        next_utc = cursor_utc + step
        yield cursor.isoformat(), cursor_utc, next_utc
        cursor_utc = next_utc
        cursor = cursor_utc.astimezone(zone)

"""补采时段工具：解析时刻、合并/差集区间，统一对外展示格式。

补采处理全部使用左闭右开区间 ``[start, end)``：
- 合并相邻区间时只在真正重叠或首尾相接时合并；
- 差集只用已覆盖区间裁剪待采区间，保证同一段成功数据不会重复入账；
- 对外的 ``时段`` 字段在同一个自然日内只显示 ``HH:MM-HH:MM``，
  跨日时才补上日期，方便值班员直接核对。
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

Interval = tuple[datetime, datetime]

_TIME_FORMATS = ("%H:%M:%S", "%H:%M")
_DATETIME_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)


def parse_date(value: Any, default: date | None = None) -> date:
    """解析补采日期；缺省时使用当天，保证只传 HH:MM 也能落成完整时间。"""
    if value in (None, ""):
        return default or date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"无法识别的日期：{value}")


def parse_moment(value: Any, base_date: date | None = None) -> datetime:
    """解析单个时刻，支持完整日期、ISO、``HH:MM`` 和 datetime/time 对象。"""
    if isinstance(value, datetime):
        return value.replace(microsecond=0)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, time):
        return datetime.combine(base_date or date.today(), value).replace(microsecond=0)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value).replace(microsecond=0)
    if value is None:
        raise ValueError("缺少开始或结束时间")
    text = str(value).strip()
    if not text:
        raise ValueError("缺少开始或结束时间")
    # 允许传入 “2026-11-22T08:00:00+08:00”，按本地时间读入。
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is not None:
            parsed = parsed.replace(tzinfo=None)
        return parsed.replace(microsecond=0)
    except ValueError:
        pass
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(microsecond=0)
        except ValueError:
            continue
    for fmt in _TIME_FORMATS:
        try:
            return datetime.combine(base_date or date.today(), datetime.strptime(text, fmt).time())
        except ValueError:
            continue
    raise ValueError(f"无法识别的时间：{value}")


def parse_window_text(text: str, base_date: date | None = None) -> Interval:
    """解析 ``08:00-10:00``、``2026-11-22 08:00~10:00`` 等时段写法。"""
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("时段不能为空")

    # 优先按中文/波浪分隔符拆分，避免日期里的 '-' 被误判。
    for pattern in (r"\s*(?:~|～|至|到|—|–)\s*",):
        parts = re.split(pattern, raw, maxsplit=1)
        if len(parts) == 2:
            return parse_moment(parts[0], base_date), parse_moment(parts[1], base_date)

    # 纯时间：08:00-10:00、08:00:00 - 10:00:00
    match = re.fullmatch(
        r"\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*", raw
    )
    if match:
        return parse_moment(match.group(1), base_date), parse_moment(match.group(2), base_date)

    # 带日期：2026-11-22 08:00-10:00 / 2026-11-22T08:00-2026-11-22T10:00
    match = re.fullmatch(
        r"\s*(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*"
        r"(?:(\d{4}-\d{2}-\d{2})[ T])?(\d{1,2}:\d{2}(?::\d{2})?)\s*",
        raw,
    )
    if match:
        start_date = parse_date(match.group(1), base_date)
        end_date = parse_date(match.group(3), start_date) if match.group(3) else start_date
        return parse_moment(match.group(2), start_date), parse_moment(match.group(4), end_date)

    raise ValueError(f"无法识别的时段：{text}")


def parse_window(start: Any, end: Any, base_date: date | None = None) -> Interval:
    """解析补采时段，只接受结束晚于开始的区间。

    跨日补采（如 22:00 到次日 02:00）需要显式写成完整日期，
    避免把 ``10:00-09:00`` 这类笔误悄悄当成 23 小时窗口。
    """
    start_dt = parse_moment(start, base_date)
    end_dt = parse_moment(end, base_date or start_dt.date())
    if end_dt <= start_dt:
        raise ValueError(
            f"结束时间必须晚于开始时间：{clock_text(start_dt)}-{clock_text(end_dt)}"
        )
    return start_dt, end_dt


def interval_from_row(row: dict[str, Any]) -> Interval:
    return (
        parse_moment(row.get("开始时间") or row.get("start")),
        parse_moment(row.get("结束时间") or row.get("end")),
    )


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    ordered = sorted((s, e) for s, e in intervals if e > s)
    merged: list[Interval] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        elif end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
    return merged


def subtract_intervals(base: Iterable[Interval], covers: Iterable[Interval]) -> list[Interval]:
    """返回 ``base - covers`` 的并集，结果区间互不重叠。"""
    remaining = merge_intervals(base)
    cover_list = merge_intervals(covers)
    for cover_start, cover_end in cover_list:
        next_remaining: list[Interval] = []
        for start, end in remaining:
            if cover_end <= start or cover_start >= end:
                next_remaining.append((start, end))
                continue
            if start < cover_start:
                next_remaining.append((start, cover_start))
            if cover_end < end:
                next_remaining.append((cover_end, end))
        remaining = merge_intervals(next_remaining)
    return remaining


def intersect_intervals(left: Iterable[Interval], right: Iterable[Interval]) -> list[Interval]:
    intersections: list[Interval] = []
    for ls, le in merge_intervals(left):
        for rs, re_ in merge_intervals(right):
            start, end = max(ls, rs), min(le, re_)
            if end > start:
                intersections.append((start, end))
    return merge_intervals(intersections)


def clamp_interval(interval: Interval, bounds: Interval) -> Interval | None:
    start, end = max(interval[0], bounds[0]), min(interval[1], bounds[1])
    return (start, end) if end > start else None


def minutes(intervals: Iterable[Interval]) -> int:
    return sum(int((end - start).total_seconds() // 60) for start, end in intervals)


def iso_text(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S")


def clock_text(moment: datetime) -> str:
    return moment.strftime("%H:%M")


def window_text(start: datetime, end: datetime) -> str:
    if start.date() == end.date():
        return f"{start:%H:%M}-{end:%H:%M}"
    return f"{start:%Y-%m-%d %H:%M}-{end:%Y-%m-%d %H:%M}"


def interval_payload(interval: Interval) -> dict[str, Any]:
    start, end = interval
    return {
        "start": iso_text(start),
        "end": iso_text(end),
        "开始时间": iso_text(start),
        "结束时间": iso_text(end),
        "开始": clock_text(start),
        "结束": clock_text(end),
        "时段": window_text(start, end),
        "时长分钟": minutes([interval]),
    }

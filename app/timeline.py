"""时间线合并与曝光预算核心算法（不依赖 Web 层，便于单元测试）。

核心规则
--------
1. 同一展柜下的照明区间可能重叠；先把所有区间投影到一条时间线上。
2. 任意时刻若有多盏灯同时亮，该时刻有效照度取 *最大值*。
3. 展品与展柜内灯光共享同一时间线，曝光量 = 有效照度 × 时长（lux·hour）。
4. 年度预算按公历年（UTC）累计，支持在时间线上线性插值找到"首次超预算时刻"。
"""
from __future__ import annotations

from calendar import isleap
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import EPSILON

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_utc(value: datetime | str) -> datetime:
    """把库里的 ISO 字符串或 datetime 解析为 UTC datetime。"""
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_utc(dt: datetime) -> str:
    """格式化为库内统一的 UTC 秒级字符串。"""
    return parse_utc(dt).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def hours_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600.0


@dataclass(frozen=True)
class Segment:
    """合并后的恒照度片段 [start, end)。"""

    start: datetime
    end: datetime
    lux: float


def merge_intervals(
    intervals: list[tuple[datetime, datetime, float]],
) -> list[Segment]:
    """扫描线算法合并重叠照明区间。

    同一时刻存在多个重叠区间时，照度取最大值（max，而非相加）。
    相邻且照度相同的片段会合并，照度为 0 的片段直接丢弃。
    """
    events: list[tuple[datetime, int, float]] = []
    for start, end, lux in intervals:
        # (时刻, 类型, lux)：类型 0=开灯(stop 在前以正确处理首尾相接)，1=关灯
        events.append((parse_utc(start), 0, lux))
        events.append((parse_utc(end), 1, lux))

    # 同时刻先处理开灯、后关灯；同类型按 lux 排序保证结果稳定
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    segments: list[Segment] = []
    active: dict[float, int] = {}
    prev_time: datetime | None = None

    for time, kind, lux in events:
        current_lux = max(active.keys()) if active else 0.0
        if prev_time is not None and time > prev_time and current_lux > 0:
            seg = Segment(prev_time, time, current_lux)
            if segments and segments[-1].lux == seg.lux and segments[-1].end == seg.start:
                segments[-1] = Segment(segments[-1].start, seg.end, seg.lux)
            else:
                segments.append(seg)
        if kind == 0:
            active[lux] = active.get(lux, 0) + 1
        else:
            count = active.get(lux, 0) - 1
            if count > 0:
                active[lux] = count
            else:
                active.pop(lux, None)
        prev_time = time

    return segments


def clip_segments(
    segments: list[Segment], window_start: datetime, window_end: datetime
) -> list[Segment]:
    """把片段裁剪到 [window_start, window_end) 窗口内。"""
    window_start, window_end = parse_utc(window_start), parse_utc(window_end)
    out: list[Segment] = []
    for seg in segments:
        start = max(seg.start, window_start)
        end = min(seg.end, window_end)
        if end > start:
            out.append(Segment(start, end, seg.lux))
    return out


def integrate(segments: list[Segment]) -> float:
    """累计 lux·hour。"""
    return sum(seg.lux * hours_between(seg.start, seg.end) for seg in segments)


def integrate_within(
    segments: list[Segment], window_start: datetime, window_end: datetime
) -> float:
    """累计指定时间窗口内的 lux·hour。"""
    return integrate(clip_segments(segments, window_start, window_end))


def first_exceed_time(
    segments: list[Segment], limit: float, year_start: datetime
) -> datetime | None:
    """在年度时间线上找到累计曝光首次（严格）越过 ``limit`` 的时刻。

    片段内照度恒定，曝光随时间线性增长，因此越界时刻可直接线性插值：
        exceed_at = segment.start + (limit - already) / lux
    恰好等于上限不算超标（剩余额度为 0）。
    """
    year_start = parse_utc(year_start)
    year_end = year_start.replace(year=year_start.year + 1)
    accumulated = 0.0
    for seg in clip_segments(segments, year_start, year_end):
        duration_h = hours_between(seg.start, seg.end)
        exposure = seg.lux * duration_h
        if accumulated + exposure > limit + EPSILON:
            remaining = limit - accumulated
            if seg.lux <= EPSILON:
                return seg.end
            offset_h = remaining / seg.lux
            return seg.start + timedelta(hours=offset_h)
        accumulated += exposure
    return None


def year_bounds(year: int) -> tuple[datetime, datetime]:
    """公历年（UTC）的 [起始, 次年起始)。"""
    start = datetime(year, 1, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    return start, end


def days_in_year(year: int) -> int:
    return 366 if isleap(year) else 365


def day_bounds(day: datetime) -> tuple[datetime, datetime]:
    """查询日（UTC）的 [00:00, 次日 00:00)。"""
    day = parse_utc(day)
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)

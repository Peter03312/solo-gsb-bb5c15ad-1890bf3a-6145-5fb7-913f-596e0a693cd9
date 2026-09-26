"""时间线合并与预算算法单元测试。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.timeline import (
    Segment,
    clip_segments,
    first_exceed_time,
    integrate,
    integrate_within,
    merge_intervals,
    year_bounds,
)


def _dt(h: float, day: int = 1) -> datetime:
    return datetime(2026, 3, day, tzinfo=timezone.utc) + timedelta(hours=h)


def test_disjoint_intervals_kept_separate():
    segs = merge_intervals([(_dt(0), _dt(2), 50), (_dt(4), _dt(6), 100)])
    assert [(s.start, s.end, s.lux) for s in segs] == [
        (_dt(0), _dt(2), 50),
        (_dt(4), _dt(6), 100),
    ]


def test_overlap_takes_max_lux_not_sum():
    # 两盏灯在 10:00-12:00 重叠：有效照度取 max(50, 200)=200
    segs = merge_intervals([(_dt(8), _dt(12), 50), (_dt(10), _dt(14), 200)])
    assert segs == [
        Segment(_dt(8), _dt(10), 50),
        Segment(_dt(10), _dt(14), 200),
    ]
    assert integrate(segs) == 50 * 2 + 200 * 4


def test_triple_overlap_and_coalescing():
    segs = merge_intervals(
        [(_dt(0), _dt(3), 50), (_dt(2), _dt(5), 80), (_dt(4), _dt(6), 80)]
    )
    # 0-2:50, 2-3:max(50,80)=80, 3-4:80, 4-5:max(80,80)=80, 5-6:80
    # 相邻同照度合并：2-6 连成一段 80
    assert segs == [Segment(_dt(0), _dt(2), 50), Segment(_dt(2), _dt(6), 80)]


def test_touching_intervals_are_not_overlap():
    # 首尾相接不算重叠，但同照度会被合并为一个片段
    segs = merge_intervals([(_dt(0), _dt(2), 50), (_dt(2), _dt(4), 50)])
    assert segs == [Segment(_dt(0), _dt(4), 50)]


def test_zero_effective_lux_dropped_when_brighter_lamp_active_then_ends():
    # 0 lux 区间与有灯区间重叠时不产生影响
    segs = merge_intervals([(_dt(0), _dt(4), 0), (_dt(1), _dt(3), 50)])
    assert segs == [Segment(_dt(1), _dt(3), 50)]


def test_clip_to_year_window():
    start, end = year_bounds(2026)
    segs = merge_intervals(
        [
            (datetime(2025, 12, 31, 20, tzinfo=timezone.utc),
             datetime(2026, 1, 1, 4, tzinfo=timezone.utc), 100)
        ]
    )
    clipped = clip_segments(segs, start, end)
    assert clipped == [
        Segment(start, datetime(2026, 1, 1, 4, tzinfo=timezone.utc), 100)
    ]
    assert integrate_within(segs, start, end) == 400


def test_first_exceed_time_interpolated():
    # 恒定 100 lux，上限 250 lux·h → 2.5 小时处超标
    segs = merge_intervals([(_dt(0), _dt(10), 100)])
    exceed = first_exceed_time(segs, 250.0, year_bounds(2026)[0])
    assert exceed == _dt(2.5)


def test_exactly_at_limit_is_not_exceeded():
    segs = merge_intervals([(_dt(0), _dt(5), 50)])  # 恰好 250
    assert first_exceed_time(segs, 250.0, year_bounds(2026)[0]) is None


def test_first_exceed_skips_low_exposure_segment():
    # 第一段 2h*50=100，第二段从 2h 起 200lux，还需 150 → 0.75h
    segs = merge_intervals([(_dt(0), _dt(2), 50), (_dt(2), _dt(6), 200)])
    exceed = first_exceed_time(segs, 250.0, year_bounds(2026)[0])
    assert exceed == _dt(2.75)

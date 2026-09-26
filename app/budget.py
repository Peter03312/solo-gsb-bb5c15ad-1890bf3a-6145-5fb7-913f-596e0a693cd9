"""预算报表服务：从展柜灯光时间线推导每件展品的曝光预算。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .config import EPSILON
from .schemas import (
    MAX_LUX,
    STANDARD_ANNUAL_LIMITS,
    ExhibitBudgetOut,
    LightingPreviewExhibitOut,
    SafeLuxExhibitOut,
    SafeLuxLimitOut,
    Sensitivity,
)
from .timeline import (
    Segment,
    day_bounds,
    days_in_year,
    first_exceed_time,
    integrate,
    integrate_within,
    merge_intervals,
    parse_utc,
    year_bounds,
)

# 数值输出统一保留 6 位小数，抑制浮点累计噪声
_ROUND = 6


def _r(value: float) -> float:
    return round(float(value), _ROUND)


def _effective_limit(exhibit: sqlite3.Row) -> float | None:
    """展品的实际年度上限。

    入库时已按敏感度填充标准值；仅当敏感度为 low 且存的是 NULL 时表示不限量。
    """
    limit = exhibit["annual_limit_lux_hours"]
    return None if limit is None else float(limit)


def load_case_intervals(
    conn: sqlite3.Connection, showcase_id: int, year: int
) -> list[tuple[int, datetime, datetime, float]]:
    """取出展柜在指定年度内可能产生曝光的全部区间，返回 (id, start, end, lux)。

    跨年区间（年初之前开始 / 年末之后结束）也会取出，裁剪在算法侧完成。
    时间戳为定长 ISO(UTC) 字符串，字典序即可完成粗过滤，再由合并算法精确裁剪。
    """
    year_start, next_year = year_bounds(year)
    rows = conn.execute(
        """
        SELECT id, start_ts, end_ts, lux FROM light_intervals
        WHERE showcase_id = ?
          AND end_ts > ? AND start_ts < ?
        ORDER BY start_ts
        """,
        (showcase_id, year_start.isoformat(), next_year.isoformat()),
    ).fetchall()
    return [
        (r["id"], parse_utc(r["start_ts"]), parse_utc(r["end_ts"]), float(r["lux"]))
        for r in rows
    ]


def load_case_segments(
    conn: sqlite3.Connection, showcase_id: int, year: int
) -> list[Segment]:
    """取出展柜在指定年度内的全部区间并合并时间线（重叠取最大照度）。"""
    intervals = [
        (start, end, lux)
        for _, start, end, lux in load_case_intervals(conn, showcase_id, year)
    ]
    return merge_intervals(intervals)


def build_exhibit_budget(
    exhibit: sqlite3.Row, segments: list[Segment], year: int, query_date: datetime
) -> ExhibitBudgetOut:
    """根据合并后的展柜时间线计算单件展品的预算结果。"""
    query_date = parse_utc(query_date)
    year_start, next_year = year_bounds(year)
    limit = _effective_limit(exhibit)
    sensitivity = Sensitivity(exhibit["sensitivity"])

    year_segments = [
        seg for seg in segments if seg.end > year_start and seg.start < next_year
    ]
    total = integrate_within(year_segments, year_start, next_year)

    # 查询日窗口：年度内已发生曝光（截至当天 24:00）
    day_start, day_end = day_bounds(query_date)
    day_end = min(day_end, next_year)
    daily_used = integrate_within(year_segments, day_start, day_end)
    used_through_day = integrate_within(year_segments, year_start, day_end)

    if limit is None:
        return ExhibitBudgetOut(
            exhibit_id=exhibit["id"],
            exhibit_name=exhibit["name"],
            sensitivity=sensitivity,
            annual_limit_lux_hours=None,
            year=year,
            total_lux_hours=_r(total),
            remaining_lux_hours=None,
            first_exceed_at=None,
            exceeded_lux_hours=0.0,
            query_date=query_date,
            daily_quota_lux_hours=None,
            daily_used_lux_hours=_r(daily_used),
            daily_remaining_lux_hours=None,
            annual_remaining_at_date=None,
        )

    exceed_at = first_exceed_time(year_segments, limit, year_start)
    daily_quota = limit / days_in_year(year)

    return ExhibitBudgetOut(
        exhibit_id=exhibit["id"],
        exhibit_name=exhibit["name"],
        sensitivity=sensitivity,
        annual_limit_lux_hours=_r(limit),
        year=year,
        total_lux_hours=_r(total),
        remaining_lux_hours=_r(max(limit - total, 0.0)),
        first_exceed_at=exceed_at,
        exceeded_lux_hours=_r(max(total - limit, 0.0)),
        query_date=query_date,
        daily_quota_lux_hours=_r(daily_quota),
        daily_used_lux_hours=_r(daily_used),
        daily_remaining_lux_hours=_r(daily_quota - daily_used),
        annual_remaining_at_date=_r(limit - used_through_day),
    )


def build_exhibit_preview(
    exhibit: sqlite3.Row,
    current_segments: list[Segment],
    preview_segments: list[Segment],
    year: int,
) -> LightingPreviewExhibitOut:
    """对单件展品对比 现状 / 调整后 两种时间线的年度曝光（纯试算，不落库）。

    与预算接口共用同一套合并、积分与越限插值算法；跨年区间同样只计入当年。
    """
    year_start, next_year = year_bounds(year)
    limit = _effective_limit(exhibit)

    current_total = integrate_within(current_segments, year_start, next_year)
    preview_total = integrate_within(preview_segments, year_start, next_year)

    if limit is None:
        current_exceed_at = preview_exceed_at = None
    else:
        current_exceed_at = first_exceed_time(current_segments, limit, year_start)
        preview_exceed_at = first_exceed_time(preview_segments, limit, year_start)

    return LightingPreviewExhibitOut(
        exhibit_id=exhibit["id"],
        exhibit_name=exhibit["name"],
        sensitivity=Sensitivity(exhibit["sensitivity"]),
        annual_limit_lux_hours=None if limit is None else _r(limit),
        year=year,
        current_total_lux_hours=_r(current_total),
        preview_total_lux_hours=_r(preview_total),
        delta_lux_hours=_r(preview_total - current_total),
        current_first_exceed_at=current_exceed_at,
        preview_first_exceed_at=preview_exceed_at,
    )


def resolve_limit(sensitivity: Sensitivity, override: float | None) -> float | None:
    """展品入库时确定实际年度上限：显式值优先，否则取敏感度标准值。"""
    if override is not None:
        return override
    return STANDARD_ANNUAL_LIMITS[sensitivity]


def build_safe_lux_limit(
    showcase: sqlite3.Row,
    exhibits: list[sqlite3.Row],
    intervals: list[tuple[int, datetime, datetime, float]],
    start: datetime,
    end: datetime,
) -> SafeLuxLimitOut:
    """核算计划时段 [start, end) 内可追加的最大整数照度（只读试算，不落库）。

    以该年已有区间合并（重叠取最大照度）的时间线为基线；把候选区间按照度 L
    重新合并后积分全年曝光。同一展柜的展品共享时间线，曝光关于 L 单调不减，
    因此可二分最大整数 L ∈ [0, MAX_LUX]，使所有有限额展品的年度曝光都不越过
    各自上限（恰好到限不算超标，沿用 EPSILON 容差）。
    """
    start, end = parse_utc(start), parse_utc(end)
    year = start.year  # 入口校验已保证起止同年（UTC）
    year_start, next_year = year_bounds(year)
    base = [(s, e, x) for _, s, e, x in intervals]
    base_total = integrate_within(merge_intervals(base), year_start, next_year)

    limited: list[tuple[sqlite3.Row, float]] = []
    for exhibit in exhibits:
        limit = _effective_limit(exhibit)
        if limit is not None:
            limited.append((exhibit, limit))

    header = {
        "showcase_id": showcase["id"],
        "showcase_name": showcase["name"],
        "year": year,
        "start": start,
        "end": end,
    }

    if not limited:
        # 没有有限额展品：照度直接顶到协议上限
        return SafeLuxLimitOut(
            **header, feasible=True, max_safe_lux=int(MAX_LUX),
            binding_exhibits=[], exceeded_exhibits=[],
        )

    def _exhibit_out(exhibit, limit: float, total: float, floor_zero: bool = False):
        remaining = limit - total
        if floor_zero:  # 可行解处剩余额度最多负一个 EPSILON，归零避免输出 -0.0
            remaining = max(remaining, 0.0)
        return SafeLuxExhibitOut(
            exhibit_id=exhibit["id"],
            exhibit_name=exhibit["name"],
            annual_limit_lux_hours=_r(limit),
            remaining_lux_hours=_r(remaining),
        )

    # 现状已超限：不可行，只报告超限展品，不给出安全上限
    exceeded = [
        _exhibit_out(e, lim, base_total)
        for e, lim in limited
        if base_total > lim + EPSILON
    ]
    if exceeded:
        return SafeLuxLimitOut(
            **header, feasible=False, max_safe_lux=None,
            binding_exhibits=[], exceeded_exhibits=exceeded,
        )

    min_limit = min(lim for _, lim in limited)

    def total_with(lux: float) -> float:
        segments = merge_intervals(base + [(start, end, lux)])
        return integrate_within(segments, year_start, next_year)

    # total_with 关于 lux 单调不减；total_with(0) == base_total 必可行
    lo, hi = 0, int(MAX_LUX)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if total_with(mid) <= min_limit + EPSILON:
            lo = mid
        else:
            hi = mid - 1

    # 共享同一时间线 ⇒ 最终曝光相同，剩余额度最少即上限最小的展品（可并列）
    final_total = total_with(lo)
    binding = [
        _exhibit_out(e, lim, final_total, floor_zero=True)
        for e, lim in limited
        if lim <= min_limit + EPSILON
    ]
    return SafeLuxLimitOut(
        **header, feasible=True, max_safe_lux=lo,
        binding_exhibits=binding, exceeded_exhibits=[],
    )

"""展柜、展品与照明区间接口。"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from ..budget import (
    build_exhibit_preview,
    build_safe_lux_limit,
    load_case_intervals,
    resolve_limit,
)
from ..database import get_db
from ..schemas import (
    ExhibitCreate,
    ExhibitOut,
    ExhibitUpdate,
    LightIntervalBulk,
    LightIntervalOut,
    LightingPreviewIn,
    LightingPreviewOut,
    SafeLuxLimitIn,
    SafeLuxLimitOut,
    ShowcaseCreate,
    ShowcaseOut,
)
from ..timeline import day_bounds, fmt_utc, merge_intervals, parse_utc

router = APIRouter(prefix="/api/v1", tags=["showcases"])


def _get_showcase(conn: sqlite3.Connection, showcase_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, name, location FROM showcases WHERE id = ?", (showcase_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"展柜 {showcase_id} 不存在")
    return row


def _get_exhibit(conn: sqlite3.Connection, exhibit_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM exhibits WHERE id = ?", (exhibit_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"展品 {exhibit_id} 不存在")
    return row


def _serialize_interval(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"],
        "showcase_id": r["showcase_id"],
        "start": parse_utc(r["start_ts"]),
        "end": parse_utc(r["end_ts"]),
        "lux": r["lux"],
    }


# ------------------------------------------------------------------ 展柜

@router.post("/showcases", response_model=ShowcaseOut, status_code=201)
def create_showcase(body: ShowcaseCreate, conn=Depends(get_db)):
    try:
        cur = conn.execute(
            "INSERT INTO showcases (name, location) VALUES (?, ?)",
            (body.name, body.location),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"展柜名称已存在：{body.name}") from exc
    return _get_showcase(conn, cur.lastrowid)


@router.get("/showcases", response_model=list[ShowcaseOut])
def list_showcases(conn=Depends(get_db)):
    return conn.execute("SELECT id, name, location FROM showcases ORDER BY id").fetchall()


@router.get("/showcases/{showcase_id}", response_model=ShowcaseOut)
def get_showcase(showcase_id: int, conn=Depends(get_db)):
    return _get_showcase(conn, showcase_id)


# ------------------------------------------------------------------ 展品

@router.post(
    "/showcases/{showcase_id}/exhibits",
    response_model=ExhibitOut,
    status_code=201,
)
def create_exhibit(showcase_id: int, body: ExhibitCreate, conn=Depends(get_db)):
    _get_showcase(conn, showcase_id)
    limit = resolve_limit(body.sensitivity, body.annual_limit_lux_hours)
    try:
        cur = conn.execute(
            """
            INSERT INTO exhibits (showcase_id, name, sensitivity, annual_limit_lux_hours)
            VALUES (?, ?, ?, ?)
            """,
            (showcase_id, body.name, body.sensitivity.value, limit),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"展柜 {showcase_id} 下已存在同名展品：{body.name}",
        ) from exc
    return _get_exhibit(conn, cur.lastrowid)


@router.get("/showcases/{showcase_id}/exhibits", response_model=list[ExhibitOut])
def list_exhibits(showcase_id: int, conn=Depends(get_db)):
    _get_showcase(conn, showcase_id)
    return conn.execute(
        "SELECT * FROM exhibits WHERE showcase_id = ? ORDER BY id", (showcase_id,)
    ).fetchall()


@router.get("/exhibits/{exhibit_id}", response_model=ExhibitOut)
def get_exhibit(exhibit_id: int, conn=Depends(get_db)):
    return _get_exhibit(conn, exhibit_id)


@router.patch("/exhibits/{exhibit_id}", response_model=ExhibitOut)
def move_exhibit(exhibit_id: int, body: ExhibitUpdate, conn=Depends(get_db)):
    _get_exhibit(conn, exhibit_id)
    _get_showcase(conn, body.showcase_id)  # 拒绝未知目标展柜
    conn.execute(
        "UPDATE exhibits SET showcase_id = ? WHERE id = ?",
        (body.showcase_id, exhibit_id),
    )
    return _get_exhibit(conn, exhibit_id)


# --------------------------------------------------------------- 照明区间

def _insert_interval(conn, showcase_id: int, interval) -> int:
    cur = conn.execute(
        """
        INSERT INTO light_intervals (showcase_id, start_ts, end_ts, lux)
        VALUES (?, ?, ?, ?)
        """,
        (showcase_id, fmt_utc(interval.start), fmt_utc(interval.end), interval.lux),
    )
    return cur.lastrowid


@router.post(
    "/showcases/{showcase_id}/intervals",
    response_model=list[LightIntervalOut],
    status_code=201,
)
def add_intervals(showcase_id: int, body: LightIntervalBulk, conn=Depends(get_db)):
    _get_showcase(conn, showcase_id)
    ids = [_insert_interval(conn, showcase_id, iv) for iv in body.intervals]
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT id, showcase_id, start_ts, end_ts, lux
        FROM light_intervals WHERE id IN ({placeholders}) ORDER BY id
        """,
        ids,
    ).fetchall()
    return [_serialize_interval(r) for r in rows]


@router.get("/showcases/{showcase_id}/intervals", response_model=list[LightIntervalOut])
def list_intervals(
    showcase_id: int,
    day: date | None = Query(default=None, description="仅返回与该日期（UTC）有交集的区间"),
    conn=Depends(get_db),
):
    _get_showcase(conn, showcase_id)
    if day is None:
        rows = conn.execute(
            """
            SELECT id, showcase_id, start_ts, end_ts, lux
            FROM light_intervals WHERE showcase_id = ? ORDER BY start_ts
            """,
            (showcase_id,),
        ).fetchall()
    else:
        day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        _, day_end = day_bounds(day_start)
        rows = conn.execute(
            """
            SELECT id, showcase_id, start_ts, end_ts, lux
            FROM light_intervals
            WHERE showcase_id = ? AND end_ts > ? AND start_ts < ?
            ORDER BY start_ts
            """,
            (showcase_id, day_start.isoformat(), day_end.isoformat()),
        ).fetchall()

    return [_serialize_interval(r) for r in rows]


# --------------------------------------------------------------- 照明试算

@router.post(
    "/showcases/{showcase_id}/lighting-preview",
    response_model=LightingPreviewOut,
    summary="照明调整试算：对比撤去/新增区间前后的年度曝光（不落库）",
)
def lighting_preview(showcase_id: int, body: LightingPreviewIn, conn=Depends(get_db)):
    showcase = _get_showcase(conn, showcase_id)
    if body.day is None:
        query_dt = datetime.now(timezone.utc)
    else:
        query_dt = datetime(body.day.year, body.day.month, body.day.day, tzinfo=timezone.utc)
    year = query_dt.year

    # 撤去项必须属于该展柜；不存在或属于其他展柜的 ID 一律 404
    removed_ids = sorted(set(body.remove_interval_ids))
    if removed_ids:
        placeholders = ",".join("?" for _ in removed_ids)
        rows = conn.execute(
            f"""
            SELECT id FROM light_intervals
            WHERE showcase_id = ? AND id IN ({placeholders})
            """,
            (showcase_id, *removed_ids),
        ).fetchall()
        found = {r["id"] for r in rows}
        missing = [i for i in removed_ids if i not in found]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"展柜 {showcase_id} 下不存在照明区间：{missing}",
            )

    # 现状与调整后两条时间线：同批区间分别合并（重叠取最大照度），全程不写库
    removed = set(removed_ids)
    intervals = load_case_intervals(conn, showcase_id, year)
    current_segments = merge_intervals([(s, e, x) for _, s, e, x in intervals])
    adjusted = [(s, e, x) for i, s, e, x in intervals if i not in removed]
    adjusted += [(iv.start, iv.end, iv.lux) for iv in body.add_intervals]
    preview_segments = merge_intervals(adjusted)

    exhibits = conn.execute(
        "SELECT * FROM exhibits WHERE showcase_id = ? ORDER BY id", (showcase_id,)
    ).fetchall()
    return {
        "showcase_id": showcase["id"],
        "showcase_name": showcase["name"],
        "query_date": query_dt,
        "year": year,
        "removed_interval_ids": removed_ids,
        "added_interval_count": len(body.add_intervals),
        "exhibits": [
            build_exhibit_preview(e, current_segments, preview_segments, year)
            for e in exhibits
        ],
    }


# ------------------------------------------------------------- 安全照度上限

@router.post(
    "/showcases/{showcase_id}/safe-lux-limit",
    response_model=SafeLuxLimitOut,
    summary="安全照度上限：计划时段内可追加的最大整数照度（只读试算，不落库）",
)
def safe_lux_limit(showcase_id: int, body: SafeLuxLimitIn, conn=Depends(get_db)):
    showcase = _get_showcase(conn, showcase_id)
    # 请求模型已保证起止时间在 UTC 下同年
    year = body.start.year
    intervals = load_case_intervals(conn, showcase_id, year)
    exhibits = conn.execute(
        "SELECT * FROM exhibits WHERE showcase_id = ? ORDER BY id", (showcase_id,)
    ).fetchall()
    return build_safe_lux_limit(showcase, exhibits, intervals, body.start, body.end)

"""预算报表查询接口。"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from ..budget import build_exhibit_budget, load_case_segments
from ..database import get_db
from ..schemas import ExhibitBudgetOut, ShowcaseBudgetOut

router = APIRouter(prefix="/api/v1", tags=["budgets"])


def _require_showcase(conn: sqlite3.Connection, showcase_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, name FROM showcases WHERE id = ?", (showcase_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"展柜 {showcase_id} 不存在")
    return row


def _query_datetime(day: date | None) -> datetime:
    if day is None:
        return datetime.now(timezone.utc)
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


@router.get(
    "/showcases/{showcase_id}/budget",
    response_model=ShowcaseBudgetOut,
    summary="按展柜查询全部展品的曝光预算（可指定日期）",
)
def showcase_budget(
    showcase_id: int,
    day: date | None = Query(default=None, description="查询日（UTC），默认今天"),
    conn=Depends(get_db),
):
    showcase = _require_showcase(conn, showcase_id)
    query_dt = _query_datetime(day)
    exhibits = conn.execute(
        "SELECT * FROM exhibits WHERE showcase_id = ? ORDER BY id", (showcase_id,)
    ).fetchall()

    # 同一展柜共用一条合并后的灯光时间线
    segments = load_case_segments(conn, showcase_id, query_dt.year)
    results = [build_exhibit_budget(e, segments, query_dt.year, query_dt) for e in exhibits]

    return {
        "showcase_id": showcase["id"],
        "showcase_name": showcase["name"],
        "query_date": query_dt,
        "exhibits": results,
    }


@router.get(
    "/exhibits/{exhibit_id}/budget",
    response_model=ExhibitBudgetOut,
    summary="按单件展品查询曝光预算（可指定日期）",
)
def exhibit_budget(
    exhibit_id: int,
    day: date | None = Query(default=None, description="查询日（UTC），默认今天"),
    conn=Depends(get_db),
):
    exhibit = conn.execute(
        "SELECT * FROM exhibits WHERE id = ?", (exhibit_id,)
    ).fetchone()
    if exhibit is None:
        raise HTTPException(status_code=404, detail=f"展品 {exhibit_id} 不存在")

    query_dt = _query_datetime(day)
    segments = load_case_segments(conn, exhibit["showcase_id"], query_dt.year)
    return build_exhibit_budget(exhibit, segments, query_dt.year, query_dt)


@router.get(
    "/budgets",
    response_model=list[ShowcaseBudgetOut],
    summary="跨展柜批量查询；不传 showcase_id 则返回全部展柜",
)
def all_budgets(
    day: date | None = Query(default=None, description="查询日（UTC），默认今天"),
    showcase_id: int | None = Query(default=None),
    conn=Depends(get_db),
):
    query_dt = _query_datetime(day)
    if showcase_id is not None:
        _require_showcase(conn, showcase_id)
        showcases = conn.execute(
            "SELECT id, name FROM showcases WHERE id = ?", (showcase_id,)
        ).fetchall()
    else:
        showcases = conn.execute("SELECT id, name FROM showcases ORDER BY id").fetchall()

    output = []
    for showcase in showcases:
        exhibits = conn.execute(
            "SELECT * FROM exhibits WHERE showcase_id = ? ORDER BY id", (showcase["id"],)
        ).fetchall()
        segments = load_case_segments(conn, showcase["id"], query_dt.year)
        output.append(
            {
                "showcase_id": showcase["id"],
                "showcase_name": showcase["name"],
                "query_date": query_dt,
                "exhibits": [
                    build_exhibit_budget(e, segments, query_dt.year, query_dt)
                    for e in exhibits
                ],
            }
        )
    return output

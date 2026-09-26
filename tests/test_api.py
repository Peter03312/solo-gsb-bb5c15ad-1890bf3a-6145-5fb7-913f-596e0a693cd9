"""API 集成测试：每个测试函数使用独立的临时数据库。"""
from __future__ import annotations

import importlib
import os
import tempfile
from datetime import datetime, timezone

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_file))

    import app.config
    import app.database
    import app.main
    importlib.reload(app.config)
    importlib.reload(app.database)
    importlib.reload(app.main)

    from fastapi.testclient import TestClient

    with TestClient(app.main.app) as c:
        yield c


def _create_case(client, name="丝绸展柜", location="一号厅"):
    r = client.post("/api/v1/showcases", json={"name": name, "location": location})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_health_and_root(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"
    assert client.get("/").json()["docs"] == "/docs"


def test_showcase_and_exhibit_crud(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "宋代丝帛", "sensitivity": "high"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["annual_limit_lux_hours"] == 15000  # high 默认年度上限
    exhibit_id = body["id"]

    assert client.get(f"/api/v1/exhibits/{exhibit_id}").status_code == 200
    # 未知展品 404
    assert client.get("/api/v1/exhibits/9999").status_code == 404
    # 未知展柜上挂展品 404
    r = client.post(
        "/api/v1/showcases/9999/exhibits",
        json={"name": "x", "sensitivity": "low"},
    )
    assert r.status_code == 404
    # 重名展柜 409
    assert client.post("/api/v1/showcases", json={"name": "丝绸展柜"}).status_code == 409


def test_custom_annual_limit_and_low_sensitivity_unlimited(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "定制", "sensitivity": "medium", "annual_limit_lux_hours": 1000},
    )
    assert r.json()["annual_limit_lux_hours"] == 1000

    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "石碑", "sensitivity": "low"},
    )
    assert r.json()["annual_limit_lux_hours"] is None


def test_reject_negative_lux(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": -5}
        ]},
    )
    assert r.status_code == 422
    assert "照度" in r.text or "greater than or equal" in r.text


def test_reject_reverse_interval(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T08:00:00Z", "lux": 50}
        ]},
    )
    assert r.status_code == 422
    assert "结束时间" in r.text


def test_reject_zero_length_interval(client):
    case_id = _create_case(client)
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": 50}
        ]},
    )
    assert r.status_code == 422


def test_unknown_showcase_interval_rejected(client):
    r = client.post(
        "/api/v1/showcases/77/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": 50}
        ]},
    )
    assert r.status_code == 404


def test_overlap_budget_max_lux_and_first_exceed(client):
    """端到端：重叠灯光取 max；高敏展品上限 1000 lux·h。

    区间 A: 08:00-18:00 50 lux（背景灯）
    区间 B: 10:00-12:00 200 lux（重点照明，与 A 重叠 → 取 200）
    合并后曝光 = 50*2 + 200*2 + 50*6 = 800 lux·h（当天未超标）
    再加 次日 08:00-16:00 50 lux = 400 → 年度累计 1200，超标 200。
    首次超标：首日 800 之后，次日还需 200，50 lux → 4h，即次日 12:00。
    """
    case_id = _create_case(client, name="预算展柜")
    r = client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "丝画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    exhibit_id = r.json()["id"]

    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50},
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T12:00:00Z", "lux": 200},
            {"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z", "lux": 50},
        ]},
    )
    assert r.status_code == 201, r.text
    assert len(r.json()) == 3

    # 按展柜 + 日期查询（3月1日当天：800，未超标）
    r = client.get(f"/api/v1/showcases/{case_id}/budget?day=2026-03-01")
    assert r.status_code == 200, r.text
    budget = r.json()["exhibits"][0]
    assert budget["total_lux_hours"] == 1200.0
    assert budget["daily_used_lux_hours"] == 800.0
    assert budget["first_exceed_at"] is None or budget["first_exceed_at"].startswith("2026-03-02")

    # 按展品查询 3月2日：应报告首次超标时刻与超出量
    r = client.get(f"/api/v1/exhibits/{exhibit_id}/budget?day=2026-03-02")
    budget = r.json()
    assert budget["first_exceed_at"].startswith("2026-03-02T12:00:00")
    assert budget["exceeded_lux_hours"] == 200.0
    assert budget["remaining_lux_hours"] == 0.0
    assert budget["daily_used_lux_hours"] == 400.0
    # 日额度 = 1000 / 365，3月2日剩余 = 日额度 - 400
    assert budget["daily_quota_lux_hours"] == round(1000 / 365, 6)
    assert budget["daily_remaining_lux_hours"] == round(1000 / 365 - 400, 6)
    assert budget["annual_remaining_at_date"] == -200.0


def test_query_intervals_by_day(client):
    case_id = _create_case(client, name="日期过滤柜")
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T20:00:00Z", "end": "2026-03-02T04:00:00Z", "lux": 30},
            {"start": "2026-03-05T08:00:00Z", "end": "2026-03-05T10:00:00Z", "lux": 30},
        ]},
    )
    days = client.get(
        f"/api/v1/showcases/{case_id}/intervals?day=2026-03-02"
    ).json()
    assert len(days) == 1  # 跨夜区间与 3月2日有交集
    assert days[0]["lux"] == 30

    assert client.get(
        f"/api/v1/showcases/{case_id}/intervals?day=2026-03-04"
    ).json() == []


def test_naive_datetime_treated_as_utc(client):
    case_id = _create_case(client, name="无时区柜")
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00", "end": "2026-03-01T10:00:00", "lux": 50}
        ]},
    )
    assert r.status_code == 201
    assert r.json()[0]["start"].endswith(("+00:00", "Z"))


def test_move_exhibit_rejects_unknown_target(client):
    c1 = _create_case(client, name="源柜")
    c2 = _create_case(client, name="目标柜")
    ex = client.post(
        f"/api/v1/showcases/{c1}/exhibits",
        json={"name": "陶俑", "sensitivity": "medium"},
    ).json()
    r = client.patch(f"/api/v1/exhibits/{ex['id']}", json={"showcase_id": c2})
    assert r.status_code == 200 and r.json()["showcase_id"] == c2
    r = client.patch(f"/api/v1/exhibits/{ex['id']}", json={"showcase_id": 9999})
    assert r.status_code == 404


def test_all_budgets_and_filter(client):
    c1 = _create_case(client, name="柜A")
    c2 = _create_case(client, name="柜B")
    for cid in (c1, c2):
        client.post(
            f"/api/v1/showcases/{cid}/exhibits",
            json={"name": "铜器", "sensitivity": "low"},
        )
    r = client.get("/api/v1/budgets?day=2026-03-01")
    assert r.status_code == 200 and len(r.json()) == 2

    r = client.get(f"/api/v1/budgets?day=2026-03-01&showcase_id={c1}")
    assert len(r.json()) == 1 and r.json()[0]["showcase_id"] == c1

    assert client.get("/api/v1/budgets?showcase_id=4242").status_code == 404


def test_lighting_preview_basic_and_no_side_effects(client):
    """试算：撤去一盏灯并新增一段照明，对比年度曝光；试算不改库。

    现状区间：08:00-18:00 50 lux；10:00-12:00 200 lux（重叠取 max）
      → 现状年度曝光 = 50*8 + 200*2 = 800 lux·h（上限 1000，不超标）
    试算：撤去 200 lux 区间，新增 次日 08:00-16:00 80 lux
      → 试算曝光 = 50*10 + 80*8 = 1140 lux·h，差额 +340
      → 首日累计 500，次日还需 500，80 lux → 6.25h，即次日 14:15 首次超标
    """
    case_id = _create_case(client, name="试算柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "绢画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50},
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T12:00:00Z", "lux": 200},
        ]},
    )
    ids = [iv["id"] for iv in r.json()]
    before = client.get(f"/api/v1/showcases/{case_id}/budget?day=2026-03-01").json()

    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={
            "day": "2026-03-01",
            "remove_interval_ids": [ids[1]],
            "add_intervals": [
                {"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z", "lux": 80}
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["showcase_id"] == case_id
    assert body["year"] == 2026
    assert body["removed_interval_ids"] == [ids[1]]
    assert body["added_interval_count"] == 1
    item = body["exhibits"][0]
    assert item["current_total_lux_hours"] == 800.0
    assert item["preview_total_lux_hours"] == 1140.0
    assert item["delta_lux_hours"] == 340.0
    assert item["current_first_exceed_at"] is None
    assert item["preview_first_exceed_at"].startswith("2026-03-02T14:15:00")

    # 试算不落库：预算接口结果与试算前完全一致，已存区间不变
    after = client.get(f"/api/v1/showcases/{case_id}/budget?day=2026-03-01").json()
    assert after == before
    stored = client.get(f"/api/v1/showcases/{case_id}/intervals").json()
    assert [iv["id"] for iv in stored] == ids


def test_lighting_preview_reject_invalid_remove_ids(client):
    c1 = _create_case(client, name="试算柜甲")
    c2 = _create_case(client, name="试算柜乙")
    r = client.post(
        f"/api/v1/showcases/{c2}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": 50}
        ]},
    )
    foreign_id = r.json()[0]["id"]

    # 属于其他展柜的区间 ID → 404
    r = client.post(
        f"/api/v1/showcases/{c1}/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [foreign_id], "add_intervals": []},
    )
    assert r.status_code == 404
    # 不存在的区间 ID → 404
    r = client.post(
        f"/api/v1/showcases/{c1}/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [999999], "add_intervals": []},
    )
    assert r.status_code == 404
    # 未知展柜 → 404
    r = client.post(
        "/api/v1/showcases/4242/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [], "add_intervals": []},
    )
    assert r.status_code == 404


def test_lighting_preview_reuses_interval_validation(client):
    case_id = _create_case(client, name="试算校验柜")
    # 负照度 → 422
    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [], "add_intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T10:00:00Z", "lux": -1}
        ]},
    )
    assert r.status_code == 422
    # 反向区间 → 422
    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={"day": "2026-03-01", "remove_interval_ids": [], "add_intervals": [
            {"start": "2026-03-01T10:00:00Z", "end": "2026-03-01T08:00:00Z", "lux": 50}
        ]},
    )
    assert r.status_code == 422


def test_lighting_preview_unlimited_exhibit_and_cross_year(client):
    """不限量展品越限时刻为 null；跨年区间只计入查询年。"""
    case_id = _create_case(client, name="试算不限量柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "石雕", "sensitivity": "low"},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            # 跨年区间：仅 2026-01-01 00:00-04:00 这 4 小时计入 2026 年
            {"start": "2025-12-31T20:00:00Z", "end": "2026-01-01T04:00:00Z", "lux": 100},
        ]},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={"day": "2026-06-01", "remove_interval_ids": [], "add_intervals": [
            {"start": "2026-06-01T00:00:00Z", "end": "2026-06-01T02:00:00Z", "lux": 30}
        ]},
    )
    assert r.status_code == 200, r.text
    item = r.json()["exhibits"][0]
    assert item["annual_limit_lux_hours"] is None
    assert item["current_total_lux_hours"] == 400.0
    assert item["preview_total_lux_hours"] == 460.0
    assert item["delta_lux_hours"] == 60.0
    assert item["current_first_exceed_at"] is None
    assert item["preview_first_exceed_at"] is None


def test_lighting_preview_default_day_is_today(client):
    case_id = _create_case(client, name="试算默认日柜")
    r = client.post(
        f"/api/v1/showcases/{case_id}/lighting-preview",
        json={"remove_interval_ids": [], "add_intervals": []},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["year"] == datetime.now(timezone.utc).year
    assert body["exhibits"] == []


def test_persistence_across_connections(client, tmp_path):
    """同一 DB_PATH 下新建客户端连接，数据仍在（验证 SQLite 落盘）。"""
    case_id = _create_case(client, name="持久化柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "敦煌写经", "sensitivity": "high"},
    )

    import app.main
    from fastapi.testclient import TestClient

    with TestClient(app.main.app) as second:
        r = second.get(f"/api/v1/showcases/{case_id}/exhibits")
        assert r.status_code == 200 and r.json()[0]["name"] == "敦煌写经"


# ------------------------------------------------------------- 安全照度上限

def test_safe_lux_limit_basic(client):
    """已有 500 lux·h（10h×50），计划时段 8h 无灯：500+8L ≤ 1000 → L=62，剩余 4。"""
    case_id = _create_case(client, name="安全上限柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "绢画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50}
        ]},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True
    assert body["year"] == 2026
    assert body["max_safe_lux"] == 62  # 500+8*62=996 ≤ 1000 < 500+8*63=1004
    assert body["exceeded_exhibits"] == []
    binding = body["binding_exhibits"]
    assert len(binding) == 1
    assert binding[0]["exhibit_name"] == "绢画"
    assert binding[0]["annual_limit_lux_hours"] == 1000.0
    assert binding[0]["remaining_lux_hours"] == 4.0


def test_safe_lux_limit_overlap_takes_max(client):
    """计划时段内已有 100 lux：L≤100 不增曝光；L>100 时 8L ≤ 1000 → L=125，剩余 0。"""
    case_id = _create_case(client, name="安全上限重叠柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "字画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z", "lux": 100}
        ]},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_safe_lux"] == 125  # 8*125=1000 恰好到限；8*126=1008 超限
    assert body["binding_exhibits"][0]["remaining_lux_hours"] == 0.0


def test_safe_lux_limit_binding_picks_tightest_exhibit(client):
    """多件有限额展品共享时间线：瓶颈为上限最小者，并列时全部返回。"""
    case_id = _create_case(client, name="安全上限多展品柜")
    for name, limit in [("甲", 2000), ("乙", 1000), ("丙", 1000)]:
        client.post(
            f"/api/v1/showcases/{case_id}/exhibits",
            json={"name": name, "sensitivity": "medium", "annual_limit_lux_hours": limit},
        )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50}
        ]},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["max_safe_lux"] == 62  # 受上限最小的 1000 约束
    names = [e["exhibit_name"] for e in body["binding_exhibits"]]
    assert names == ["乙", "丙"]  # 甲剩余 1004，不在瓶颈列表
    assert all(e["remaining_lux_hours"] == 4.0 for e in body["binding_exhibits"])


def test_safe_lux_limit_no_limited_exhibits(client):
    """无有限额展品：直接返回协议上限 100000 lux 与空展品列表。"""
    case_id = _create_case(client, name="安全上限不限量柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "石雕", "sensitivity": "low"},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True
    assert body["max_safe_lux"] == 100000
    assert body["binding_exhibits"] == []
    assert body["exceeded_exhibits"] == []


def test_safe_lux_limit_already_exceeded(client):
    """现状已超限：不可行，报告超限展品与负剩余额度，不给出安全上限。"""
    case_id = _create_case(client, name="安全上限超限柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "丝帛", "sensitivity": "high", "annual_limit_lux_hours": 500},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "陶俑", "sensitivity": "medium", "annual_limit_lux_hours": 100000},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50},
            {"start": "2026-03-01T12:00:00Z", "end": "2026-03-01T13:00:00Z", "lux": 100},
        ]},
    )
    # 现状年度曝光 = 50*4 + 100*1 + 50*5 = 550 > 500
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is False
    assert body["max_safe_lux"] is None
    assert body["binding_exhibits"] == []
    exceeded = body["exceeded_exhibits"]
    assert [e["exhibit_name"] for e in exceeded] == ["丝帛"]  # 陶俑未超限
    assert exceeded[0]["remaining_lux_hours"] == -50.0


def test_safe_lux_limit_exactly_at_limit_allows_zero(client):
    """现状恰好到限不算超限：可追加照度为 0，瓶颈展品剩余额度为 0。"""
    case_id = _create_case(client, name="安全上限到限柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "纸本", "sensitivity": "high", "annual_limit_lux_hours": 500},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50}
        ]},
    )
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["feasible"] is True
    assert body["max_safe_lux"] == 0
    assert body["binding_exhibits"][0]["remaining_lux_hours"] == 0.0


def test_safe_lux_limit_validation(client):
    """反向 / 零长度 / 跨年（含时区归一化后跨年）→ 422；未知展柜 → 404。"""
    case_id = _create_case(client, name="安全上限校验柜")
    url = f"/api/v1/showcases/{case_id}/safe-lux-limit"

    # 反向区间
    r = client.post(url, json={
        "start": "2026-03-02T16:00:00Z", "end": "2026-03-02T08:00:00Z"})
    assert r.status_code == 422 and "结束时间" in r.text
    # 零长度区间
    r = client.post(url, json={
        "start": "2026-03-02T08:00:00Z", "end": "2026-03-02T08:00:00Z"})
    assert r.status_code == 422
    # UTC 下跨年
    r = client.post(url, json={
        "start": "2026-12-31T20:00:00Z", "end": "2027-01-01T04:00:00Z"})
    assert r.status_code == 422 and "同一公历年" in r.text
    # 带时区提交，归一化到 UTC 后跨年：2026-01-01 00:30 (+08:00) → UTC 2025-12-31 16:30
    r = client.post(url, json={
        "start": "2026-01-01T00:30:00+08:00", "end": "2026-06-01T00:00:00Z"})
    assert r.status_code == 422 and "同一公历年" in r.text
    # 未知展柜
    r = client.post("/api/v1/showcases/4242/safe-lux-limit", json={
        "start": "2026-03-02T08:00:00Z", "end": "2026-03-02T16:00:00Z"})
    assert r.status_code == 404


def test_safe_lux_limit_read_only_and_time_normalized(client):
    """naive 时间视为 UTC、带时区时间归一化为 UTC 返回；全程只读不改库。"""
    case_id = _create_case(client, name="安全上限只读柜")
    client.post(
        f"/api/v1/showcases/{case_id}/exhibits",
        json={"name": "绢画", "sensitivity": "high", "annual_limit_lux_hours": 1000},
    )
    client.post(
        f"/api/v1/showcases/{case_id}/intervals",
        json={"intervals": [
            {"start": "2026-03-01T08:00:00Z", "end": "2026-03-01T18:00:00Z", "lux": 50}
        ]},
    )
    before_budget = client.get(
        f"/api/v1/showcases/{case_id}/budget?day=2026-03-01").json()
    before_intervals = client.get(f"/api/v1/showcases/{case_id}/intervals").json()

    # naive 起 + 带时区止：2026-03-03 00:00 (+08:00) → UTC 2026-03-02 16:00
    r = client.post(
        f"/api/v1/showcases/{case_id}/safe-lux-limit",
        json={"start": "2026-03-02T08:00:00", "end": "2026-03-03T00:00:00+08:00"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["start"].startswith("2026-03-02T08:00:00")
    assert body["end"].startswith("2026-03-02T16:00:00")
    assert body["start"].endswith(("+00:00", "Z"))
    assert body["end"].endswith(("+00:00", "Z"))
    assert body["max_safe_lux"] == 62  # 与 UTC 08:00-16:00 场景一致

    # 只读：预算与已存区间均不变
    assert client.get(
        f"/api/v1/showcases/{case_id}/budget?day=2026-03-01").json() == before_budget
    assert client.get(f"/api/v1/showcases/{case_id}/intervals").json() == before_intervals

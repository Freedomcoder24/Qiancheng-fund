"""
Phase 13 候选池筛选条件 API 测试（内存 SQLite，全 mock 不依赖网络）

覆盖 /api/candidates/filter：
- 从未保存过 → 默认条件（全勾 + 0 + is_set=false）
- 保存后重读（单行 upsert，重启语义由持久化层保证）
- 全不勾基金类型 → 422
- min_return_1y 越界 → 422
- 保存后 /api/funds/candidates 按条件过滤（类型 + 收益下限）
"""
import httpx
import pytest
from datetime import date, timedelta
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.database import Base, get_db
from app.main import app
from app.services import fund_service
from app.services.fund_data_source import EastMoneyFundSource

# ---------------- 内存数据库 ----------------

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


# ---------------- mock（复用 test_candidates_api 的最小版本） ----------------

def rank_row(code: str, name: str, ret_1y: str = "35.00") -> str:
    cols = [
        code, name, "混合型", "2026-09-22", "1.1000", "1.1000",
        "1.50", "-", "-", "12.50", "20.00", ret_1y,
    ]
    return ",".join(cols)


def rank_jsonp(rows: list[str], total: int) -> str:
    datas = ",".join(f'"{r}"' for r in rows)
    return (
        "var rankData = {datas:[" + datas + "],allRecords:" + str(total)
        + ",pageIndex:1,pageSize:15};"
    )


def make_handler(gp_rows: list[str], hh_rows: list[str]):
    """mock handler：rankhandler + lsjz（温和上涨序列，让历史校验能通过）"""
    today = date.today()
    nav = Decimal("1.0000")
    navs: list[Decimal] = []
    for _ in range(40):
        nav = nav * (Decimal("1") + Decimal("0.10") / Decimal("100"))
        navs.append(nav)
    calm_rows = [
        {
            "FSRQ": (today - timedelta(days=39 - k)).isoformat(),
            "DWJZ": f"{v:.4f}",
            "LJJZ": f"{v:.4f}",
            "JZZZL": "0.10",
        }
        for k, v in enumerate(navs)
    ]
    calm_rows.reverse()

    def handler(request: httpx.Request) -> httpx.Response:
        if "rankhandler" in str(request.url):
            ft = request.url.params.get("ft", "gp")
            rows = gp_rows if ft == "gp" else hh_rows
            return httpx.Response(200, text=rank_jsonp(rows, len(rows)))
        if "lsjz" in str(request.url):
            params = request.url.params
            start = params.get("startDate")
            end = params.get("endDate")
            filtered = [
                r for r in calm_rows
                if (start is None or r["FSRQ"] >= start)
                and (end is None or r["FSRQ"] <= end)
            ]
            page = int(params.get("pageIndex", 1))
            size = int(params.get("pageSize", 20))
            chunk = filtered[(page - 1) * size: (page - 1) * size + size]
            return httpx.Response(
                200, json={"TotalCount": len(filtered), "Data": {"LSJZList": chunk}}
            )
        return httpx.Response(404, json={"error": "unknown url"})

    return handler


GP_ROWS = [rank_row("000001", "华夏成长混合", "35.00")]
HH_ROWS = [rank_row("161725", "招商中证白酒指数(LOF)A", "28.00")]


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=test_engine)
    source = EastMoneyFundSource(transport=httpx.MockTransport(make_handler(GP_ROWS, HH_ROWS)))
    monkeypatch.setattr(fund_service, "data_source", source)
    fund_service.clear_cache()
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    fund_service.clear_cache()
    Base.metadata.drop_all(bind=test_engine)


# ---------------- 测试 ----------------


def test_filter_default(client):
    """测试 1：从未保存过 → 默认条件（全勾 + 0 + is_set=false）"""
    resp = client.get("/api/candidates/filter")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_set"] is False
    assert data["include_stock"] is True
    assert data["include_mixed"] is True
    assert data["min_return_1y"] == 0


def test_filter_save_and_reload(client):
    """测试 2：保存后重读（单行 upsert；再保存一次覆盖不新增行）"""
    resp = client.put("/api/candidates/filter", json={
        "include_stock": False, "include_mixed": True, "min_return_1y": 10,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_set"] is True
    assert data["include_stock"] is False
    assert data["include_mixed"] is True
    assert data["min_return_1y"] == 10

    # 覆盖保存
    resp = client.put("/api/candidates/filter", json={
        "include_stock": True, "include_mixed": True, "min_return_1y": 0,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["min_return_1y"] == 0
    assert data["include_stock"] is True

    # 仍只有一行
    from app.database.models import CandidateFilter
    with TestSession() as db:
        assert db.query(CandidateFilter).count() == 1


def test_filter_no_type_selected_422(client):
    """测试 3：全不勾基金类型 → 422"""
    resp = client.put("/api/candidates/filter", json={
        "include_stock": False, "include_mixed": False, "min_return_1y": 0,
    })
    assert resp.status_code == 422
    assert "至少勾选" in resp.text


@pytest.mark.parametrize("bad_value", [-101, 1001])
def test_filter_min_return_out_of_range_422(client, bad_value):
    """测试 4：min_return_1y 越界（< -100 或 > 1000）→ 422"""
    resp = client.put("/api/candidates/filter", json={
        "include_stock": True, "include_mixed": True, "min_return_1y": bad_value,
    })
    assert resp.status_code == 422


def test_candidates_filter_applied(client):
    """测试 5：保存条件后 /api/funds/candidates 按条件过滤（类型 + 收益下限）"""
    # 仅混合型 + 下限 30 → 161725（28 < 30 被过滤）→ 候选池为空 + data_issues 说明
    client.put("/api/candidates/filter", json={
        "include_stock": False, "include_mixed": True, "min_return_1y": 30,
    })
    data = client.get("/api/funds/candidates").json()
    assert data["count"] == 0
    assert any("未进入候选池" in s for s in data["data_issues"])
    # 动态筛选规则文案包含条件且保留"不构成任何推荐"
    assert "30.00%" in data["screening_rule"]
    assert "不构成任何推荐" in data["screening_rule"]

    # 放宽到仅混合型 + 下限 0 → 只剩 161725（000001 是股票型被类型过滤）
    client.put("/api/candidates/filter", json={
        "include_stock": False, "include_mixed": True, "min_return_1y": 0,
    })
    data = client.get("/api/funds/candidates").json()
    codes = [c["code"] for c in data["candidates"]]
    assert codes == ["161725"]

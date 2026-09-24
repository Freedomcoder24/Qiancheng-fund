"""
持仓 API 测试（内存 SQLite + mock 数据源，不依赖真实网络）

mock 方式：
1. 数据库：dependency_overrides 覆盖 get_db，使用内存 SQLite
2. 基金净值：monkeypatch portfolio_service._fetch_fund_data，返回固定净值
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.database import Base, get_db
from app.main import app
from app.models.fund import FundNavItem
from app.services import portfolio_service
from app.services.fund_data_source import FundNotFoundError
from app.services.portfolio_service import _fetch_fund_data

# ---------------- 内存数据库与 mock 数据 ----------------

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # 内存库共享同一个连接，多个请求都能看到数据
)
TestSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)

# 固定的净值 mock 数据（对应指令第二十节的数据一致性验收基准）
LATEST = FundNavItem(
    date="2026-09-21",
    unit_nav=Decimal("1.3320"),
    accumulated_nav=Decimal("3.9050"),
    daily_change=Decimal("-0.08"),
)
PREV = FundNavItem(
    date="2026-09-18",
    unit_nav=Decimal("1.3330"),
    accumulated_nav=Decimal("3.9060"),
    daily_change=Decimal("0.10"),
)


async def fake_fetch_success(fund_code: str):
    """mock：正常返回基金名称和固定净值"""
    return "华夏成长混合", LATEST, PREV


async def fake_fetch_not_found(fund_code: str):
    """mock：基金不存在"""
    raise FundNotFoundError(f"基金 {fund_code} 不存在或没有净值数据")


def override_get_db():
    """内存数据库会话（必须是显式 generator 函数）

    注意：不能直接把 sessionmaker 实例设为 override，
    因为 FastAPI 会分析它的签名，把 sessionmaker.__call__(**local_kw)
    的 local_kw 误当成必需的 query 参数导致所有请求 422。
    """
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def client(monkeypatch):
    """创建测试客户端：内存数据库 + 成功的净值 mock"""
    Base.metadata.create_all(bind=test_engine)
    monkeypatch.setattr(portfolio_service, "_fetch_fund_data", fake_fetch_success)
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    # 每个测试用干净的表
    Base.metadata.drop_all(bind=test_engine)


CREATE_BODY = {
    "fund_code": "000001",
    "shares": 3000,
    "cost_price": 1.2,
}


# ---------------- 持仓 CRUD 测试 ----------------

class TestHoldingCrud:
    def test_create_holding(self, client):
        """测试 1：添加持仓成功，自动获取名称，收益计算正确"""
        resp = client.post("/api/portfolio/holdings", json=CREATE_BODY)
        assert resp.status_code == 201
        data = resp.json()
        assert data["fund_code"] == "000001"
        assert data["fund_name"] == "华夏成长混合"  # 名称来自 mock 数据源
        # 指令第二十节的数据一致性基准
        assert data["invested_amount"] == 3600.0
        assert data["market_value"] == 3996.0
        assert data["profit"] == 396.0
        assert data["profit_rate"] == 11.0
        # 今日收益估算：3000 × (1.3320 - 1.3330) = -3.00
        assert data["latest_nav_change"] == -3.0

    def test_list_holdings(self, client):
        """测试 2：查询全部持仓"""
        client.post("/api/portfolio/holdings", json=CREATE_BODY)
        resp = client.get("/api/portfolio/holdings")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["market_value"] == 3996.0

    def test_get_single_holding(self, client):
        """测试 5：查询单条持仓"""
        created = client.post("/api/portfolio/holdings", json=CREATE_BODY).json()
        resp = client.get(f"/api/portfolio/holdings/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]

    def test_update_holding(self, client):
        """测试 3：修改份额和成本后收益重新计算"""
        created = client.post("/api/portfolio/holdings", json=CREATE_BODY).json()
        resp = client.put(
            f"/api/portfolio/holdings/{created['id']}",
            json={"shares": 1000, "cost_price": 1.0},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["shares"] == 1000.0
        # 1000 × 1.332 = 1332.00；1000 × 1.0 = 1000.00
        assert data["invested_amount"] == 1000.0
        assert data["market_value"] == 1332.0
        assert data["profit"] == 332.0

    def test_update_partial(self, client):
        """修改时只传一个字段，另一个字段保持不变"""
        created = client.post("/api/portfolio/holdings", json=CREATE_BODY).json()
        resp = client.put(f"/api/portfolio/holdings/{created['id']}", json={"shares": 6000})
        assert resp.status_code == 200
        data = resp.json()
        assert data["shares"] == 6000.0
        assert data["cost_price"] == 1.2  # 成本未被修改

    def test_delete_holding(self, client):
        """测试 4：删除持仓返回 success=true"""
        created = client.post("/api/portfolio/holdings", json=CREATE_BODY).json()
        resp = client.delete(f"/api/portfolio/holdings/{created['id']}")
        assert resp.status_code == 200
        assert resp.json() == {"success": True}
        # 删除后列表为空
        assert client.get("/api/portfolio/holdings").json() == []

    def test_get_not_exist_holding(self, client):
        """测试 5：查询不存在的持仓返回 404 而不是 500"""
        resp = client.get("/api/portfolio/holdings/9999")
        assert resp.status_code == 404

    def test_update_not_exist_holding(self, client):
        resp = client.put("/api/portfolio/holdings/9999", json={"shares": 100})
        assert resp.status_code == 404

    def test_delete_not_exist_holding(self, client):
        resp = client.delete("/api/portfolio/holdings/9999")
        assert resp.status_code == 404


# ---------------- 校验与错误处理测试 ----------------

class TestHoldingValidation:
    def test_duplicate_fund_conflict(self, client):
        """测试 6：重复添加同一基金返回 409 Conflict"""
        client.post("/api/portfolio/holdings", json=CREATE_BODY)
        resp = client.post("/api/portfolio/holdings", json=CREATE_BODY)
        assert resp.status_code == 409
        assert "已经存在" in resp.json()["detail"]

    def test_invalid_shares(self, client):
        """测试 7：份额 <= 0 被拒绝（FastAPI 参数校验返回 422）"""
        resp = client.post(
            "/api/portfolio/holdings", json={"fund_code": "000001", "shares": 0, "cost_price": 1.2}
        )
        assert resp.status_code == 422

    def test_invalid_cost(self, client):
        """测试 8：成本 <= 0 被拒绝"""
        resp = client.post(
            "/api/portfolio/holdings", json={"fund_code": "000001", "shares": 100, "cost_price": -1}
        )
        assert resp.status_code == 422

    def test_invalid_code_format(self, client):
        """基金代码格式错误被拒绝"""
        resp = client.post(
            "/api/portfolio/holdings", json={"fund_code": "abc123", "shares": 100, "cost_price": 1.2}
        )
        assert resp.status_code == 422

    def test_fund_not_found(self, client, monkeypatch):
        """测试 9：基金不存在时添加返回 404"""
        monkeypatch.setattr(portfolio_service, "_fetch_fund_data", fake_fetch_not_found)
        resp = client.post(
            "/api/portfolio/holdings", json={"fund_code": "999999", "shares": 100, "cost_price": 1.2}
        )
        assert resp.status_code == 404


# ---------------- 账户汇总测试 ----------------

class TestSummary:
    def test_summary_empty(self, client):
        """测试 10：空账户汇总全 0，无除零错误"""
        resp = client.get("/api/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "holding_count": 0,
            "total_invested": 0.0,
            "total_market_value": 0.0,
            "total_profit": 0.0,
            "total_profit_rate": 0.0,
            "latest_nav_change": 0.0,
        }

    def test_summary_with_holdings(self, client):
        """测试 11：有持仓时汇总 = 各持仓之和（两只基金验证求和正确）"""
        client.post("/api/portfolio/holdings", json=CREATE_BODY)
        client.post(
            "/api/portfolio/holdings",
            json={"fund_code": "161725", "shares": 2000, "cost_price": 1.5},
        )
        resp = client.get("/api/portfolio/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["holding_count"] == 2
        # 投入：3600 + 3000 = 6600；市值：3996 + 2664 = 6660
        assert data["total_invested"] == 6600.0
        assert data["total_market_value"] == 6660.0
        assert data["total_profit"] == 60.0
        # 收益率：60 / 6600 × 100 = 0.91（两位小数量化）
        assert data["total_profit_rate"] == 0.91

"""
Phase 4 历史表现 / 模拟市值 API 测试（内存 SQLite + MockTransport，全 mock 不依赖网络）

mock 方式：
1. 数据库：dependency_overrides 覆盖 get_db，使用内存 SQLite
2. 基金数据：monkeypatch fund_service.data_source 为带 MockTransport 的数据源，
   handler 同时处理 lsjz（历史净值）和 FundSearchAPI（搜索）两种请求
3. 每个测试前后清空 fund_service 缓存，避免测试间互相污染
"""
import httpx
import pytest
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
    poolclass=StaticPool,  # 内存库共享同一个连接，多个请求都能看到数据
)
TestSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)


def override_get_db():
    """内存数据库会话（必须是显式 generator 函数，原因见 test_portfolio_api.py）"""
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


# ---------------- mock 净值数据（日期倒序，与真实接口一致） ----------------

# 时间正序净值：1.0 → 1.1 → 1.2 → 0.9 → 1.1
# 区间收益 = (1.1 - 1.0) / 1.0 × 100 = 10.00%
# 最大回撤 = 峰值 1.2 跌到 0.9：(0.9 - 1.2) / 1.2 = -25.00%
NAV_ROWS = [
    {"FSRQ": "2026-09-21", "DWJZ": "1.1000", "LJJZ": "1.1000", "JZZZL": "10.00"},
    {"FSRQ": "2026-09-18", "DWJZ": "0.9000", "LJJZ": "0.9000", "JZZZL": "-25.00"},
    {"FSRQ": "2026-09-17", "DWJZ": "1.2000", "LJJZ": "1.2000", "JZZZL": "9.09"},
    {"FSRQ": "2026-09-16", "DWJZ": "1.1000", "LJJZ": "1.1000", "JZZZL": "10.00"},
    {"FSRQ": "2026-09-15", "DWJZ": "1.0000", "LJJZ": "1.0000", "JZZZL": "0.00"},
]


def success_handler(request: httpx.Request) -> httpx.Response:
    """正常数据源：历史净值 + 基金搜索都能返回"""
    url = str(request.url)
    if "lsjz" in url:
        return httpx.Response(
            200,
            json={"TotalCount": len(NAV_ROWS), "Data": {"LSJZList": NAV_ROWS}},
        )
    if "FundSearchAPI" in url:
        return httpx.Response(
            200,
            json={
                "Datas": [
                    {
                        "CODE": "000001",
                        "NAME": "华夏成长混合",
                        "CATEGORY": "700",
                        "FundBaseInfo": {"FundType": "混合型-偏股"},
                    }
                ]
            },
        )
    return httpx.Response(404, json={"error": "unknown url"})


def not_found_handler(request: httpx.Request) -> httpx.Response:
    """基金不存在：lsjz 返回 TotalCount=0（数据源层会转成 FundNotFoundError）"""
    if "lsjz" in str(request.url):
        return httpx.Response(200, json={"TotalCount": 0, "Data": {"LSJZList": []}})
    return httpx.Response(200, json={"Datas": []})


def unavailable_handler(request: httpx.Request) -> httpx.Response:
    """数据源不可用：模拟网络连接失败"""
    raise httpx.ConnectError("connection refused", request=request)


# ---------------- 测试客户端 fixture ----------------

def _make_client(monkeypatch, handler):
    """公共流程：建表 + 替换数据源 + 清缓存 + 覆盖数据库依赖"""
    Base.metadata.create_all(bind=test_engine)
    source = EastMoneyFundSource(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(fund_service, "data_source", source)
    fund_service.clear_cache()
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    fund_service.clear_cache()
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture()
def client(monkeypatch):
    """正常数据源客户端"""
    yield from _make_client(monkeypatch, success_handler)


@pytest.fixture()
def not_found_client(monkeypatch):
    """基金不存在客户端"""
    yield from _make_client(monkeypatch, not_found_handler)


@pytest.fixture()
def unavailable_client(monkeypatch):
    """数据源不可用客户端"""
    yield from _make_client(monkeypatch, unavailable_handler)


def create_one_holding(client: TestClient) -> dict:
    """创建一条验收持仓（000001 / 3000 份 / 成本 1.2），返回响应 JSON"""
    resp = client.post(
        "/api/portfolio/holdings",
        json={"fund_code": "000001", "shares": 3000, "cost_price": 1.2},
    )
    assert resp.status_code == 201
    return resp.json()


# ---------------- 基金历史表现测试 ----------------

@pytest.mark.parametrize("period", [7, 30, 90, 180])
class TestFundPerformance:
    """四个合法区间都能正常返回，核心指标一致"""

    def test_performance_ok(self, client, period):
        """测试 1~4：四个 period 均返回 200 且计算正确"""
        resp = client.get(f"/api/funds/000001/performance?period={period}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fund_code"] == "000001"
        assert data["fund_name"] == "华夏成长混合"  # 名称来自 mock 搜索接口
        assert data["period"] == period
        # 时间正序首尾：起点 2026-09-15 净值 1.0，终点 2026-09-21 净值 1.1
        assert data["start_date"] == "2026-09-15"
        assert data["end_date"] == "2026-09-21"
        assert data["start_nav"] == 1.0
        assert data["end_nav"] == 1.1
        assert data["period_return"] == 10.0
        assert data["max_drawdown"] == -25.0
        # 净值走势点按时间正序，共 5 个
        points = data["nav_points"]
        assert len(points) == 5
        assert points[0] == {"date": "2026-09-15", "nav": 1.0}
        assert points[-1] == {"date": "2026-09-21", "nav": 1.1}


class TestFundPerformanceErrors:
    def test_invalid_period_422(self, client):
        """测试 5：period 不在 7/30/90/180 内返回 422"""
        resp = client.get("/api/funds/000001/performance?period=45")
        assert resp.status_code == 422

    def test_fund_not_found_404(self, not_found_client):
        """测试 6：基金不存在返回 404 而不是 500"""
        resp = not_found_client.get("/api/funds/999999/performance?period=30")
        assert resp.status_code == 404

    def test_source_unavailable_503(self, unavailable_client):
        """测试 7：数据源网络异常返回 503"""
        resp = unavailable_client.get("/api/funds/000001/performance?period=30")
        assert resp.status_code == 503


# ---------------- 持仓历史模拟市值测试 ----------------

class TestHoldingSimulatedHistory:
    def test_history_normal(self, client):
        """测试 8：模拟市值逐点计算正确（3000 份 × 历史净值，投入固定 3600）"""
        created = create_one_holding(client)
        resp = client.get(f"/api/portfolio/holdings/{created['id']}/history?period=30")
        assert resp.status_code == 200
        data = resp.json()
        assert data["holding_id"] == created["id"]
        assert data["fund_code"] == "000001"
        assert data["fund_name"] == "华夏成长混合"
        assert data["period"] == 30
        assert data["shares"] == 3000.0
        assert data["invested_amount"] == 3600.0

        # 逐点核对：模拟市值 = 3000 × 当日净值，模拟收益 = 市值 - 3600
        expected = [
            ("2026-09-15", 1.0, 3000.0, -600.0),
            ("2026-09-16", 1.1, 3300.0, -300.0),
            ("2026-09-17", 1.2, 3600.0, 0.0),
            ("2026-09-18", 0.9, 2700.0, -900.0),
            ("2026-09-21", 1.1, 3300.0, -300.0),
        ]
        points = data["points"]
        assert len(points) == len(expected)
        for point, (date, nav, mv, profit) in zip(points, expected):
            assert point["date"] == date
            assert point["nav"] == nav
            assert point["market_value"] == mv
            assert point["simulated_profit"] == profit

    def test_history_holding_not_found_404(self, client):
        """测试 9：持仓不存在返回 404"""
        resp = client.get("/api/portfolio/holdings/9999/history?period=30")
        assert resp.status_code == 404

    def test_history_source_unavailable_503(self, unavailable_client):
        """测试 10：数据源网络异常返回 503"""
        # 先用正常客户端造一条持仓？不可用客户端里数据源本身就失败，
        # 直接向内存库手工插入持仓，绕过创建流程
        from app.database.models import PortfolioHolding
        from decimal import Decimal

        db = TestSession()
        db.add(
            PortfolioHolding(
                fund_code="000001",
                fund_name="华夏成长混合",
                shares=Decimal("3000"),
                cost_price=Decimal("1.2"),
            )
        )
        db.commit()
        holding_id = db.query(PortfolioHolding).first().id
        db.close()

        resp = unavailable_client.get(
            f"/api/portfolio/holdings/{holding_id}/history?period=30"
        )
        assert resp.status_code == 503

    def test_history_invalid_period_422(self, client):
        """持仓历史的 period 同样只允许 7/30/90/180"""
        created = create_one_holding(client)
        resp = client.get(f"/api/portfolio/holdings/{created['id']}/history?period=45")
        assert resp.status_code == 422

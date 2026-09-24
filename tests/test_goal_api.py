"""
Phase 12 投资目标 API 测试（内存 SQLite + MockTransport，全 mock 不依赖网络）

覆盖：
- /api/goal：读取 / 保存 / 覆盖（单行 upsert）/ 参数越界 422
- /api/goal/analysis：
  - 未设置目标 → 200 + target_set=false（友好降级，不 400）
  - 设置目标后 → 账户级 / 持仓级指标全部由后端 Decimal 计算（含精确值断言）
  - 已达到目标 → achieved=true、gap 为负、required_profit=0、进度截断 100%
  - 数据源区间接口失败 → 200 降级，data_issues 说明，不伪造回撤 / 波动

mock 净值行的日期基于 date.today() 动态生成（与 test_monitor_api.py 相同口径）。
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
from app.database.models import InvestmentGoal
from app.main import app
from app.services import fund_service
from app.services.fund_data_source import EastMoneyFundSource
from app.services.goal_service import GLOBAL_DISCLAIMER

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


# ---------------- mock 净值数据（同 test_monitor_api.py 口径） ----------------

def build_rows(daily_changes: list[str], start_nav: str = "1.0000") -> list[dict]:
    """按时间正序的日涨跌幅列表 → lsjz 行（日期倒序：最新在前）"""
    today = date.today()
    nav = Decimal(start_nav)
    navs: list[Decimal] = []
    for pct in daily_changes:
        nav = nav * (Decimal("1") + Decimal(pct) / Decimal("100"))
        navs.append(nav)

    n = len(daily_changes)
    rows = []
    for k, pct in enumerate(daily_changes):
        rows.append({
            "FSRQ": (today - timedelta(days=n - 1 - k)).isoformat(),
            "DWJZ": f"{navs[k]:.4f}",
            "LJJZ": f"{navs[k]:.4f}",
            "JZZZL": pct,
        })
    rows.reverse()
    return rows


# 常用场景：40 天温和 +0.10% → 最新净值 1.0408（成本 1.0 时收益率恰好 +4.08%）
CALM_ROWS = build_rows(["0.10"] * 40)
# 已达标场景：首日 +10% 再 39 天温和 → 最新净值 1.1437（成本 1.0 时收益率 +14.37%）
ACHIEVED_ROWS = build_rows(["10.00"] + ["0.10"] * 39)


def make_handler(rows_by_code: dict[str, list[dict]], fail_range_for: set | None = None):
    """mock handler：lsjz（分页 + 日期过滤）+ FundSearchAPI；fail_range_for 指定区间查询失败的代码"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "lsjz" in url:
            params = request.url.params
            code = params.get("fundCode", "")
            if params.get("startDate") and fail_range_for and code in fail_range_for:
                raise httpx.ConnectError("connection refused", request=request)
            rows = rows_by_code.get(code, [])
            if not rows:
                return httpx.Response(
                    200, json={"TotalCount": 0, "Data": {"LSJZList": []}}
                )
            start = params.get("startDate")
            end = params.get("endDate")
            filtered = [
                r for r in rows
                if (start is None or r["FSRQ"] >= start)
                and (end is None or r["FSRQ"] <= end)
            ]
            page = int(params.get("pageIndex", 1))
            size = int(params.get("pageSize", 20))
            chunk = filtered[(page - 1) * size: (page - 1) * size + size]
            return httpx.Response(
                200, json={"TotalCount": len(filtered), "Data": {"LSJZList": chunk}}
            )
        if "FundSearchAPI" in url:
            code = request.url.params.get("key", "")
            return httpx.Response(200, json={"Datas": [{
                "CODE": code,
                "NAME": "华夏成长混合",
                "CATEGORY": "700",
                "FundBaseInfo": {"FundType": "混合型-偏股"},
            }]})
        return httpx.Response(404, json={"error": "unknown url"})

    return handler


def _make_client(monkeypatch, handler):
    Base.metadata.create_all(bind=test_engine)
    source = EastMoneyFundSource(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(fund_service, "data_source", source)
    fund_service.clear_cache()
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    fund_service.clear_cache()
    Base.metadata.drop_all(bind=test_engine)


def make_client_fixture(handler):
    @pytest.fixture()
    def client(monkeypatch):
        yield from _make_client(monkeypatch, handler)
    return client


goal_client = make_client_fixture(make_handler({"000001": CALM_ROWS}))
achieved_client = make_client_fixture(make_handler({"000001": ACHIEVED_ROWS}))
range_error_client = make_client_fixture(
    make_handler({"000001": CALM_ROWS}, fail_range_for={"000001"})
)


def set_goal(client: TestClient, rate: float) -> dict:
    resp = client.put("/api/goal", json={"target_return_rate": rate})
    assert resp.status_code == 200
    return resp.json()


def create_holding(client: TestClient, shares: float = 1000.0, cost: float = 1.0):
    resp = client.post(
        "/api/portfolio/holdings",
        json={"fund_code": "000001", "shares": shares, "cost_price": cost},
    )
    assert resp.status_code == 201


# ---------------- /api/goal CRUD ----------------


def test_get_goal_not_set(goal_client):
    """测试 1：未设置目标 → 200，is_set=false（不伪造默认目标）"""
    resp = goal_client.get("/api/goal")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_set"] is False
    assert data["target_return_rate"] is None


def test_save_and_overwrite_single_row(goal_client):
    """测试 2：保存 / 覆盖目标 → 单行 upsert，回读一致"""
    first = set_goal(goal_client, 10)
    assert first["is_set"] is True
    assert first["target_return_rate"] == 10.0
    assert first["updated_at"]

    second = set_goal(goal_client, 20)
    assert second["target_return_rate"] == 20.0

    # 回读为最后一次保存的值，且库里只有一行
    back = goal_client.get("/api/goal").json()
    assert back["target_return_rate"] == 20.0
    db = TestSession()
    try:
        assert db.query(InvestmentGoal).count() == 1
    finally:
        db.close()


@pytest.mark.parametrize("bad_rate", [0, -5, 600])
def test_save_invalid_range_422(goal_client, bad_rate):
    """测试 3：0 / 负数 / 超过 500 → 422"""
    resp = goal_client.put("/api/goal", json={"target_return_rate": bad_rate})
    assert resp.status_code == 422


# ---------------- /api/goal/analysis ----------------


def test_analysis_not_set_degrades(goal_client):
    """测试 4：未设置目标 → 200 + target_set=false + 引导文案（不 400）"""
    resp = goal_client.get("/api/goal/analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_set"] is False
    assert data["account"] is None
    assert data["holdings"] == []
    # 无持仓时 data_issues 引导添加持仓；有持仓时降级说明在 target_set=false 本身
    assert data["data_issues"] == ["暂无持仓，添加持仓后即可查看目标进度"]
    assert data["disclaimer"] == GLOBAL_DISCLAIMER


def test_analysis_values_exact(goal_client):
    """测试 5：目标 10% + 成本 1.0 + 最新净值 1.0408 → 全链路精确值

    收益率 = 4.08%，距离 = 5.92 个百分点，还差收益 = 59.20 元，
    进度 = 40.80%；温和上涨序列回撤 = 0.00%、30 天大波动 = 0 天。
    """
    set_goal(goal_client, 10)
    create_holding(goal_client)
    resp = goal_client.get("/api/goal/analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_set"] is True
    assert data["data_date"] == date.today().isoformat()
    assert data["disclaimer"] == GLOBAL_DISCLAIMER

    acc = data["account"]
    assert acc["current_profit_rate"] == 4.08
    assert acc["target_return_rate"] == 10.0
    assert acc["gap_percent"] == 5.92
    assert acc["required_profit"] == 59.2
    assert acc["achieved"] is False
    assert acc["progress_percent"] == 40.8
    assert acc["drawdown_90d"] == 0.0
    assert acc["volatility_days_30d"] == 0

    item = data["holdings"][0]
    assert item["fund_code"] == "000001"
    assert item["profit_rate"] == 4.08
    assert item["goal_gap_percent"] == 5.92
    assert item["achieved"] is False
    assert item["drawdown_90d"] == 0.0
    assert item["drawdown_180d"] == 0.0
    assert item["volatility_days_30d"] == 0
    assert data["data_issues"] == []


def test_analysis_achieved_status_only(achieved_client):
    """测试 6：已达到目标 → achieved=true、gap 为负、required_profit=0、进度截断 100%

    达到目标只显示状态，响应中不存在任何卖出 / 止盈指令字段。
    """
    set_goal(achieved_client, 5)
    create_holding(achieved_client)
    resp = achieved_client.get("/api/goal/analysis")
    assert resp.status_code == 200
    data = resp.json()
    acc = data["account"]
    assert acc["current_profit_rate"] == 14.37
    assert acc["achieved"] is True
    assert acc["gap_percent"] == -9.37  # 负数 = 已超过目标
    assert acc["required_profit"] == 0.0
    assert acc["progress_percent"] == 100.0  # 287.4 → 截断
    assert data["holdings"][0]["achieved"] is True
    # 全响应不含卖出 / 止盈类交易指令文案
    assert "卖出" not in resp.text
    assert "止盈" not in resp.text


def test_analysis_range_failure_degrades(range_error_client):
    """测试 7：区间净值接口失败 → 200 降级，data_issues 说明，不伪造回撤 / 波动"""
    set_goal(range_error_client, 10)
    create_holding(range_error_client)
    resp = range_error_client.get("/api/goal/analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["target_set"] is True
    # 收益 / 距离仍可计算（不依赖区间净值）
    assert data["account"]["current_profit_rate"] == 4.08
    assert data["account"]["gap_percent"] == 5.92
    # 回撤 / 波动缺失 → None + data_issues 说明，绝不编造
    assert data["account"]["drawdown_90d"] is None
    assert data["account"]["volatility_days_30d"] is None
    assert data["holdings"][0]["drawdown_90d"] is None
    assert data["holdings"][0]["volatility_days_30d"] is None
    assert data["data_issues"]
    assert "回撤" in "".join(data["data_issues"])


# ---------------- Phase 13：目标分析 5 状态（互斥单选，只描述不指令） ----------------

from app.services.goal_service import classify_goal_status  # noqa: E402


class TestClassifyGoalStatus:
    """classify_goal_status 纯函数单测：优先级 未设置 > 风险 > 已达标 > 接近 > 较远"""

    def test_not_set(self):
        assert classify_goal_status(
            target_set=False, drawdown_90d=None, volatility_days_30d=None,
            progress_percent=None, achieved=False,
        ) == ("not_set", "未设置")

    def test_far(self):
        assert classify_goal_status(
            target_set=True, drawdown_90d=Decimal("0"),
            volatility_days_30d=0, progress_percent=Decimal("40.80"), achieved=False,
        ) == ("far_from_target", "距离目标较远")

    def test_near_boundary_50(self):
        # 恰好 50% → 接近目标
        assert classify_goal_status(
            target_set=True, drawdown_90d=Decimal("0"),
            volatility_days_30d=0, progress_percent=Decimal("50.00"), achieved=False,
        ) == ("near_target", "接近目标")

    def test_achieved(self):
        assert classify_goal_status(
            target_set=True, drawdown_90d=Decimal("0"),
            volatility_days_30d=0, progress_percent=Decimal("100.00"), achieved=True,
        ) == ("target_achieved", "已达到目标")

    def test_risk_by_drawdown_priority_over_achieved(self):
        # 回撤 ≥10% 且 progress ≥100 → 仍是风险优先（不因达标掩盖风险）
        assert classify_goal_status(
            target_set=True, drawdown_90d=Decimal("10.00"),
            volatility_days_30d=0, progress_percent=Decimal("100.00"), achieved=True,
        ) == ("risk_attention", "风险需要关注")

    def test_risk_by_volatility(self):
        # 波动天数 ≥3 触发；回撤无数据不阻塞波动判定
        assert classify_goal_status(
            target_set=True, drawdown_90d=None,
            volatility_days_30d=3, progress_percent=Decimal("60.00"), achieved=False,
        ) == ("risk_attention", "风险需要关注")

    def test_no_risk_data_falls_through(self):
        # 回撤 / 波动均无数据 → 不触发风险，按进度判定
        assert classify_goal_status(
            target_set=True, drawdown_90d=None,
            volatility_days_30d=None, progress_percent=Decimal("20.00"), achieved=False,
        ) == ("far_from_target", "距离目标较远")


def test_analysis_status_not_set(goal_client):
    """测试 8（Phase 13）：未设置目标 → status=not_set"""
    data = goal_client.get("/api/goal/analysis").json()
    assert data["status"] == "not_set"
    assert data["status_label"] == "未设置"


def test_analysis_status_far(goal_client):
    """测试 9（Phase 13）：进度 40.8%（<50）→ status=far_from_target"""
    set_goal(goal_client, 10)
    create_holding(goal_client)
    data = goal_client.get("/api/goal/analysis").json()
    assert data["status"] == "far_from_target"
    assert data["status_label"] == "距离目标较远"


def test_analysis_status_near(goal_client):
    """测试 10（Phase 13）：目标 5% + 当前 4.08% → 进度 81.6% → near_target"""
    set_goal(goal_client, 5)
    create_holding(goal_client)
    data = goal_client.get("/api/goal/analysis").json()
    assert data["account"]["progress_percent"] == 81.6
    assert data["status"] == "near_target"
    assert data["status_label"] == "接近目标"


def test_analysis_status_achieved(achieved_client):
    """测试 11（Phase 13）：已达标（回撤 0 / 波动 0）→ target_achieved，无卖出文案"""
    set_goal(achieved_client, 5)
    create_holding(achieved_client)
    resp = achieved_client.get("/api/goal/analysis")
    data = resp.json()
    assert data["status"] == "target_achieved"
    assert data["status_label"] == "已达到目标"
    assert "卖出" not in resp.text
    assert "止盈" not in resp.text


def test_analysis_status_risk_priority():
    """测试 12（Phase 13）：已达标但近 30 天有 3 天大波动 → 风险优先于达标

    序列：首日 +10%（30 天窗口外）+ 36 天温和 + 末 3 天各 -2% →
    最终收益约 7.29%（>5，achieved=true），波动窗口内大波动 3 天 → risk_attention。
    """
    from unittest.mock import patch

    risk_rows = build_rows(["10.00"] + ["0.10"] * 36 + ["-2.00"] * 3)
    Base.metadata.create_all(bind=test_engine)
    source = EastMoneyFundSource(
        transport=httpx.MockTransport(make_handler({"000001": risk_rows}))
    )
    app.dependency_overrides[get_db] = override_get_db
    try:
        with patch.object(fund_service, "data_source", source):
            fund_service.clear_cache()
            tc = TestClient(app)
            set_goal(tc, 5)
            create_holding(tc)
            data = tc.get("/api/goal/analysis").json()
            assert data["account"]["achieved"] is True
            assert data["account"]["volatility_days_30d"] == 3
            assert data["status"] == "risk_attention"
            assert data["status_label"] == "风险需要关注"
    finally:
        app.dependency_overrides.clear()
        fund_service.clear_cache()
        Base.metadata.drop_all(bind=test_engine)

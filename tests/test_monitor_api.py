"""
Phase 7 智能监控 API 测试（内存 SQLite + MockTransport，全 mock 不依赖网络）

mock 方式：
1. 数据库：dependency_overrides 覆盖 get_db，使用内存 SQLite
2. 基金数据：monkeypatch fund_service.data_source，handler 支持
   - lsjz 无日期参数（历史净值分页，供持仓列表 / 单日涨跌幅使用）
   - lsjz 带 startDate/endDate（区间净值，供回撤 / 波动检查使用，自动翻页）
   - FundSearchAPI（基金名称）
3. mock 净值行的日期基于 date.today() 动态生成（监控的波动窗口按"今天-30 天"
   过滤，写死日期会随时间推移失效），每个测试前后清空 fund_service 缓存
"""
import httpx
import pytest
from decimal import Decimal
from datetime import date, timedelta
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


# ---------------- mock 净值数据构造 ----------------

FUND_NAMES = {
    "000001": "华夏成长混合",
    "002974": "广发信息技术联接C",
    "161725": "招商中证白酒指数(LOF)A",
}


def build_rows(daily_changes: list[str], start_nav: str = "1.0000") -> list[dict]:
    """按时间正序的日涨跌幅列表 → lsjz 行（日期倒序：最新在前）

    日期从今天往回逐自然日排（mock 不关心周末）；
    DWJZ 按涨跌幅链式计算（供回撤检查），JZZZL 直接使用给定值。
    """
    today = date.today()
    nav = Decimal(start_nav)
    navs: list[Decimal] = []
    for pct in daily_changes:
        nav = nav * (Decimal("1") + Decimal(pct) / Decimal("100"))
        navs.append(nav)

    n = len(daily_changes)
    rows = []
    for k, pct in enumerate(daily_changes):  # k=0 为最早一天
        rows.append({
            "FSRQ": (today - timedelta(days=n - 1 - k)).isoformat(),
            "DWJZ": f"{navs[k]:.4f}",
            "LJJZ": f"{navs[k]:.4f}",
            "JZZZL": pct,
        })
    rows.reverse()
    return rows


# 常用场景：40 天温和 +0.10%（全部检查都不触发）
CALM_CHANGES = ["0.10"] * 40


def make_handler(rows_by_code: dict[str, list[dict]]):
    """生成 mock handler：lsjz（分页 + 日期过滤）+ FundSearchAPI"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "lsjz" in url:
            params = request.url.params
            code = params.get("fundCode", "")
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
            if code in FUND_NAMES:
                return httpx.Response(200, json={"Datas": [{
                    "CODE": code,
                    "NAME": FUND_NAMES[code],
                    "CATEGORY": "700",
                    "FundBaseInfo": {"FundType": "混合型-偏股"},
                }]})
            return httpx.Response(200, json={"Datas": []})
        return httpx.Response(404, json={"error": "unknown url"})

    return handler


def make_range_error_handler(rows_by_code: dict[str, list[dict]]):
    """历史净值正常，但带日期区间的请求网络失败（模拟区间接口挂了）"""

    def handler(request: httpx.Request) -> httpx.Response:
        if "lsjz" in str(request.url) and request.url.params.get("startDate"):
            raise httpx.ConnectError("connection refused", request=request)
        return make_handler(rows_by_code)(request)

    return handler


def unavailable_handler(request: httpx.Request) -> httpx.Response:
    """数据源整体不可用"""
    raise httpx.ConnectError("connection refused", request=request)


# ---------------- 测试客户端 fixture ----------------

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


# 各场景客户端（handler 在模块加载时确定，rows 基于今天动态生成）
calm_client = make_client_fixture(make_handler({
    code: build_rows(CALM_CHANGES) for code in FUND_NAMES
}))
drop_client = make_client_fixture(make_handler({
    "000001": build_rows(["0.10"] * 39 + ["-3.50"]),
}))
rise_client = make_client_fixture(make_handler({
    "000001": build_rows(["0.10"] * 39 + ["5.20"]),
}))
drawdown_client = make_client_fixture(make_handler({
    # 先温和上涨 35 天，再单日 -20% 建立深度回撤（回撤 ≈ -22% → 高风险）
    "000001": build_rows(["0.20"] * 35 + ["-20.00", "-1.00", "-1.00", "-1.00"]),
}))
volatility_client = make_client_fixture(make_handler({
    # 近 30 天窗口内 3 天 +2.5%（日涨跌幅按时间正序，索引 20/25/30 落在窗口内）
    "000001": build_rows(
        ["0.10"] * 20 + ["2.50", "0.10"] * 1 + ["0.10"] * 4
        + ["2.50", "0.10"] * 1 + ["0.10"] * 4
        + ["2.50"] + ["0.10"] * 8
    ),
}))
insufficient_client = make_client_fixture(make_handler({
    "000001": build_rows(["0.10"]),  # 只有 1 个净值点
}))
range_error_client = make_client_fixture(
    make_range_error_handler({"000001": build_rows(CALM_CHANGES)})
)
unavailable_client_fixture = make_client_fixture(unavailable_handler)


def create_holding(client: TestClient, code: str = "000001", shares: float = 1000.0,
                   cost: float = 1.0) -> dict:
    resp = client.post(
        "/api/portfolio/holdings",
        json={"fund_code": code, "shares": shares, "cost_price": cost},
    )
    assert resp.status_code == 201
    return resp.json()


# ---------------- 测试 ----------------

class TestMonitorBasic:
    def test_no_holdings(self, calm_client):
        """测试 1：无持仓 → 200，无提醒，summary 提示添加持仓"""
        resp = calm_client.get("/api/monitor")
        assert resp.status_code == 200
        data = resp.json()
        assert data["checked_count"] == 0
        assert data["alerts"] == []
        assert "暂无持仓" in data["summary"]
        assert data["data_date"] is None

    def test_all_clear(self, calm_client):
        """测试 2：三只等市值持仓 + 温和净值 → 无任何提醒"""
        for i, code in enumerate(FUND_NAMES):
            create_holding(calm_client, code, shares=1000, cost=1.0)
        resp = calm_client.get("/api/monitor")
        assert resp.status_code == 200
        data = resp.json()
        assert data["checked_count"] == 3
        assert data["alert_count"] == 0
        assert data["alerts"] == []
        assert data["summary"] == "当前未发现明显异常"
        assert data["data_issues"] == []
        assert data["data_date"] == date.today().isoformat()
        assert data["generated_at"]
        assert "不构成投资建议" in data["disclaimer"]

    def test_levels_and_disclaimer_fields(self, calm_client):
        """测试 3：提醒字段结构完整（类型/等级/基金/原因/数据）"""
        create_holding(calm_client, "000001")
        resp = calm_client.get("/api/monitor")
        data = resp.json()
        # 单只持仓必然触发集中度（100% ≥ 80% 高风险）
        assert data["alert_count"] >= 1
        alert = data["alerts"][0]
        assert alert["type"] in {"daily_change", "concentration", "max_drawdown", "volatility"}
        assert alert["level"] in {"info", "warning", "danger"}
        assert alert["level_label"] in {"提示", "注意", "高风险"}
        assert alert["type_label"]
        assert alert["fund_code"] == "000001"
        assert alert["fund_name"] == "华夏成长混合"
        assert alert["reason"] and alert["detail"]


class TestDailyChangeRule:
    def test_drop_3_5_percent_warning(self, drop_client):
        """测试 4：单日下跌 3.5% → 注意级涨跌幅提醒"""
        create_holding(drop_client)
        resp = drop_client.get("/api/monitor")
        data = resp.json()
        dc = [a for a in data["alerts"] if a["type"] == "daily_change"]
        assert len(dc) == 1
        assert dc[0]["level"] == "warning"
        assert dc[0]["level_label"] == "注意"
        assert "下跌" in dc[0]["reason"]
        assert "-3.50%" in dc[0]["detail"]

    def test_rise_5_2_percent_danger(self, rise_client):
        """测试 5：单日上涨 5.2% → 高风险涨跌幅提醒"""
        create_holding(rise_client)
        data = rise_client.get("/api/monitor").json()
        dc = [a for a in data["alerts"] if a["type"] == "daily_change"]
        assert len(dc) == 1
        assert dc[0]["level"] == "danger"
        assert "上涨" in dc[0]["reason"]

    def test_calm_no_daily_change_alert(self, calm_client):
        """测试 6：单日 +0.1% 不触发涨跌幅提醒"""
        create_holding(calm_client)
        data = calm_client.get("/api/monitor").json()
        assert all(a["type"] != "daily_change" for a in data["alerts"])


class TestConcentrationRule:
    def test_single_holding_100_percent_danger(self, calm_client):
        """测试 7：仅 1 只持仓（占比 100%）→ 高风险集中度提醒"""
        create_holding(calm_client)
        data = calm_client.get("/api/monitor").json()
        cc = [a for a in data["alerts"] if a["type"] == "concentration"]
        assert len(cc) == 1
        assert cc[0]["level"] == "danger"
        assert "100.00%" in cc[0]["detail"]

    def test_three_equal_holdings_no_alert(self, calm_client):
        """测试 8：三只等市值持仓（各 33.33%）→ 不触发集中度提醒"""
        for code in FUND_NAMES:
            create_holding(calm_client, code, shares=1000, cost=1.0)
        data = calm_client.get("/api/monitor").json()
        assert all(a["type"] != "concentration" for a in data["alerts"])


class TestDrawdownAndVolatilityRules:
    def test_drawdown_danger(self, drawdown_client):
        """测试 9：近 90 天回撤约 -22% → 高风险回撤提醒，且波动不误报"""
        create_holding(drawdown_client)
        data = drawdown_client.get("/api/monitor").json()
        dd = [a for a in data["alerts"] if a["type"] == "max_drawdown"]
        assert len(dd) == 1
        assert dd[0]["level"] == "danger"
        assert "最大回撤" in dd[0]["detail"]
        # 除 -20% 当天外其余交易日温和，波动天数 = 1 < 2，不应触发波动提醒
        assert all(a["type"] != "volatility" for a in data["alerts"])

    def test_volatility_warning(self, volatility_client):
        """测试 10：近 30 天 3 天波动超 2% → 注意级波动提醒"""
        create_holding(volatility_client)
        data = volatility_client.get("/api/monitor").json()
        vol = [a for a in data["alerts"] if a["type"] == "volatility"]
        assert len(vol) == 1
        assert vol[0]["level"] == "warning"
        assert "3 个交易日" in vol[0]["reason"]


class TestDataIssues:
    def test_insufficient_nav_points(self, insufficient_client):
        """测试 11：仅 1 个净值点 → 回撤与波动检查跳过并如实说明"""
        create_holding(insufficient_client)
        resp = insufficient_client.get("/api/monitor")
        assert resp.status_code == 200
        data = resp.json()
        text = "".join(data["data_issues"])
        assert "最大回撤检查跳过" in text
        assert "异常波动检查跳过" in text
        # 数据不足不伪造提醒
        assert all(a["type"] not in {"max_drawdown", "volatility"} for a in data["alerts"])

    def test_range_source_error_graceful(self, range_error_client):
        """测试 12：区间接口挂了但历史净值正常 → 200，说明问题，涨跌幅检查照常"""
        create_holding(range_error_client)
        resp = range_error_client.get("/api/monitor")
        assert resp.status_code == 200
        data = resp.json()
        assert data["checked_count"] == 1
        assert any("区间净值获取失败" in i for i in data["data_issues"])
        assert all(a["type"] not in {"max_drawdown", "volatility"} for a in data["alerts"])

    def test_source_unavailable_503(self, unavailable_client_fixture):
        """测试 13：数据源整体不可用 → 503（需先直接插库造持仓）"""
        from app.database.models import PortfolioHolding

        db = TestSession()
        db.add(
            PortfolioHolding(
                fund_code="000001",
                fund_name="华夏成长混合",
                shares=Decimal("1000"),
                cost_price=Decimal("1.0"),
            )
        )
        db.commit()
        db.close()

        resp = unavailable_client_fixture.get("/api/monitor")
        assert resp.status_code == 503


class TestAlertOrdering:
    def test_danger_sorted_before_warning(self, drop_client):
        """测试 14：混合等级时高风险排在最前（集中度 danger + 涨跌幅 warning）"""
        create_holding(drop_client)
        data = drop_client.get("/api/monitor").json()
        ranks = [{"danger": 0, "warning": 1, "info": 2}[a["level"]] for a in data["alerts"]]
        assert ranks == sorted(ranks)
        assert data["alerts"][0]["type"] == "concentration"
        assert data["alerts"][0]["level"] == "danger"

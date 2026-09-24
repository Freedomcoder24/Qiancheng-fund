"""
Phase 12 候选池 API 测试（内存 SQLite + MockTransport，全 mock 不依赖网络）

覆盖 /api/funds/candidates：
- 排行（rankhandler.aspx JSONP）+ 180 天净值校验 → 结构化候选池
- gp / hh 排行重复代码合并去重
- 单只基金历史数据不足 → 跳过并记 data_issues（不中断整体）
- 排行数据源整体不可用 → 503
- count 参数越界 → 422
- 响应固定包含筛选规则说明与全文免责声明（定位：不是推荐）
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


# ---------------- mock 排行行（rankhandler datas 逗号分隔格式） ----------------

def rank_row(code: str, name: str, ret_1y: str = "35.00") -> str:
    """构造一行排行数据：索引 0=代码 1=名称 3=净值日期 4=单位净值 6=日涨幅
    9=近3月 10=近6月 11=近1年（与 EastMoneyFundSource.get_fund_ranking 对齐）"""
    cols = [
        code, name, "混合型", "2026-09-22", "1.1000", "1.1000",
        "1.50", "-", "-", "12.50", "20.00", ret_1y,
    ]
    return ",".join(cols)


def rank_jsonp(rows: list[str], total: int) -> str:
    """var rankData = {datas:[...],allRecords:N}; 形式的 JSONP 文本
    （忠实还原真实格式：键名无引号的 JS 对象字面量，非严格 JSON）"""
    datas = ",".join(f'"{r}"' for r in rows)
    return (
        "var rankData = {datas:[" + datas + "],allRecords:" + str(total)
        + ",pageIndex:1,pageSize:15};"
    )


# ---------------- mock 净值行（180 天校验用，同 test_monitor_api 口径） ----------------

def build_rows(daily_changes: list[str], start_nav: str = "1.0000") -> list[dict]:
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


CALM_ROWS = build_rows(["0.10"] * 40)  # 温和上涨 → 回撤 0.00%、大波动 0 天


def make_candidates_handler(
    gp_rows: list[str],
    hh_rows: list[str],
    navs_by_code: dict[str, list[dict]],
    ranking_error: bool = False,
):
    """mock handler：rankhandler（ft=gp/hh）+ lsjz（180 天区间）"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "rankhandler" in url:
            if ranking_error:
                raise httpx.ConnectError("connection refused", request=request)
            ft = request.url.params.get("ft", "gp")
            if ft == "gp":
                return httpx.Response(200, text=rank_jsonp(gp_rows, len(gp_rows)))
            return httpx.Response(200, text=rank_jsonp(hh_rows, len(hh_rows)))
        if "lsjz" in url:
            params = request.url.params
            code = params.get("fundCode", "")
            rows = navs_by_code.get(code, [])
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
        return httpx.Response(404, json={"error": "unknown url"})

    return handler


GP_ROWS = [rank_row("000001", "华夏成长混合"), rank_row("002974", "广发信息技术联接C")]
HH_ROWS = [rank_row("161725", "招商中证白酒指数(LOF)A"), rank_row("000001", "华夏成长混合")]
ALL_NAVS = {
    "000001": CALM_ROWS,
    "002974": CALM_ROWS,
    "161725": CALM_ROWS,
}


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


candidates_client = make_client_fixture(
    make_candidates_handler(GP_ROWS, HH_ROWS, ALL_NAVS)
)
# 002974 无历史净值 → 历史校验失败，从候选池移除
insufficient_client = make_client_fixture(make_candidates_handler(
    GP_ROWS, HH_ROWS, {k: v for k, v in ALL_NAVS.items() if k != "002974"}
))
ranking_error_client = make_client_fixture(
    make_candidates_handler(GP_ROWS, HH_ROWS, ALL_NAVS, ranking_error=True)
)


# ---------------- 测试 ----------------


def test_candidates_success(candidates_client):
    """测试 1：排行 + 历史校验 → 结构化候选池（含筛选规则与免责声明）"""
    resp = candidates_client.get("/api/funds/candidates")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 3
    assert data["ranking_total"] == 4  # gp 2 + hh 2
    assert data["nav_date"] == "2026-09-22"
    assert data["data_issues"] == []
    assert data["disclaimer"] == GLOBAL_DISCLAIMER
    # 定位话术：筛选规则中明确"不构成任何推荐"
    assert "不构成任何推荐" in data["screening_rule"]

    item = next(c for c in data["candidates"] if c["code"] == "000001")
    assert item["name"] == "华夏成长混合"
    # 000001 首次出现在 gp（股票型）排行 → 去重保留首次出现的类别
    assert item["fund_type"] == "股票型"
    hh_item = next(c for c in data["candidates"] if c["code"] == "161725")
    assert hh_item["fund_type"] == "混合型"
    assert item["return_1y"] == 35.0
    # 180 天校验指标来自 mock 净值（温和上涨 → 回撤 0、大波动 0；
    # 序列首行 1.0010 → 最新 1.0408，区间收益 = 3.98%）
    assert item["return_180d"] == 3.98
    assert item["max_drawdown_180d"] == 0.0
    assert item["volatility_days_30d"] == 0
    assert item["history_end"] == date.today().isoformat()


def test_candidates_dedupe_across_types(candidates_client):
    """测试 2：gp / hh 排行重复代码合并去重（000001 只出现一次）"""
    data = candidates_client.get("/api/funds/candidates").json()
    codes = [c["code"] for c in data["candidates"]]
    assert len(codes) == len(set(codes))
    assert codes == ["000001", "002974", "161725"]  # gp 在前，重复保留首次出现


def test_candidates_insufficient_history_skipped(insufficient_client):
    """测试 3：单只基金历史净值不足 → 跳过并记 data_issues，其余正常"""
    resp = insufficient_client.get("/api/funds/candidates")
    assert resp.status_code == 200
    data = resp.json()
    codes = [c["code"] for c in data["candidates"]]
    assert codes == ["000001", "161725"]
    assert data["count"] == 2
    assert data["data_issues"]
    assert "002974" in "".join(data["data_issues"])


def test_candidates_ranking_unavailable_503(ranking_error_client):
    """测试 4：排行接口整体不可用 → 503"""
    resp = ranking_error_client.get("/api/funds/candidates")
    assert resp.status_code == 503


@pytest.mark.parametrize("bad_count", [4, 31])
def test_candidates_count_validation(candidates_client, bad_count):
    """测试 5：count 越界（<5 或 >30）→ 422"""
    resp = candidates_client.get(f"/api/funds/candidates?count={bad_count}")
    assert resp.status_code == 422


# ---------------- Phase 13：筛选条件过滤 ----------------


def test_candidates_filter_by_min_return(candidates_client):
    """测试 6：近 1 年收益下限过滤（全部低于下限 → 空池 + data_issues 汇总说明）"""
    # 默认 ret_1y=35 → 下限 40 全被过滤
    resp = candidates_client.put("/api/candidates/filter", json={
        "include_stock": True, "include_mixed": True, "min_return_1y": 40,
    })
    assert resp.status_code == 200
    data = candidates_client.get("/api/funds/candidates").json()
    assert data["count"] == 0
    assert any("未进入候选池" in s for s in data["data_issues"])
    # 规则文案动态反映条件，且保留"不构成任何推荐"
    assert "40.00%" in data["screening_rule"]
    assert "不构成任何推荐" in data["screening_rule"]


def test_candidates_filter_partial_keep(candidates_client):
    """测试 7：下限过滤部分保留（35 ≥ 30 保留），不足 count 如实返回不补位"""
    candidates_client.put("/api/candidates/filter", json={
        "include_stock": True, "include_mixed": True, "min_return_1y": 30,
    })
    data = candidates_client.get("/api/funds/candidates").json()
    assert data["count"] == 3  # 全部 35 ≥ 30 保留；count 上限 20 远大于 3，不补位
    assert data["candidates"]  # 非空
    assert all(c["return_1y"] >= 30 for c in data["candidates"])


def test_candidates_filter_by_type(candidates_client):
    """测试 8：仅勾混合型 → 只请求 hh 排行，000001（gp 首现）被类型过滤"""
    candidates_client.put("/api/candidates/filter", json={
        "include_stock": False, "include_mixed": True, "min_return_1y": 0,
    })
    data = candidates_client.get("/api/funds/candidates").json()
    codes = [c["code"] for c in data["candidates"]]
    # HH_ROWS 含 161725 + 000001（hh 排行里 000001 类型跟随请求 ft=hh → 混合型，保留）
    assert set(codes) == {"161725", "000001"}
    assert all(c["fund_type"] == "混合型" for c in data["candidates"])
    assert "混合型" in data["screening_rule"]

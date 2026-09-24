"""
Phase 8 后台自动化测试（内存 SQLite + MockTransport + mock 模型调用，全 mock 不依赖网络）

mock 三层（与 test_monitor_api / test_ai_api 同口径）：
1. 数据库：dependency_overrides 覆盖 get_db，使用内存 SQLite
2. 基金数据源：monkeypatch fund_service.data_source 为 MockTransport 数据源
3. AI 模型调用：monkeypatch ai_service._call_model，不发真实请求

覆盖点：
- 快照 / 日报 API（无记录 → null；有记录 → 完整内容 + 免责声明）
- 手动触发 POST /api/auto/run（与定时任务同一逻辑）：
  快照保存、提醒内容、日报每天一份、AI 失败 / 数据源失败不崩溃（指令 7）
- 定时配置解析（间隔可配、非法值兜底、开关可关，指令 9）
- 生命周期：lifespan 启动创建任务、关闭时取消；AUTO_TASK_ENABLED=false 不启动
"""
import asyncio
import json
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.database import Base, get_db
from app.database.models import DailyReport, MonitorSnapshot, PortfolioHolding
from app.main import app
from app.services import ai_service, automation_service, fund_service
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


# ---------------- mock 净值数据（日期动态生成，与 test_monitor_api 同理） ----------------

FUND_NAMES = {"000001": "华夏成长混合"}

AI_MODEL_JSON = (
    '{"today_summary": "账户今日估算收益 +30.00 元，整体平稳。",'
    ' "profit_sources": "收益主要来自华夏成长混合。",'
    ' "risk_warnings": ["单只基金市值占比 100%，集中度较高。"]}'
)


def build_rows(daily_changes: list[str], start_nav: str = "1.0000") -> list[dict]:
    """按时间正序的日涨跌幅列表 → lsjz 行（日期倒序，日期基于今天动态生成）"""
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


CALM_CHANGES = ["0.10"] * 40  # 温和行情：不触发任何监控提醒


def make_handler(rows_by_code: dict[str, list[dict]]):
    """mock handler：lsjz（分页 + 日期过滤）+ FundSearchAPI"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "lsjz" in url:
            params = request.url.params
            rows = rows_by_code.get(params.get("fundCode", ""), [])
            if not rows:
                return httpx.Response(200, json={"TotalCount": 0, "Data": {"LSJZList": []}})
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
                    "CODE": code, "NAME": FUND_NAMES[code], "CATEGORY": "700",
                    "FundBaseInfo": {"FundType": "混合型-偏股"},
                }]})
            return httpx.Response(200, json={"Datas": []})
        return httpx.Response(404, json={"error": "unknown url"})

    return handler


def unavailable_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


# ---------------- 客户端 fixture ----------------

AI_ENV = {
    "AI_API_KEY": "test-key-for-pytest",
    "AI_BASE_URL": "https://ai.test/v1",
    "AI_MODEL": "test-model",
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


@pytest.fixture()
def auto_client(monkeypatch):
    """自动检查标准场景：AI 已配置 + 模型调用 mock 成功 + 温和行情"""
    for key, value in AI_ENV.items():
        monkeypatch.setenv(key, value)

    async def fake_call(user_prompt: str) -> str:
        assert "test-key-for-pytest" not in user_prompt  # Key 不进 prompt
        return AI_MODEL_JSON

    monkeypatch.setattr(ai_service, "_call_model", fake_call)
    yield from _make_client(monkeypatch, make_handler(
        {code: build_rows(CALM_CHANGES) for code in FUND_NAMES}
    ))


@pytest.fixture()
def unavailable_client(monkeypatch):
    """数据源整体不可用场景（AI 配置正常，轮不到调用）"""
    for key, value in AI_ENV.items():
        monkeypatch.setenv(key, value)
    yield from _make_client(monkeypatch, unavailable_handler)


def create_holding(client: TestClient, code: str = "000001") -> dict:
    resp = client.post(
        "/api/portfolio/holdings",
        json={"fund_code": code, "shares": 1000, "cost_price": 1.0},
    )
    assert resp.status_code == 201
    return resp.json()


# ---------------- 快照 / 日报查询 API ----------------


def test_latest_snapshot_empty(auto_client):
    """测试 1：从未执行过自动检查 → 200 + null（前端显示引导，不报错）"""
    resp = auto_client.get("/api/auto/latest")
    assert resp.status_code == 200
    assert resp.json() is None


def test_daily_report_empty(auto_client):
    """测试 2：还没有日报 → 200 + null"""
    resp = auto_client.get("/api/auto/daily-report")
    assert resp.status_code == 200
    assert resp.json() is None


def test_run_saves_snapshot_and_report(auto_client):
    """测试 3：手动触发一轮 → 快照 + 日报落库，查询接口能取回完整内容"""
    create_holding(auto_client)
    resp = auto_client.post("/api/auto/run")
    assert resp.status_code == 200
    data = resp.json()

    # 快照：单只持仓必然触发集中度提醒（占比 100% 高风险）
    assert data["snapshot"] is not None
    assert data["snapshot"]["checked_count"] == 1
    assert data["snapshot"]["alert_count"] >= 1
    assert any(a["type"] == "concentration" for a in data["snapshot"]["alerts"])
    assert data["snapshot"]["executed_at"]
    assert data["errors"] == []

    # 日报：当天生成一份，内容 / 免责声明齐全
    assert data["daily_report_generated"] is True
    report = data["daily_report"]
    assert report["report_date"] == date.today().isoformat()
    assert report["model"] == "test-model"
    assert report["content"]["today_summary"].startswith("账户今日估算收益")
    assert "华夏成长混合" in report["content"]["profit_sources"]
    assert "不构成投资建议" in report["disclaimer"]

    # 查询接口取回同一份快照与日报
    latest = auto_client.get("/api/auto/latest").json()
    assert latest["alert_count"] == data["snapshot"]["alert_count"]
    assert latest["alerts"][0]["fund_code"] == "000001"
    assert latest["alerts"][0]["level_label"] in {"提示", "注意", "高风险"}
    got_report = auto_client.get("/api/auto/daily-report").json()
    assert got_report["report_date"] == report["report_date"]
    assert got_report["content"]["risk_warnings"] == report["content"]["risk_warnings"]

    # 落库抽查：两张表各有一条记录，JSON 字段合法
    db = TestSession()
    try:
        assert db.query(DailyReport).count() == 1
        snap_row = db.query(MonitorSnapshot).first()
        assert json.loads(snap_row.alerts_json)[0]["type"] == "concentration"
    finally:
        db.close()


def test_daily_report_once_per_day(auto_client):
    """测试 4：同一天第二轮检查不再重复生成日报（每天最多一份）"""
    create_holding(auto_client)
    first = auto_client.post("/api/auto/run").json()
    assert first["daily_report_generated"] is True

    second = auto_client.post("/api/auto/run").json()
    assert second["daily_report_generated"] is False
    assert second["daily_report"] is None
    # 已有报告仍可通过查询接口取到
    assert auto_client.get("/api/auto/daily-report").json() is not None

    db = TestSession()
    try:
        assert db.query(DailyReport).count() == 1
    finally:
        db.close()


def test_latest_snapshot_ordering(auto_client):
    """测试 5：多轮检查后 /api/auto/latest 返回最新一份快照"""
    # 第一轮：无持仓（快照 checked_count=0）
    auto_client.post("/api/auto/run")
    create_holding(auto_client)
    # 第二轮：1 只持仓
    auto_client.post("/api/auto/run")
    latest = auto_client.get("/api/auto/latest").json()
    assert latest["checked_count"] == 1
    assert latest["alert_count"] >= 1


# ---------------- 失败隔离（指令 7：任何失败不能让任务崩溃） ----------------


def test_ai_failure_does_not_crash_run(auto_client, monkeypatch):
    """测试 6：AI 未配置时 → 快照照常保存，errors 记录日报失败，不抛 5xx"""
    create_holding(auto_client)
    monkeypatch.delenv("AI_API_KEY")
    resp = auto_client.post("/api/auto/run")
    assert resp.status_code == 200
    data = resp.json()
    assert data["snapshot"] is not None           # 快照不受 AI 失败影响
    assert data["daily_report_generated"] is False
    assert data["daily_report"] is None
    assert any("AI 每日报告生成失败" in e for e in data["errors"])
    # 快照查询接口照常可用
    assert auto_client.get("/api/auto/latest").json() is not None


def test_report_skipped_when_disabled(auto_client, monkeypatch):
    """测试 7：AUTO_DAILY_REPORT_ENABLED=false → 跳过日报且不算错误"""
    create_holding(auto_client)
    monkeypatch.setenv("AUTO_DAILY_REPORT_ENABLED", "false")
    resp = auto_client.post("/api/auto/run")
    data = resp.json()
    assert data["snapshot"] is not None
    assert data["daily_report_generated"] is False
    assert data["errors"] == []


def test_source_failure_saves_failure_snapshot(unavailable_client):
    """测试 8：数据源整体不可用 → 保存"失败记录"快照（不伪造提醒），任务不崩溃"""
    # 直接插库造持仓，让监控在读取净值时撞上不可用数据源
    db = TestSession()
    db.add(PortfolioHolding(
        fund_code="000001", fund_name="华夏成长混合",
        shares=Decimal("1000"), cost_price=Decimal("1.0"),
    ))
    db.commit()
    db.close()

    resp = unavailable_client.post("/api/auto/run")
    assert resp.status_code == 200  # 失败被吞掉，任务层面永远 200 + errors 说明
    data = resp.json()
    assert data["daily_report_generated"] is False
    assert any("监控检查失败" in e for e in data["errors"])
    assert any("AI 每日报告生成失败" in e for e in data["errors"])

    snap = data["snapshot"]
    assert snap is not None
    assert snap["alert_count"] == 0
    assert snap["alerts"] == []
    assert any("自动检查未完成" in i for i in snap["data_issues"])


# ---------------- 定时配置与生命周期（指令 2 / 9） ----------------


def test_scheduler_config(monkeypatch):
    """测试 9：间隔来自环境变量；非法值用默认；过小被钳制；开关可关"""
    # 默认值（清掉环境里的配置，包括 .env 已写入的）
    for name in ("AUTO_TASK_ENABLED", "AUTO_MONITOR_INTERVAL_MINUTES",
                 "AUTO_DAILY_REPORT_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    config = automation_service.get_scheduler_config()
    assert config == {
        "enabled": True,
        "interval_minutes": 60,
        "daily_report_enabled": True,
    }

    # 正常配置
    monkeypatch.setenv("AUTO_MONITOR_INTERVAL_MINUTES", "90")
    monkeypatch.setenv("AUTO_TASK_ENABLED", "false")
    monkeypatch.setenv("AUTO_DAILY_REPORT_ENABLED", "no")
    config = automation_service.get_scheduler_config()
    assert config["interval_minutes"] == 90
    assert config["enabled"] is False
    assert config["daily_report_enabled"] is False

    # 非法值 → 默认；过小 → 钳制到下限 1 分钟
    monkeypatch.setenv("AUTO_MONITOR_INTERVAL_MINUTES", "abc")
    assert automation_service.get_scheduler_config()["interval_minutes"] == 60
    monkeypatch.setenv("AUTO_MONITOR_INTERVAL_MINUTES", "0.2")
    assert automation_service.get_scheduler_config()["interval_minutes"] == 1


def test_scheduler_disabled_no_task(monkeypatch):
    """测试 10：AUTO_TASK_ENABLED=false → start_scheduler 不创建任务"""
    monkeypatch.setenv("AUTO_TASK_ENABLED", "false")
    automation_service.start_scheduler()
    assert automation_service._task is None


def test_scheduler_start_and_stop(monkeypatch):
    """测试 11：start/stop 创建并干净取消后台任务（间隔内不会真正执行检查）"""
    monkeypatch.setenv("AUTO_TASK_ENABLED", "true")
    monkeypatch.setenv("AUTO_MONITOR_INTERVAL_MINUTES", "60")

    async def scenario():
        automation_service.start_scheduler()
        task = automation_service._task
        assert task is not None and not task.done()
        await automation_service.stop_scheduler()
        assert automation_service._task is None
        assert task.cancelled()

    asyncio.run(scenario())


def test_lifespan_starts_and_stops_scheduler(monkeypatch):
    """测试 12：应用 lifespan 启动时创建任务、关闭时停止（指令 2）"""
    monkeypatch.setenv("AUTO_TASK_ENABLED", "true")
    monkeypatch.setenv("AUTO_MONITOR_INTERVAL_MINUTES", "60")
    with TestClient(app):
        assert automation_service._task is not None
        assert not automation_service._task.done()
    assert automation_service._task is None

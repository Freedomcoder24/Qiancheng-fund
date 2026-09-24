"""
Phase 6 AI 分析 API 测试（内存 SQLite + mock 数据源 + mock 模型调用，全 mock 不依赖网络）

mock 三层：
1. 数据库：dependency_overrides 覆盖 get_db，使用内存 SQLite（同 test_performance_api.py）
2. 基金数据源：monkeypatch fund_service.data_source 为 MockTransport 数据源
3. AI 模型调用：monkeypatch ai_service._call_model，不发真实 OpenAI 请求

同时用 monkeypatch.setenv 固定 AI_* 环境变量，保证测试不依赖本机 .env 内容。
"""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.database import Base, get_db
from app.database.models import PortfolioHolding
from app.main import app
from app.services import ai_service, fund_service
from app.services.ai_service import AIUpstreamError
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


# ---------------- mock 净值数据（与 test_performance_api.py 相同口径） ----------------

NAV_ROWS = [
    {"FSRQ": "2026-09-21", "DWJZ": "1.1000", "LJJZ": "1.1000", "JZZZL": "10.00"},
    {"FSRQ": "2026-09-18", "DWJZ": "0.9000", "LJJZ": "0.9000", "JZZZL": "-25.00"},
    {"FSRQ": "2026-09-17", "DWJZ": "1.2000", "LJJZ": "1.2000", "JZZZL": "9.09"},
    {"FSRQ": "2026-09-16", "DWJZ": "1.1000", "LJJZ": "1.1000", "JZZZL": "10.00"},
    {"FSRQ": "2026-09-15", "DWJZ": "1.0000", "LJJZ": "1.0000", "JZZZL": "0.00"},
]


def success_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "lsjz" in url:
        return httpx.Response(
            200, json={"TotalCount": len(NAV_ROWS), "Data": {"LSJZList": NAV_ROWS}}
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


def unavailable_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


# ---------------- AI 环境变量与模型调用 mock ----------------

AI_MODEL_JSON = (
    '{"today_summary": "账户今日估算收益 +30.00 元，整体平稳。",'
    ' "profit_sources": "收益主要来自华夏成长混合。",'
    ' "risk_warnings": ["单只基金市值占比 100%，集中度较高。"]}'
)


@pytest.fixture(autouse=True)
def ai_env(monkeypatch):
    """固定 AI 配置环境变量（不依赖本机 .env；delenv 防止外部泄漏进断言）"""
    monkeypatch.setenv("AI_API_KEY", "test-key-for-pytest")
    monkeypatch.setenv("AI_BASE_URL", "https://ai.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")


def patch_model_call(monkeypatch, content=None, exc=None):
    """替换 ai_service._call_model：content 为返回文本，exc 为抛出的异常"""

    async def fake_call(user_prompt: str) -> str:
        if exc is not None:
            raise exc
        # 附带检查：发给模型的 prompt 里不应包含 API Key
        assert "test-key-for-pytest" not in user_prompt
        return content

    monkeypatch.setattr(ai_service, "_call_model", fake_call)


# ---------------- 客户端 fixture ----------------

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
def client(monkeypatch):
    yield from _make_client(monkeypatch, success_handler)


@pytest.fixture()
def unavailable_client(monkeypatch):
    yield from _make_client(monkeypatch, unavailable_handler)


def create_one_holding(client: TestClient) -> dict:
    """创建一条测试持仓（000001 / 3000 份 / 成本 1.2），返回响应 JSON"""
    resp = client.post(
        "/api/portfolio/holdings",
        json={"fund_code": "000001", "shares": 3000, "cost_price": 1.2},
    )
    assert resp.status_code == 201
    return resp.json()


# ---------------- status 测试 ----------------


def test_ai_status_never_exposes_key(client):
    """测试 1：status 接口响应中绝不能出现 API Key"""
    resp = client.get("/api/ai/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "api_key" not in data
    assert "AI_API_KEY" not in data
    # 全响应文本中不得出现真实 key 值
    assert "test-key-for-pytest" not in resp.text


def test_ai_status_fields(client):
    """测试 2：status 返回 configured / model / base_url 三字段"""
    resp = client.get("/api/ai/status")
    data = resp.json()
    assert data["configured"] is True
    assert data["model"] == "test-model"
    assert data["base_url"] == "https://ai.test/v1"


# ---------------- analysis 错误分支测试 ----------------


def test_analysis_not_configured(client, monkeypatch):
    """测试 3：未配置 AI（缺环境变量）→ 503"""
    monkeypatch.delenv("AI_API_KEY")
    resp = client.post("/api/ai/analysis")
    assert resp.status_code == 503
    assert "未配置" in resp.json()["detail"]


def test_analysis_no_holdings(client):
    """测试 4：无持仓 → 400，明确提示先添加持仓（模型调用不应发生）"""
    resp = client.post("/api/ai/analysis")
    assert resp.status_code == 400
    assert "暂无持仓" in resp.json()["detail"]


def test_analysis_upstream_error(client, monkeypatch):
    """测试 5：AI 上游调用失败 → 502"""
    create_one_holding(client)
    patch_model_call(monkeypatch, exc=AIUpstreamError("AI 服务调用失败：连接超时"))
    resp = client.post("/api/ai/analysis")
    assert resp.status_code == 502


def test_analysis_invalid_json(client, monkeypatch):
    """测试 6：模型返回非 JSON 内容 → 502"""
    create_one_holding(client)
    patch_model_call(monkeypatch, content="抱歉，我无法完成这次分析。")
    resp = client.post("/api/ai/analysis")
    assert resp.status_code == 502
    assert "无法解析" in resp.json()["detail"]


def test_analysis_data_source_unavailable(unavailable_client, monkeypatch):
    """测试 7：组装数据时基金数据源不可用 → 503

    数据源不可用时无法通过 API 创建持仓（创建时会查名称），
    这里直接向内存库插入一条持仓，让分析流程在读取净值时撞上不可用数据源。
    """
    db = TestSession()
    db.add(
        PortfolioHolding(
            fund_code="000001", fund_name="华夏成长混合",
            shares=3000, cost_price=1.2,
        )
    )
    db.commit()
    db.close()
    patch_model_call(monkeypatch, content=AI_MODEL_JSON)  # 不应被调用到
    resp = unavailable_client.post("/api/ai/analysis")
    assert resp.status_code == 503


# ---------------- analysis 成功测试 ----------------


def test_analysis_success_plain_json(client, monkeypatch):
    """测试 8：模型返回纯 JSON → 200，三块内容 + 固定免责声明齐全"""
    create_one_holding(client)
    patch_model_call(monkeypatch, content=AI_MODEL_JSON)
    resp = client.post("/api/ai/analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "test-model"
    assert data["today_summary"].startswith("账户今日估算收益")
    assert "华夏成长混合" in data["profit_sources"]
    assert data["risk_warnings"] == ["单只基金市值占比 100%，集中度较高。"]
    # 免责声明固定附带
    assert "不构成投资建议" in data["disclaimer"]
    assert "自动交易" in data["disclaimer"]
    # 数据日期来自 mock 净值
    assert data["data_date"] == "2026-09-21"
    assert data["generated_at"]  # 生成时间已填


def test_analysis_success_fenced_json(client, monkeypatch):
    """测试 9：模型返回 ```json 代码块包裹 → 正常剥壳解析"""
    create_one_holding(client)
    fenced = "```json\n" + AI_MODEL_JSON + "\n```"
    patch_model_call(monkeypatch, content=fenced)
    resp = client.post("/api/ai/analysis")
    assert resp.status_code == 200
    assert resp.json()["today_summary"]


def test_analysis_missing_fields_defaults(client, monkeypatch):
    """测试 10：模型 JSON 缺字段 → 安全默认值而不是 500"""
    create_one_holding(client)
    patch_model_call(monkeypatch, content='{"today_summary": "只有总结"}')
    resp = client.post("/api/ai/analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["today_summary"] == "只有总结"
    assert data["profit_sources"] == ""
    assert data["risk_warnings"] == []

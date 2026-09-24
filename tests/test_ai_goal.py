"""
Phase 12 AI 目标结论 / 候选池解读 测试（内存 SQLite + mock 数据源 + mock 模型调用）

mock 三层（同 test_ai_api.py 模式）：
1. 数据库：dependency_overrides 覆盖 get_db，使用内存 SQLite
2. 基金数据源：MockTransport（lsjz + FundSearchAPI + rankhandler）
3. AI 模型调用：monkeypatch ai_service._call_model（支持 system_prompt 参数）

重点覆盖用户红线：
- 目标结论 conclusion 只允许 near_target / target_achieved / risk_attention，
  非法枚举保守回退 risk_attention，不产生任何交易指令
- 候选池解读含预测性表述（"后续有望上涨"等）→ 解析层直接拒绝（502）
"""
import httpx
import json
import pytest
from datetime import date, timedelta
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.database import Base, get_db
from app.main import app
from app.services import ai_service, fund_service
from app.services.ai_service import AIUpstreamError
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


# ---------------- mock 数据（净值 + 排行） ----------------

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


CALM_ROWS = build_rows(["0.10"] * 40)


def rank_jsonp(rows: list[str], total: int) -> str:
    """var rankData = {datas:[...],allRecords:N}; 形式的 JSONP 文本
    （忠实还原真实格式：键名无引号的 JS 对象字面量，非严格 JSON）"""
    datas = ",".join(f'"{r}"' for r in rows)
    return (
        "var rankData = {datas:[" + datas + "],allRecords:" + str(total)
        + ",pageIndex:1,pageSize:15};"
    )


def rank_row(code: str, name: str) -> str:
    cols = [
        code, name, "混合型", "2026-09-22", "1.1000", "1.1000",
        "1.50", "-", "-", "12.50", "20.00", "35.00",
    ]
    return ",".join(cols)


def success_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "rankhandler" in url:
        ft = request.url.params.get("ft", "gp")
        rows = [rank_row("000001", "华夏成长混合")] if ft == "gp" else [
            rank_row("161725", "招商中证白酒指数(LOF)A")
        ]
        return httpx.Response(200, text=rank_jsonp(rows, len(rows)))
    if "lsjz" in url:
        params = request.url.params
        code = params.get("fundCode", "")
        rows = CALM_ROWS if code in ("000001", "161725") else []
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
        return httpx.Response(200, json={"Datas": [{
            "CODE": code,
            "NAME": "华夏成长混合",
            "CATEGORY": "700",
            "FundBaseInfo": {"FundType": "混合型-偏股"},
        }]})
    return httpx.Response(404, json={"error": "unknown url"})


# ---------------- AI 环境变量与模型调用 mock ----------------

AI_MODEL_JSON = json.dumps({
    "conclusion": "near_target",
    "reason": "当前收益率距离目标收益率还有 5.92 个百分点，尚处于观察区间。",
    "risks": ["近 90 天组合回撤为 0.00%，但历史数据不预示未来表现。"],
}, ensure_ascii=False)

CANDIDATE_MODEL_JSON = json.dumps({
    "overview": "候选池中的基金均为按近 1 年历史收益排序后符合当前量化筛选条件的基金。",
    "highlights": [
        {"code": "000001", "reason": "近一年历史表现较高，符合当前量化筛选条件。"},
        {"code": "999999", "reason": "这个代码不在候选池中，应被丢弃。"},
    ],
    "cautions": ["近期历史波动较大。"],
    "market_background": "宏观背景为模型公开知识，非实时，可能过时。",
}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def ai_env(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "test-key-for-pytest")
    monkeypatch.setenv("AI_BASE_URL", "https://ai.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")


def patch_model_call(monkeypatch, content=None, exc=None):
    """替换 ai_service._call_model：记录调用参数，content 为返回文本，exc 为抛出的异常"""
    calls = {}

    async def fake_call(user_prompt: str, *args, **kwargs) -> str:
        calls["user_prompt"] = user_prompt
        calls["system_prompt"] = kwargs.get("system_prompt")
        if exc is not None:
            raise exc
        assert "test-key-for-pytest" not in user_prompt  # prompt 不得包含 API Key
        return content

    monkeypatch.setattr(ai_service, "_call_model", fake_call)
    return calls


# ---------------- 客户端 fixture ----------------

def _make_client(monkeypatch):
    Base.metadata.create_all(bind=test_engine)
    source = EastMoneyFundSource(transport=httpx.MockTransport(success_handler))
    monkeypatch.setattr(fund_service, "data_source", source)
    fund_service.clear_cache()
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    fund_service.clear_cache()
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture()
def client(monkeypatch):
    yield from _make_client(monkeypatch)


def set_goal(client: TestClient, rate: float = 10):
    resp = client.put("/api/goal", json={"target_return_rate": rate})
    assert resp.status_code == 200


def create_holding(client: TestClient):
    resp = client.post(
        "/api/portfolio/holdings",
        json={"fund_code": "000001", "shares": 1000, "cost_price": 1.0},
    )
    assert resp.status_code == 201


# ---------------- AI 目标结论 ----------------


def test_goal_conclusion_not_configured(client, monkeypatch):
    """测试 1：未配置 AI → 503"""
    monkeypatch.delenv("AI_API_KEY")
    resp = client.post("/api/ai/goal-conclusion")
    assert resp.status_code == 503
    assert "未配置" in resp.json()["detail"]


def test_goal_conclusion_goal_not_set(client):
    """测试 2：已配置 AI 但未设置目标 → 400（提示先设置目标）"""
    create_holding(client)
    resp = client.post("/api/ai/goal-conclusion")
    assert resp.status_code == 400
    assert "尚未设置" in resp.json()["detail"]


def test_goal_conclusion_success(client, monkeypatch):
    """测试 3：成功 → 枚举结论 + 标签 + 免责声明；prompt 含后端数据与专属系统提示"""
    set_goal(client, 10)
    create_holding(client)
    calls = patch_model_call(monkeypatch, content=AI_MODEL_JSON)
    resp = client.post("/api/ai/goal-conclusion")
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "test-model"
    assert data["conclusion"] == "near_target"
    assert data["conclusion_label"] == "接近目标"
    assert data["risks"]
    assert data["low_confidence"] is False
    assert data["data_date"] == date.today().isoformat()
    assert data["disclaimer"] == GLOBAL_DISCLAIMER
    # 发给模型的是目标专属系统提示，且包含后端计算的目标进度数据
    assert calls["system_prompt"] == ai_service.GOAL_SYSTEM_PROMPT
    assert "target_return_rate" in calls["user_prompt"]
    assert "gap_percent" in calls["user_prompt"]


def test_goal_conclusion_illegal_enum_fallback(client, monkeypatch):
    """测试 4：模型输出非法枚举（如 should_sell）→ 保守回退 risk_attention + 低置信"""
    set_goal(client, 10)
    create_holding(client)
    bad = json.dumps({
        "conclusion": "should_sell", "reason": "建议止盈。", "risks": [],
    }, ensure_ascii=False)
    patch_model_call(monkeypatch, content=bad)
    resp = client.post("/api/ai/goal-conclusion")
    assert resp.status_code == 200
    data = resp.json()
    assert data["conclusion"] == "risk_attention"
    assert data["conclusion_label"] == "风险需要关注"
    assert data["low_confidence"] is True


def test_goal_conclusion_fenced_json(client, monkeypatch):
    """测试 5：模型返回 ```json 围栏 → 正常剥壳解析"""
    set_goal(client, 10)
    create_holding(client)
    fenced = "```json\n" + AI_MODEL_JSON + "\n```"
    patch_model_call(monkeypatch, content=fenced)
    resp = client.post("/api/ai/goal-conclusion")
    assert resp.status_code == 200
    assert resp.json()["conclusion"] == "near_target"


def test_goal_conclusion_upstream_error(client, monkeypatch):
    """测试 6：AI 上游调用失败 → 502"""
    set_goal(client, 10)
    create_holding(client)
    patch_model_call(monkeypatch, exc=AIUpstreamError("AI 服务调用失败：连接超时"))
    resp = client.post("/api/ai/goal-conclusion")
    assert resp.status_code == 502


# ---------------- AI 候选池解读 ----------------


def test_candidate_analysis_success(client, monkeypatch):
    """测试 7：成功 → 入选原因只解释筛选条件；候选池外的编造代码被丢弃"""
    calls = patch_model_call(monkeypatch, content=CANDIDATE_MODEL_JSON)
    resp = client.post("/api/ai/candidate-analysis")
    assert resp.status_code == 200
    data = resp.json()
    assert data["model"] == "test-model"
    assert data["overview"].startswith("候选池中的基金")
    # highlights 只保留候选池内的代码，编造的 999999 被剔除
    assert len(data["highlights"]) == 1
    assert data["highlights"][0]["code"] == "000001"
    assert "符合当前量化筛选条件" in data["highlights"][0]["reason"]
    assert data["cautions"] == ["近期历史波动较大。"]
    assert "非实时" in data["market_background"]
    assert data["disclaimer"] == GLOBAL_DISCLAIMER
    assert calls["system_prompt"] == ai_service.CANDIDATE_SYSTEM_PROMPT


def test_candidate_analysis_predictive_phrase_rejected(client, monkeypatch):
    """测试 8（红线）：overview 含"后续有望上涨"→ 拒绝整次解读（502），不上屏预测内容"""
    bad = json.dumps({
        "overview": "这些基金后续有望上涨。",
        "highlights": [{"code": "000001", "reason": "近一年历史表现较高。"}],
        "cautions": [],
        "market_background": "宏观背景（非实时）。",
    }, ensure_ascii=False)
    patch_model_call(monkeypatch, content=bad)
    resp = client.post("/api/ai/candidate-analysis")
    assert resp.status_code == 502
    assert "预测性" in resp.json()["detail"]


def test_candidate_analysis_highlight_redline_dropped(client, monkeypatch):
    """测试 9（红线）：单条 highlight 含"盈利概率较高"→ 只丢弃该条，其余保留"""
    content = json.dumps({
        "overview": "候选池为符合当前量化筛选条件的基金。",
        "highlights": [
            {"code": "000001", "reason": "盈利概率较高。"},
            {"code": "161725", "reason": "历史回撤较明显，符合当前量化筛选条件。"},
        ],
        "cautions": ["近期历史波动较大。"],
        "market_background": "宏观背景（非实时）。",
    }, ensure_ascii=False)
    patch_model_call(monkeypatch, content=content)
    resp = client.post("/api/ai/candidate-analysis")
    assert resp.status_code == 200
    highlights = resp.json()["highlights"]
    assert [h["code"] for h in highlights] == ["161725"]


def test_candidate_analysis_not_configured(client, monkeypatch):
    """测试 10：未配置 AI → 503"""
    monkeypatch.delenv("AI_API_KEY")
    resp = client.post("/api/ai/candidate-analysis")
    assert resp.status_code == 503

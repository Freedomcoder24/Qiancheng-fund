"""
Phase 15 Agent（Tool Calling）测试（内存 SQLite + mock 数据源 + mock 模型多轮脚本）

mock 三层（同 test_ai_goal.py 模式）：
1. 数据库：dependency_overrides 覆盖 get_db，使用内存 SQLite
2. 基金数据源：MockTransport（lsjz + FundSearchAPI）
3. 模型调用：monkeypatch agent_service._chat 为脚本化多轮假模型
   （按脚本依次返回"工具调用"或"最终回复"，并记录每轮收到的 messages）

重点覆盖：
- Agent 循环：工具调用 → 结果回填 → 最终回复；达到最大轮数 → 502
- 6 个 Tool 均可被调用并执行（含真实执行结果校验）
- 工具参数非法（period=999 / 代码格式错 / holding_id 缺失）→ error 回给模型自纠
- 红线：最终回复含预测性表述 → 整次拒绝（502），不做片段丢弃
- 未配置 AI → 503；模型上游异常 → 502；空白消息 → 400
"""
import asyncio
import httpx
import json
import pytest
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.database import Base, get_db
from app.main import app
from app.services import agent_service, fund_service
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


# ---------------- mock 数据源（净值 + 搜索） ----------------

def build_rows(daily_changes: list[str], start_nav: str = "1.0000") -> list[dict]:
    today = date.today()
    nav = Decimal(start_nav)
    navs: list[Decimal] = []
    for pct in daily_changes:
        nav = nav * (Decimal("1") + Decimal(pct) / Decimal("100"))
        navs.append(nav)

    n = len(daily_changes)
    rows = []
    for k in range(n):
        rows.append({
            "FSRQ": (today - timedelta(days=n - 1 - k)).isoformat(),
            "DWJZ": f"{navs[k]:.4f}",
            "LJJZ": f"{navs[k]:.4f}",
            "JZZZL": daily_changes[k],
        })
    rows.reverse()
    return rows


CALM_ROWS = build_rows(["0.10"] * 40)


def success_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "lsjz" in url:
        params = request.url.params
        code = params.get("fundCode", "")
        rows = CALM_ROWS if code == "000001" else []
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


# ---------------- 模型多轮脚本 mock ----------------

def make_tool_call(call_id: str, name: str, arguments="{}"):
    """构造一个 openai 风格的 tool_call 对象（duck typing）"""
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


class ScriptedModel:
    """脚本化假模型：按脚本依次返回轮次结果，记录每轮收到的 messages

    turns 中每项为 (content, tool_calls)：tool_calls 非空表示本轮要求调工具，
    为空表示本轮给出最终回复。脚本耗尽后再被调用视为测试失败。
    """

    def __init__(self, turns: list[tuple[str | None, list]]):
        self.turns = list(turns)
        self.calls: list[list] = []

    async def __call__(self, config: dict, messages: list):
        # 存快照（messages 是 run_agent 持续 append 的同一列表，必须拷贝）
        self.calls.append(list(messages))
        assert config["api_key"] == "test-key-for-pytest"
        assert config["model"] == "test-model"
        for m in messages:
            text = json.dumps(m, ensure_ascii=False, default=str)
            assert "test-key-for-pytest" not in text  # 上下文不得包含 API Key
        if not self.turns:
            raise AssertionError("模型调用次数超出脚本轮数")
        content, tool_calls = self.turns.pop(0)
        return SimpleNamespace(content=content, tool_calls=tool_calls)


@pytest.fixture(autouse=True)
def ai_env(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "test-key-for-pytest")
    monkeypatch.setenv("AI_BASE_URL", "https://ai.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")


def patch_chat(monkeypatch, model: ScriptedModel) -> ScriptedModel:
    monkeypatch.setattr(agent_service, "_chat", model)
    return model


# ---------------- 客户端 fixture ----------------

@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=test_engine)
    source = EastMoneyFundSource(transport=httpx.MockTransport(success_handler))
    monkeypatch.setattr(fund_service, "data_source", source)
    fund_service.clear_cache()
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    fund_service.clear_cache()
    Base.metadata.drop_all(bind=test_engine)


def create_holding(client: TestClient) -> int:
    resp = client.post(
        "/api/portfolio/holdings",
        json={"fund_code": "000001", "shares": 1000, "cost_price": 1.0},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def chat(client: TestClient, message: str):
    return client.post("/api/agent/chat", json={"message": message})


# ---------------- 基础行为 ----------------


def test_agent_empty_message_400(client):
    """测试 1：空白消息 → 400"""
    resp = chat(client, "   ")
    assert resp.status_code == 400


def test_agent_not_configured_503(client, monkeypatch):
    """测试 2：未配置 AI → 503"""
    monkeypatch.delenv("AI_API_KEY")
    resp = chat(client, "帮我分析一下持仓")
    assert resp.status_code == 503
    assert "未配置" in resp.json()["detail"]


def test_agent_direct_answer_without_tools(client, monkeypatch):
    """测试 3：模型不调工具直接回答 → 200，tools_used 为空，1 轮"""
    model = patch_chat(monkeypatch, ScriptedModel([
        ("请告诉我您想分析哪只基金，或需要了解持仓的整体情况。", []),
    ]))
    resp = chat(client, "你好")
    assert resp.status_code == 200
    data = resp.json()
    assert data["rounds"] == 1
    assert data["tools_used"] == []
    assert "基金" in data["answer"]
    assert data["disclaimer"] == GLOBAL_DISCLAIMER
    assert data["model"] == "test-model"
    # 首轮上下文：system 提示 + 用户消息
    first = model.calls[0]
    assert first[0]["role"] == "system"
    assert first[1] == {"role": "user", "content": "你好"}
    assert "工具" in first[0]["content"]


# ---------------- 持仓类工具链路 ----------------


def test_agent_holding_tools_chain(client, monkeypatch):
    """测试 4：持仓分析链路 get_holdings → get_portfolio_summary → 最终回复"""
    create_holding(client)
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [make_tool_call("call_1", "get_holdings")]),
        (None, [make_tool_call("call_2", "get_portfolio_summary")]),
        ("根据工具返回的数据：您当前持有 1 只基金，投入 1000.00 元，"
         "累计收益率 11.00%，整体处于盈利状态；历史数据不预示未来表现。",
         []),
    ]))
    resp = chat(client, "帮我分析一下我现在的持仓")
    assert resp.status_code == 200
    data = resp.json()
    assert data["rounds"] == 3
    assert [t["tool"] for t in data["tools_used"]] == [
        "get_holdings", "get_portfolio_summary",
    ]
    assert data["tools_used"][0]["arguments"] == {}

    # 第二轮上下文应包含：assistant(tool_calls) + tool 结果（真实执行数据）
    second = model.calls[1]
    assert second[2]["role"] == "assistant"
    assert second[2]["tool_calls"][0]["function"]["name"] == "get_holdings"
    tool_msg = json.loads(json.dumps(second[3]))
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_call_id"] == "call_1"
    tool_payload = json.loads(tool_msg["content"])
    assert tool_payload["count"] == 1
    assert tool_payload["holdings"][0]["fund_code"] == "000001"
    assert tool_payload["holdings"][0]["profit_rate"] == pytest.approx(
        float((Decimal("1.001") ** 40 - 1) * 100), abs=0.1
    )  # mock 净值连续 40 天 +0.10%：收益率 ≈ (1.001^40 - 1) × 100

    # 第三轮上下文应包含汇总工具的真实结果（索引 5 = 第二个 tool 结果）
    third_payload = json.loads(model.calls[2][5]["content"])
    assert third_payload["holding_count"] == 1


def test_agent_holding_history_tool(client, monkeypatch):
    """测试 5：持仓历史回放工具 get_holding_history 执行成功"""
    holding_id = create_holding(client)
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [make_tool_call(
            "call_1", "get_holding_history",
            json.dumps({"holding_id": holding_id, "period": 7}),
        )]),
        ("近 7 天该持仓的模拟市值整体上行（按当前份额回放历史净值，"
         "不代表真实历史账户资产）。", []),
    ]))
    resp = chat(client, f"看看持仓 {holding_id} 最近一周的走势")
    assert resp.status_code == 200
    data_tools = resp.json()["tools_used"]
    assert data_tools
    assert data_tools[0]["tool"] == "get_holding_history"
    assert data_tools[0]["arguments"] == {"holding_id": holding_id, "period": 7}
    # 工具真实执行：回放点数 = 近 7 个交易日数（mock 数据连续）
    history_payload = json.loads(model.calls[1][3]["content"])
    assert history_payload["period"] == 7
    assert history_payload["fund_code"] == "000001"
    assert len(history_payload["points"]) > 0
    assert "market_value" in history_payload["points"][0]


# ---------------- 基金类工具链路 ----------------


def test_agent_fund_detail_and_performance(client, monkeypatch):
    """测试 6：基金基本信息 + 区间表现两个工具连续调用"""
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [
            make_tool_call("call_1", "get_fund_detail",
                           json.dumps({"fund_code": "000001"})),
            make_tool_call("call_2", "get_fund_performance",
                           json.dumps({"fund_code": "000001", "period": 30})),
        ]),
        ("000001（华夏成长混合，混合型-偏股）最新净值与近 30 天区间表现如下："
         "历史数据显示区间内净值稳步上行、回撤有限；历史数据不预示未来表现。", []),
    ]))
    resp = chat(client, "分析一下基金 000001 近一个月的表现")
    assert resp.status_code == 200
    data = resp.json()
    assert data["rounds"] == 2
    assert [t["tool"] for t in data["tools_used"]] == [
        "get_fund_detail", "get_fund_performance",
    ]
    assert data["tools_used"][1]["arguments"] == {"fund_code": "000001", "period": 30}
    # 工具真实执行结果校验
    detail_payload = json.loads(model.calls[1][3]["content"])
    assert detail_payload["name"] == "华夏成长混合"
    assert detail_payload["latest_nav"]["date"] == date.today().isoformat()
    perf_payload = json.loads(model.calls[1][4]["content"])
    assert perf_payload["period"] == 30
    assert perf_payload["period_return"] == pytest.approx(
        float(Decimal("1.001") ** 30 * 100 - 100), abs=0.1
    )
    assert perf_payload["max_drawdown"] == 0.0


def test_agent_history_page_tool(client, monkeypatch):
    """测试 7：基金历史净值分页工具 get_fund_history"""
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [make_tool_call("call_1", "get_fund_history",
                               json.dumps({"fund_code": "000001", "page": 1, "page_size": 5}))]),
        ("已获取该基金最近 5 个交易日的净值数据（分页数据，最新在前）。", []),
    ]))
    resp = chat(client, "000001 最近 5 天净值是多少")
    assert resp.status_code == 200
    payload = json.loads(model.calls[1][3]["content"])
    assert payload["page"] == 1
    assert len(payload["items"]) == 5
    # 倒序：第一条为最新日期
    assert payload["items"][0]["date"] == date.today().isoformat()


# ---------------- 参数校验与自纠 ----------------


def test_agent_invalid_period_self_correct(client, monkeypatch):
    """测试 8（参数校验）：period=999 → 工具返回 error → 模型自纠后重调成功"""
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [make_tool_call("call_1", "get_fund_performance",
                               json.dumps({"fund_code": "000001", "period": 999}))]),
        (None, [make_tool_call("call_2", "get_fund_performance",
                               json.dumps({"fund_code": "000001", "period": 30}))]),
        ("已改用近 30 天数据：区间内净值稳步上行；历史数据不预示未来表现。", []),
    ]))
    resp = chat(client, "分析基金 000001 近 300 天表现")
    assert resp.status_code == 200
    data = resp.json()
    assert data["rounds"] == 3
    # 第一次调用的工具消息内容应包含 error，参数校验生效
    first_tool = json.loads(model.calls[1][3]["content"])
    assert "error" in first_tool
    assert "period" in first_tool["error"]
    # 第二次（自纠后）成功执行（索引 5 = 第二个 tool 结果）
    second_tool = json.loads(model.calls[2][5]["content"])
    assert "error" not in second_tool
    assert second_tool["period"] == 30


def test_agent_invalid_fund_code_self_correct(client, monkeypatch):
    """测试 9（参数校验）：基金代码格式错误 → error 回给模型"""
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [make_tool_call("call_1", "get_fund_detail",
                               json.dumps({"fund_code": "abc123"}))]),
        ("您提供的代码格式有误，基金代码应为 6 位数字，请确认后重试。", []),
    ]))
    resp = chat(client, "查一下 abc123")
    assert resp.status_code == 200
    tool_payload = json.loads(model.calls[1][3]["content"])
    assert "error" in tool_payload
    assert "6 位数字" in tool_payload["error"]


def test_agent_fund_not_found_as_error(client, monkeypatch):
    """测试 10：不存在的基金 → 工具返回 error（而非 500），模型如实转告"""
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [make_tool_call("call_1", "get_fund_detail",
                               json.dumps({"fund_code": "999999"}))]),
        ("未能查询到基金 999999 的数据，该基金可能不存在，请核对代码。", []),
    ]))
    resp = chat(client, "查一下基金 999999")
    assert resp.status_code == 200
    tool_payload = json.loads(model.calls[1][3]["content"])
    assert "error" in tool_payload


def test_agent_unknown_holding_id(client, monkeypatch):
    """测试 11：不存在的持仓 ID → error 回给模型"""
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [make_tool_call("call_1", "get_holding_history",
                               json.dumps({"holding_id": 999, "period": 30}))]),
        ("没有找到 ID 为 999 的持仓记录，请先在 get_holdings 中确认。", []),
    ]))
    resp = chat(client, "看看持仓 999 的历史")
    assert resp.status_code == 200
    tool_payload = json.loads(model.calls[1][3]["content"])
    assert "error" in tool_payload


def test_agent_missing_required_param(client, monkeypatch):
    """测试 12：缺少必填参数 holding_id → error 回给模型"""
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [make_tool_call("call_1", "get_holding_history", "{}")]),
        ("请先提供持仓 ID，或让我调用 get_holdings 查询您的持仓列表。", []),
    ]))
    resp = chat(client, "看看我的持仓历史")
    assert resp.status_code == 200
    tool_payload = json.loads(model.calls[1][3]["content"])
    assert "error" in tool_payload
    assert "holding_id" in tool_payload["error"]


# ---------------- 红线与终止条件 ----------------


def test_agent_redline_final_answer_rejected(client, monkeypatch):
    """测试 13（红线）：最终回复含"预计会上涨"→ 整次拒绝（502），不做片段丢弃"""
    create_holding(client)
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [make_tool_call("call_1", "get_holdings")]),
        ("您的持仓表现良好，该基金预计会上涨，值得保持关注。", []),
    ]))
    resp = chat(client, "帮我分析一下持仓，说说后面的走势")
    assert resp.status_code == 502
    assert "预测性" in resp.json()["detail"]


def test_agent_max_rounds_exceeded(client, monkeypatch):
    """测试 14：模型一直要求调工具直到最大轮数 → 502"""
    endless = [  # AGENT_MAX_ROUNDS 轮全部要求调工具
        (None, [make_tool_call(f"call_{i}", "get_holdings")])
        for i in range(agent_service.AGENT_MAX_ROUNDS)
    ]
    model = patch_chat(monkeypatch, ScriptedModel(endless + [("仍要调工具", [])]))
    resp = chat(client, "帮我分析持仓")
    assert resp.status_code == 502
    assert "轮" in resp.json()["detail"]
    # 循环恰好在最大轮数处终止，第 7 次模型调用不应发生
    assert len(model.calls) == agent_service.AGENT_MAX_ROUNDS


def test_agent_upstream_error_502(client, monkeypatch):
    """测试 15：模型上游调用失败 → 502"""

    async def boom(config, messages):
        raise AIUpstreamError("AI 服务调用失败：连接超时")

    monkeypatch.setattr(agent_service, "_chat", boom)
    resp = chat(client, "分析持仓")
    assert resp.status_code == 502


# ==================== Phase 16：边界与稳定性 ====================


def test_agent_tool_content_with_forbidden_phrase_not_redlined(client, monkeypatch):
    """P16-1（红线位置）：工具返回内容含禁词、最终回复干净 → 正常 200

    红线只检查最终回复（按现有设计，不擅自改变红线位置）；
    工具返回的是后端数据，不做预测性表述检查。
    """

    async def fake_execute(db, name, arguments):
        # 模拟工具数据内容里出现禁词（例如基金名称/备注），但这是数据不是 AI 表述
        return {"note": "数据备注：该基金宣传语中含值得买字样", "count": 0, "holdings": []}

    monkeypatch.setattr(agent_service, "execute_tool", fake_execute)
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [make_tool_call("call_1", "get_holdings")]),
        ("您当前没有持仓记录，请先在页面添加持仓后再进行分析。", []),
    ]))
    resp = chat(client, "看看我的持仓")
    assert resp.status_code == 200
    assert resp.json()["rounds"] == 2


def test_agent_empty_final_answer_502(client, monkeypatch):
    """P16-2：模型最终轮返回空内容 / None → 502 空内容"""

    for empty in ("", None):
        model = patch_chat(monkeypatch, ScriptedModel([(empty, [])]))
        resp = chat(client, "分析持仓")
        assert resp.status_code == 502
        assert "空内容" in resp.json()["detail"]


def test_agent_message_too_long_422(client):
    """P16-3：消息超过 2000 字 → Pydantic 422"""
    resp = chat(client, "分" * 2001)
    assert resp.status_code == 422


# ---------------- execute_tool 直测（不经模型，asyncio.run 驱动） ----------------


def test_execute_tool_unknown_tool(client, monkeypatch):
    """P16-4：模型调用了不存在的工具 → error 列出可用工具，不抛异常"""
    from app.agent.tools import execute_tool

    result = asyncio.run(execute_tool(None, "delete_holdings", "{}"))
    assert "error" in result
    assert "未知工具" in result["error"]
    assert "get_holdings" in result["error"]  # 提示了可用工具列表


def test_execute_tool_invalid_json_arguments(client, monkeypatch):
    """P16-5：模型给的参数不是合法 JSON → error"""
    from app.agent.tools import execute_tool

    result = asyncio.run(execute_tool(None, "get_fund_detail", "fund_code=000001"))
    assert result == {"error": "工具参数不是合法 JSON"}


def test_execute_tool_non_object_arguments(client, monkeypatch):
    """P16-6：参数是合法 JSON 但不是对象（数组/字符串）→ error"""
    from app.agent.tools import execute_tool

    result = asyncio.run(execute_tool(None, "get_fund_detail", '["000001"]'))
    assert "error" in result
    assert "JSON 对象" in result["error"]


def test_execute_tool_unexpected_exception_becomes_error(client, monkeypatch):
    """P16-7（稳定性）：执行器抛出未预期异常 → 转为 error dict，不打断循环"""
    from app.agent import tools as agent_tools
    from app.agent.tools import execute_tool

    async def broken(db, args):
        raise RuntimeError("boom")

    monkeypatch.setitem(agent_tools.TOOL_EXECUTORS, "get_holdings", broken)
    result = asyncio.run(execute_tool(None, "get_holdings", "{}"))
    assert result == {"error": "工具执行失败：RuntimeError"}


def test_execute_tool_fund_history_param_bounds(client, monkeypatch):
    """P16-8（参数边界）：page<1 / page_size 越界 → error，且不触发数据源请求"""
    from app.agent.tools import execute_tool

    for args, keyword in [
        ({"fund_code": "000001", "page": 0}, "page"),
        ({"fund_code": "000001", "page_size": 0}, "page_size"),
        ({"fund_code": "000001", "page_size": 51}, "page_size"),
    ]:
        result = asyncio.run(execute_tool(None, "get_fund_history", args))
        assert "error" in result, f"args={args} 应返回 error"
        assert keyword in result["error"]


def test_execute_tool_period_bounds(client, monkeypatch):
    """P16-9（参数边界）：period 非法（0 / 999 / 字符串 abc）→ error"""
    from app.agent.tools import execute_tool

    for value in (0, 999, "abc", None):
        result = asyncio.run(
            execute_tool(None, "get_fund_performance",
                         {"fund_code": "000001", "period": value})
        )
        assert "error" in result, f"period={value!r} 应返回 error"


def test_agent_data_source_down_becomes_error_dict(client, monkeypatch):
    """P16-10（异常处理）：数据源整体 500 → 工具返回"数据源暂不可用"error，
    模型如实转告用户，整次请求 200 而非 500"""

    def broken_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "mock down"})

    source = EastMoneyFundSource(transport=httpx.MockTransport(broken_handler))
    monkeypatch.setattr(fund_service, "data_source", source)
    fund_service.clear_cache()
    try:
        model = patch_chat(monkeypatch, ScriptedModel([
            (None, [make_tool_call("call_1", "get_fund_detail",
                                   json.dumps({"fund_code": "000001"}))]),
            ("基金数据源暂时不可用，暂时无法查询该基金的数据，请稍后重试。", []),
        ]))
        resp = chat(client, "查一下基金 000001")
        assert resp.status_code == 200
        tool_payload = json.loads(model.calls[1][3]["content"])
        assert "error" in tool_payload
        assert "数据源" in tool_payload["error"]
    finally:
        fund_service.clear_cache()


def test_agent_repeated_identical_tool_calls_terminated_by_cap(client, monkeypatch):
    """P16-11（防循环）：模型每轮重复调用完全相同的工具（同参）→
    由 AGENT_MAX_ROUNDS 兜底终止（502），不会无限循环"""
    same_calls = [
        (None, [make_tool_call("c", "get_holdings", "{}")])
        for _ in range(agent_service.AGENT_MAX_ROUNDS)
    ]
    model = patch_chat(monkeypatch, ScriptedModel(same_calls + [("还要调", [])]))
    resp = chat(client, "分析持仓")
    assert resp.status_code == 502
    assert len(model.calls) == agent_service.AGENT_MAX_ROUNDS
    # 每轮的上下文都在增长（工具结果确实回填了），循环上限是唯一终止机制
    assert len(model.calls[0]) < len(model.calls[-1])

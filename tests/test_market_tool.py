"""
Phase 20 大盘行情 Tool（get_market_index）测试

mock 两层：
1. 数据源：httpx.MockTransport 模拟东财 push2 ulist 行情接口（成功 / 500 / 字段缺失）
2. 模型调用：ScriptedModel（同 test_agent_session_stream.py 模式）

重点覆盖：
- Service 解析：三只指数字段（点位/涨跌点/涨跌幅/更新时间）与市场状态
- 数据源异常：整体 500 → DataSourceUnavailableError → 工具 error（不抛穿）
- 字段缺失：f2="-" / 缺 f124 → data_issues 如实说明，可用指数正常返回
- Tool 参数过滤：index="上证指数" 只返回一只；未知指数 → error 列出支持项
- Agent 集成：SSE 流式中工具状态 label="查询大盘行情" + 完成事件含该工具
- 既有 6 个 Tool 不受影响（全量回归由 pytest 总体保证）
"""
import asyncio
import httpx
import json
import pytest
import time
from types import SimpleNamespace
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database.database import Base, get_db
from app.main import app
from app.services import agent_service, agent_session, fund_service, market_service
from app.services.fund_data_source import EastMoneyFundSource, DataSourceUnavailableError

# ---------------- 内存数据库 + mock 数据源（基金侧） ----------------

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


def fund_ok_handler(request: httpx.Request) -> httpx.Response:
    """基金数据源兜底 mock（本轮测试不依赖真实净值细节）"""
    url = str(request.url)
    if "lsjz" in url:
        return httpx.Response(200, json={"TotalCount": 0, "Data": {"LSJZList": []}})
    if "FundSearchAPI" in url:
        code = request.url.params.get("key", "")
        return httpx.Response(200, json={"Datas": [{
            "CODE": code, "NAME": "测试基金", "CATEGORY": "700",
            "FundBaseInfo": {"FundType": "混合型-偏股"},
        }]})
    return httpx.Response(404, json={"error": "unknown url"})


# ---------------- 行情接口 mock（腾讯 qt.gtimg.cn 文本协议） ----------------

NOW_TS = time.strftime("%Y%m%d%H%M%S")


def tencent_row(symbol: str, name: str, code: str, current: str,
                change_point: str, change_percent: str, ts: str = NOW_TS) -> str:
    """构造一行腾讯行情文本（33 个字段，关键位按真实布局填值）"""
    parts = ["0"] * 33
    parts[1], parts[2], parts[3] = name, code, current
    parts[30], parts[31], parts[32] = ts, change_point, change_percent
    return f'v_{symbol}="' + "~".join(parts) + '";'


def market_text() -> str:
    return "\n".join([
        tencent_row("sh000001", "上证指数", "000001", "3245.67", "-3.9", "-0.12"),
        tencent_row("sz399001", "深证成指", "399001", "10500.10", "36.6", "0.35"),
        tencent_row("sz399006", "创业板指", "399006", "2100.25", "16.7", "0.8"),
    ])


def market_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    assert "qt.gtimg.cn" in url
    assert "sh000001" in url and "sz399006" in url
    return httpx.Response(200, content=market_text().encode("gbk"))


# ---------------- 模型脚本 mock ----------------

def make_tool_call(call_id, name, arguments="{}"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


class ScriptedModel:
    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    async def __call__(self, config, messages):
        self.calls.append(list(messages))
        if not self.turns:
            raise AssertionError("模型调用次数超出脚本轮数")
        content, tool_calls = self.turns.pop(0)
        return SimpleNamespace(content=content, tool_calls=tool_calls)


@pytest.fixture(autouse=True)
def ai_env_and_clean_sessions(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "test-key-for-pytest")
    monkeypatch.setenv("AI_BASE_URL", "https://ai.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")
    agent_session._sessions.clear()
    yield
    agent_session._sessions.clear()


@pytest.fixture()
def client(monkeypatch):
    Base.metadata.create_all(bind=test_engine)
    source = EastMoneyFundSource(transport=httpx.MockTransport(fund_ok_handler))
    monkeypatch.setattr(fund_service, "data_source", source)
    fund_service.clear_cache()
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    fund_service.clear_cache()
    Base.metadata.drop_all(bind=test_engine)


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if block.startswith("data:"):
            events.append(json.loads(block[5:].strip()))
    return events


# ---------------- Service 层 ----------------


def test_market_service_parses_indexes():
    """M-1：正常解析三只指数 + 市场状态"""
    async def run():
        return await market_service.get_market_indexes(
            transport=httpx.MockTransport(market_handler)
        )

    result = asyncio.run(run())
    assert result["data_issues"] == []
    assert [i["name"] for i in result["indexes"]] == ["上证指数", "深证成指", "创业板指"]
    sh = result["indexes"][0]
    assert sh["current_point"] == 3245.67
    assert sh["change_point"] == -3.9
    assert sh["change_percent"] == -0.12
    assert sh["update_time"]
    assert result["market_status"] in ("交易中（数据实时更新）", "今日已收盘（显示最新数据）")


def test_market_service_source_down(monkeypatch):
    """M-2：数据源 500 → DataSourceUnavailableError（Service 不伪造数据）"""

    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def run():
        await market_service.get_market_indexes(transport=httpx.MockTransport(broken))

    try:
        asyncio.run(run())
        assert False, "应抛出 DataSourceUnavailableError"
    except DataSourceUnavailableError:
        pass


def test_market_service_partial_fields():
    """M-3：单只指数数值缺失 / 缺更新时间 → data_issues 如实说明，其余正常"""
    lines = [
        tencent_row("sh000001", "上证指数", "000001", "3245.67", "-3.9", "-0.12"),
        # 深证成指：当前点位为空
        tencent_row("sz399001", "深证成指", "399001", "", "36.6", "0.35"),
        # 创业板指：缺更新时间
        tencent_row("sz399006", "创业板指", "399006", "2100.25", "16.7", "0.8", ts=""),
    ]
    text = "\n".join(lines)

    async def run():
        return await market_service.get_market_indexes(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=text.encode("gbk"))
            )
        )

    result = asyncio.run(run())
    names = [i["name"] for i in result["indexes"]]
    assert names == ["上证指数"]
    assert len(result["data_issues"]) == 2
    assert any("深证成指" in s for s in result["data_issues"])
    assert any("创业板指" in s for s in result["data_issues"])


def test_market_service_all_rows_missing_raises():
    """M-3b：三只指数全部缺失 → 汇总抛 DataSourceUnavailableError"""
    text = "\n".join([
        tencent_row("sh000001", "上证指数", "000001", "", "", ""),
        tencent_row("sz399001", "深证成指", "399001", "", "", ""),
        tencent_row("sz399006", "创业板指", "399006", "", "", ""),
    ])

    async def run():
        await market_service.get_market_indexes(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=text.encode("gbk"))
            )
        )

    try:
        asyncio.run(run())
        assert False, "应抛出 DataSourceUnavailableError"
    except DataSourceUnavailableError as e:
        assert "上证指数" in str(e)


# ---------------- Tool 层 ----------------


def _make_fake_indexes(monkeypatch):
    """把 market_service.get_market_indexes 换成注入 mock transport 的版本"""
    original = market_service.get_market_indexes

    async def fake_indexes(transport=None):
        return await original(transport=httpx.MockTransport(market_handler))

    monkeypatch.setattr(market_service, "get_market_indexes", fake_indexes)


def test_tool_market_index_filter_and_error(client, monkeypatch):
    """M-4：index 参数过滤 + 未知指数 error 列出支持项"""
    from app.agent.tools import execute_tool

    _make_fake_indexes(monkeypatch)

    # 精确过滤：只返回上证指数
    result = asyncio.run(execute_tool(None, "get_market_index", {"index": "上证指数"}))
    assert len(result["indexes"]) == 1
    assert result["indexes"][0]["name"] == "上证指数"
    assert "error" not in result

    # 未知指数 → error 列出支持项
    result = asyncio.run(execute_tool(None, "get_market_index", {"index": "恒生指数"}))
    assert "error" in result
    assert "创业板指" in result["error"]


def test_tool_market_index_unknown_executes_error(client, monkeypatch):
    """M-5（直测执行器）：未知指数 → error dict（工具不抛异常）"""
    from app.agent.tools import execute_tool

    _make_fake_indexes(monkeypatch)
    result = asyncio.run(execute_tool(None, "get_market_index", {"index": "纳斯达克"}))
    assert "error" in result
    assert "上证指数" in result["error"]


def test_tool_market_index_source_down_executes_error(client, monkeypatch):
    """M-6：数据源挂掉 → 工具返回 error（Agent 循环不被打断）"""
    from app.agent.tools import execute_tool

    async def broken(transport=None):
        raise DataSourceUnavailableError("大盘行情数据源请求失败")

    monkeypatch.setattr(market_service, "get_market_indexes", broken)
    result = asyncio.run(execute_tool(None, "get_market_index", {}))
    assert "error" in result
    assert "大盘行情" in result["error"]


# ---------------- Agent 集成（SSE + 会话链路） ----------------


def test_agent_market_tool_via_stream(client, monkeypatch):
    """M-7：Agent 调用大盘工具的完整 SSE 链路（状态 label + 完成事件含工具）"""
    _make_fake_indexes(monkeypatch)
    model = ScriptedModel([
        (None, [make_tool_call("call_1", "get_market_index", "{}")]),
        ("今天三大指数涨跌互现：上证指数 3245.67 点（-0.12%）。"
         "以上为行情数据描述，不构成投资建议。", []),
    ])
    monkeypatch.setattr(agent_service, "_chat", model)

    resp = client.post("/api/agent/chat/stream", json={"message": "今天大盘怎么样？"})
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    types = [e["type"] for e in events]

    # 工具阶段状态与完成事件
    tool_status = [e for e in events if e["type"] == "status" and e.get("stage") == "tool"]
    assert tool_status and tool_status[0]["label"] == "查询大盘行情"
    done = events[-1]
    assert done["type"] == "done"
    assert [t["tool"] for t in done["tools_used"]] == ["get_market_index"]

    # 工具结果真实回填模型上下文
    tool_msg = model.calls[1][3]
    assert tool_msg["role"] == "tool"
    payload = json.loads(tool_msg["content"])
    assert payload["market_status"]
    assert len(payload["indexes"]) == 3
    assert "error" not in payload

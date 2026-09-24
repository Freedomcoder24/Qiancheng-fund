"""
Phase 18 Agent 多轮会话 + SSE 流式输出 测试（内存 SQLite + mock 数据源 + mock 模型脚本）

沿用 test_agent_api.py 的三层 mock 模式，另加：
4. 会话存储：app.services.agent_session（进程内存，测试间强制清空）

重点覆盖（Phase 18 验收清单）：
- 多轮上下文：同一 session_id 第二轮的模型输入包含上一轮问答历史
- 指代理解机制：历史中的基金代码 / 回答文本可供模型解析"它"
- Session 隔离：不同 session_id 历史互不可见
- 历史截断：单会话最多保留 MAX_MESSAGES 条（防上下文无限增长）
- 流式正常结束：session → status → delta* → done 事件序，deltas 拼回完整回答
- 流式异常：上游失败 → error 事件、无 delta
- 红线（流式）：命中禁词 → error 事件、无任何 delta 流出、会话不记录该轮
- Tool Calling + 流式组合：status(tool) / tool_done 事件 + 完成事件含 tools_used
- 既有非流式 /api/agent/chat 行为保持兼容（test_agent_api.py 26 例继续通过）
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
from app.services import agent_service, agent_session, fund_service
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


# ---------------- 模型多轮脚本 mock（同 test_agent_api.py） ----------------

def make_tool_call(call_id: str, name: str, arguments="{}"):
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


class ScriptedModel:
    def __init__(self, turns: list[tuple[str | None, list]]):
        self.turns = list(turns)
        self.calls: list[list] = []

    async def __call__(self, config: dict, messages: list):
        self.calls.append(list(messages))  # 快照（run_agent 持续 append）
        assert config["api_key"] == "test-key-for-pytest"
        for m in messages:
            assert "test-key-for-pytest" not in json.dumps(m, ensure_ascii=False, default=str)
        if not self.turns:
            raise AssertionError("模型调用次数超出脚本轮数")
        content, tool_calls = self.turns.pop(0)
        return SimpleNamespace(content=content, tool_calls=tool_calls)


@pytest.fixture(autouse=True)
def ai_env_and_clean_sessions(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "test-key-for-pytest")
    monkeypatch.setenv("AI_BASE_URL", "https://ai.test/v1")
    monkeypatch.setenv("AI_MODEL", "test-model")
    agent_session._sessions.clear()  # 会话存储是进程级单例，测试间清空
    yield
    agent_session._sessions.clear()


def patch_chat(monkeypatch, model: ScriptedModel) -> ScriptedModel:
    monkeypatch.setattr(agent_service, "_chat", model)
    return model


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


def chat(client: TestClient, message: str, session_id: str = ""):
    return client.post(
        "/api/agent/chat", json={"message": message, "session_id": session_id}
    )


def create_holding(client: TestClient) -> int:
    resp = client.post(
        "/api/portfolio/holdings",
        json={"fund_code": "000001", "shares": 1000, "cost_price": 1.0},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def parse_sse(text: str) -> list[dict]:
    """把 SSE 响应体解析为事件列表"""
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if block.startswith("data:"):
            events.append(json.loads(block[5:].strip()))
    return events


# ---------------- 多轮上下文 ----------------


def test_session_multi_turn_context(client, monkeypatch):
    """P18-1：同一 session 第二轮，模型输入包含上一轮问答历史"""
    model = patch_chat(monkeypatch, ScriptedModel([
        ("基金 000001 是华夏成长混合，最新净值数据已列出。", []),
        ("它近 90 天区间收益为正，历史回撤较小。", []),
    ]))
    # 第一轮：新建会话
    resp1 = chat(client, "帮我看看 000001")
    assert resp1.status_code == 200
    sid = resp1.json()["session_id"]
    assert sid, "首次响应应返回新建的 session_id"

    # 第二轮：带 session_id（"它" 指代 000001）
    resp2 = chat(client, "那它最近90天怎么样？", session_id=sid)
    assert resp2.status_code == 200
    assert resp2.json()["session_id"] == sid

    # 第二轮模型的首轮输入应包含：system + 第一轮 user + 第一轮 assistant + 新 user
    second_first_call = model.calls[1]
    assert second_first_call[0]["role"] == "system"
    assert second_first_call[1] == {"role": "user", "content": "帮我看看 000001"}
    assert "000001" in second_first_call[2]["content"]  # 指代解析依据在历史中
    assert second_first_call[2]["role"] == "assistant"
    assert second_first_call[3] == {"role": "user", "content": "那它最近90天怎么样？"}


def test_session_isolation(client, monkeypatch):
    """P18-2：不同 session_id 的历史互相隔离"""
    model = patch_chat(monkeypatch, ScriptedModel([
        ("会话 A 的回答，提到基金 000001。", []),
        ("会话 B 的回答。", []),
        ("会话 B 的第二轮回答。", []),
    ]))
    resp_a = chat(client, "帮我看看 000001")
    sid_a = resp_a.json()["session_id"]
    resp_b = chat(client, "你好")
    assert resp_b.status_code == 200
    sid_b = resp_b.json()["session_id"]
    assert sid_a != sid_b

    # 会话 B 的第二轮请求不应看到会话 A 的历史
    chat(client, "继续", session_id=sid_b)
    messages_b = model.calls[2]
    joined = json.dumps(messages_b, ensure_ascii=False)
    assert "000001" not in joined
    assert "会话 A 的回答" not in joined


def test_session_invalid_id_creates_new(client, monkeypatch):
    """P18-3：无效 / 过期的 session_id → 静默新建会话并返回新 id"""
    model = patch_chat(monkeypatch, ScriptedModel([("直接回答。", [])]))
    resp = chat(client, "你好", session_id="not-exist-session")
    assert resp.status_code == 200
    new_sid = resp.json()["session_id"]
    assert new_sid and new_sid != "not-exist-session"
    # 新会话没有历史
    assert len(model.calls[0]) == 2  # system + user


# ---------------- 会话存储单元测试 ----------------


def test_session_store_truncation_and_expiry():
    """P18-4：历史条数截断到 MAX_MESSAGES；过期会话被清理"""
    sid, history = agent_session.get_or_create(None)
    assert history == []
    # 连续追加 5 轮（10 条）→ 只保留最近 8 条
    for i in range(5):
        agent_session.append_exchange(sid, f"问题{i}", f"回答{i}")
    _, history = agent_session.get_or_create(sid)
    assert len(history) == agent_session.MAX_MESSAGES
    assert history[0]["content"] == "问题1"  # 最早的两条被截掉
    assert history[-1]["content"] == "回答4"

    # 过期：last_access 拨回 TTL 之前 → 会话被清理，原 id 失效
    agent_session._sessions[sid]["last_access"] = (
        asyncio.get_event_loop().time() if False else 0  # 直接置 0（远在过去）
    )
    new_sid, history2 = agent_session.get_or_create(sid)
    assert new_sid != sid
    assert history2 == []


# ---------------- 流式输出 ----------------


def test_stream_success_event_sequence(client, monkeypatch):
    """P18-5（流式正常结束）：session → status → delta* → done，deltas 拼回完整回答"""
    answer = ("您当前没有持仓记录。请先在页面添加持仓，"
              "之后我就可以帮您分析市值、收益与风险分布了。")
    model = patch_chat(monkeypatch, ScriptedModel([(answer, [])]))
    resp = client.post("/api/agent/chat/stream", json={"message": "看看我的持仓"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert types[0] == "session"
    assert "status" in types
    assert types.count("delta") >= 2  # 分块流出
    assert types[-1] == "done"
    assert "error" not in types

    session_ev = events[0]
    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    done = events[-1]
    assert deltas == answer  # 增量拼回 = 完整回答
    assert done["answer"] == answer
    assert done["session_id"] == session_ev["session_id"]
    assert done["disclaimer"]
    assert model.calls[0][0]["role"] == "system"


def test_stream_tool_calling_combo(client, monkeypatch):
    """P18-6（Tool Calling + 流式组合）：工具阶段实时 status/tool_done + 完成含 tools_used"""
    create_holding(client)
    model = patch_chat(monkeypatch, ScriptedModel([
        (None, [make_tool_call("call_1", "get_holdings")]),
        ("您当前持有 1 只基金，整体表现平稳，历史数据不预示未来表现。", []),
    ]))
    resp = client.post("/api/agent/chat/stream", json={"message": "分析我的持仓"})
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    types = [e["type"] for e in events]

    # 工具阶段：status(tool, label) → tool_done（label 为中文标签）
    tool_status = [e for e in events if e["type"] == "status" and e.get("stage") == "tool"]
    assert tool_status and tool_status[0]["label"] == "查询持仓列表"
    tool_done = [e for e in events if e["type"] == "tool_done"]
    assert tool_done and tool_done[0]["ok"] is True

    # 完成事件：工具调用链完整记录
    done = events[-1]
    assert [t["tool"] for t in done["tools_used"]] == ["get_holdings"]
    assert done["rounds"] == 2

    # 工具结果真实回填到第二轮模型输入（role=tool）
    second_call = model.calls[1]
    assert second_call[3]["role"] == "tool"
    assert json.loads(second_call[3]["content"])["count"] == 1


def test_stream_upstream_error_event(client, monkeypatch):
    """P18-7（流式异常）：上游失败 → error 事件、无任何 delta"""

    async def boom(config, messages):
        raise AIUpstreamError("AI 服务调用失败：连接超时")

    monkeypatch.setattr(agent_service, "_chat", boom)
    resp = client.post("/api/agent/chat/stream", json={"message": "分析持仓"})
    assert resp.status_code == 200  # SSE 通道本身建立成功
    events = parse_sse(resp.text)
    assert [e["type"] for e in events] == ["session", "status", "error"]
    assert "连接超时" in events[-1]["detail"]


def test_stream_redline_rejection_no_delta_and_not_recorded(client, monkeypatch):
    """P18-8（红线，流式）：最终回复命中禁词 → error 事件，
    不流出任何 delta（不为流式效果降低红线安全性），会话也不记录该轮"""
    model = patch_chat(monkeypatch, ScriptedModel([
        ("您的持仓表现良好，该基金预计会上涨。", []),
    ]))
    resp = client.post("/api/agent/chat/stream", json={"message": "说说走势"})
    events = parse_sse(resp.text)
    types = [e["type"] for e in events]
    assert "delta" not in types  # 违规文本一个字都没有流出
    assert "done" not in types
    error = events[-1]
    assert error["type"] == "error"
    assert "预测性" in error["detail"]

    # 被拒绝的问答不进入会话历史
    sid = events[0]["session_id"]
    _, history = agent_session.get_or_create(sid)
    assert history == []


def test_stream_failed_turn_not_recorded_in_session(client, monkeypatch):
    """P18-9：上游异常的一轮不写入会话历史（只有成功轮才记录）"""
    sid, _ = agent_session.get_or_create(None)

    async def boom(config, messages):
        raise AIUpstreamError("AI 服务调用失败：上游异常")

    monkeypatch.setattr(agent_service, "_chat", boom)
    resp = client.post(
        "/api/agent/chat/stream", json={"message": "分析持仓", "session_id": sid}
    )
    events = parse_sse(resp.text)
    assert events[-1]["type"] == "error"

    _, history = agent_session.get_or_create(sid)
    assert history == []

    # 成功一轮后再看：历史里只有成功的那轮
    model = patch_chat(monkeypatch, ScriptedModel([("成功回答。", [])]))
    chat(client, "成功的问题", session_id=sid)
    _, history = agent_session.get_or_create(sid)
    assert [m["content"] for m in history] == ["成功的问题", "成功回答。"]


def test_stream_history_respects_truncation(client, monkeypatch):
    """P18-10：多轮后模型输入的历史不超过 MAX_MESSAGES 条，最早的轮次被截掉"""
    # 6 轮问答：第 6 轮时历史已有 10 条 → 只保留最近 8 条（第 2~5 轮）
    answers = [f"第{i}轮回答，涉及基金 00000{i}。" for i in range(1, 7)]
    turns = [(a, []) for a in answers]
    model = patch_chat(monkeypatch, ScriptedModel(turns))
    sid = ""
    for i in range(6):
        resp = chat(client, f"第{i + 1}轮问题", session_id=sid)
        assert resp.status_code == 200
        sid = resp.json()["session_id"]

    # 第 6 轮（最后一轮）模型输入：system + 历史（≤MAX_MESSAGES）+ 最新 user
    last_call = model.calls[5]
    history = last_call[1:-1]
    assert len(history) <= agent_session.MAX_MESSAGES
    joined = json.dumps(history, ensure_ascii=False)
    assert "第1轮问题" not in joined  # 最早的轮次已被截掉
    assert "第2轮问题" in joined      # 截断边界之后的轮次都在
    assert "第5轮问题" in joined

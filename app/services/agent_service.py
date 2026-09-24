"""Agent 服务（Phase 15，Phase 18 增加多轮会话与流式事件）

与现有一次性 AI 分析（run_analysis / run_goal_conclusion / run_candidate_analysis）
的区别：Agent 由模型自主决定调用哪些工具、调用几次，循环执行
"模型 → 工具 → 模型 → …" 直到给出最终回复，而不是后端固定组装数据投喂。

职责边界（沿用项目红线）：
1. 所有数字仍来自后端（工具返回 service 层 Decimal 计算结果），prompt 禁止编造；
2. 最终回复命中 FORBIDDEN_PREDICTIVE_PHRASES（预测性 / 推荐性红线）→
   整次拒绝，抛 AIResponseParseError，由路由层映射 502，不做片段丢弃；
3. 不产生任何交易指令；工具异常不打断循环，模型可自纠。

Phase 18：
- 多轮会话：agent_session 内存存储最近几轮问答，置于 system 之后参与推理，
  使模型能理解"它 / 这只基金"等指代；成功一轮才记入历史；
- 流式事件：run_agent_events 为唯一的循环实现（async generator 产出事件），
  非流式 run_agent 是它的包装；最终回复采用"完整生成 → 红线检测 →
  分块流出"，绝不为流式效果降低红线安全性（不先流出再撤销）；
  工具执行阶段的状态事件是真实实时推送。
"""
import asyncio
import json
import logging
from datetime import datetime

from openai import AsyncOpenAI

from app.agent.tools import AGENT_TOOLS, execute_tool
from app.services import agent_session, web_config_service
from app.services.ai_service import (
    AINotConfiguredError,
    AIResponseParseError,
    AIUpstreamError,
    FORBIDDEN_PREDICTIVE_PHRASES,
)
from app.services.ai_service import AI_TIMEOUT_SECONDS  # 单次模型调用超时（60 秒）
from app.services.goal_service import GLOBAL_DISCLAIMER

logger = logging.getLogger(__name__)

# Agent 循环最大轮数：每轮一次模型调用（可能伴随若干工具执行），
# 防止模型反复调工具拖长请求，达到上限仍无结论则终止本次对话
AGENT_MAX_ROUNDS = 6
# Agent 单次模型调用输出上限：tool_calls 需要更多输出空间，比普通分析（1000）放宽
AGENT_MAX_TOKENS = 1500

# 工具名 → 用户可读中文标签（流式状态事件与前端徽章共用同一语义）
AGENT_TOOL_LABELS = {
    "get_fund_detail": "查询基金基本信息",
    "get_fund_history": "查询历史净值",
    "get_fund_performance": "查询区间表现",
    "get_holdings": "查询持仓列表",
    "get_portfolio_summary": "查询账户汇总",
    "get_holding_history": "查询持仓历史走势",
    "get_market_index": "查询大盘行情",
}

# 最终回复分块流出的块大小（字符）与间隔（秒）：视觉上逐步展示，
# 全程在红线检测通过之后发生
STREAM_CHUNK_SIZE = 24
STREAM_CHUNK_INTERVAL = 0.02


def _contains_forbidden_phrase(text: str) -> str | None:
    """检查最终回复是否命中预测性 / 推荐性红线，命中返回命中的短语"""
    if not text:
        return None
    for phrase in FORBIDDEN_PREDICTIVE_PHRASES:
        if phrase in text:
            return phrase
    return None


def _chunk_text(text: str, size: int = STREAM_CHUNK_SIZE) -> list[str]:
    """把完整回复切成小块（流式逐块推送用）"""
    return [text[i:i + size] for i in range(0, len(text), size)]


AGENT_SYSTEM_PROMPT = """你是「钱程似锦」的个人基金分析助手（Agent）。你可以调用工具查询后端系统精确计算的数据，然后基于数据回答用户关于基金与持仓的分析问题。

你必须遵守以下规则：
1. 回答前先判断需要哪些数据，主动调用工具获取；不要凭空编造任何数字。
2. 所有数字（净值、收益、市值、涨跌幅）只能来自工具返回的数据，引用时必须与数据完全一致；工具没提供的数字和事实要明确说明"数据未提供"，禁止自行计算或猜测。
3. 你只做信息解读与风险提示，禁止给出"买入 / 卖出 / 加仓 / 减仓 / 止盈 / 赎回"等任何交易指令，也不要暗示用户应该采取某种交易行动。
4. 禁止根据历史数据预测未来收益，禁止出现"有望上涨 / 预计上涨 / 盈利概率 / 值得买"等预测性、推荐性表述；历史数据只用于描述和解释。
5. 持仓历史是按当前份额回放历史净值的模拟数据（未考虑历史申购、赎回及份额变化），提及它时必须说明"不代表真实历史账户资产"。
6. 工具返回 error 时，如实告知用户该数据暂时无法获取及原因，不要猜测或换数据编造。
7. 最终回复用中文、简明扼要，数据较多时可用简短分点；直接输出用户可读的分析文字，不要输出 JSON、代码块或工具调用过程。"""


async def _chat(config: dict, messages: list) -> object:
    """调用一次模型（带工具定义），返回响应中的 message 对象

    config 为生效配置（Web 优先，回退 .env）；异常统一转 AIUpstreamError，
    错误文本先抹去 API Key 再对外，与 ai_service._call_model 的安全约定一致。
    独立成函数便于测试时整体替换（mock 模型多轮脚本）。
    """
    client = AsyncOpenAI(
        api_key=config["api_key"], base_url=config["base_url"], timeout=AI_TIMEOUT_SECONDS
    )
    try:
        resp = await client.chat.completions.create(
            model=config["model"],
            messages=messages,
            tools=AGENT_TOOLS,
            max_tokens=AGENT_MAX_TOKENS,
            temperature=0.3,  # 低温度：数字分析要求稳定
        )
    except Exception as e:  # openai 各类异常统一转为业务异常
        logger.error("Agent 模型调用失败: %s", type(e).__name__)
        detail = str(e).replace(config["api_key"], "***") if config["api_key"] else str(e)
        raise AIUpstreamError(f"AI 服务调用失败：{detail}") from e

    if not resp.choices:
        raise AIUpstreamError("AI 服务返回了空内容")
    return resp.choices[0].message


async def run_agent_events(db, user_message: str, session_id: str | None = None):
    """Agent 主循环（唯一的循环实现）：产出事件流的 async generator

    事件类型（JSON 序列化后由路由层推给前端）：
    - session   : {"session_id"}                       会话就绪（首轮新建 id）
    - status    : {"stage": "thinking" | "composing"}  模型思考 / 生成回复中
    - status    : {"stage": "tool", "label"}           正在执行某个工具
    - tool_done : {"label", "ok"}                      工具执行完成
    - delta     : {"text"}                             最终回复增量（红线通过后）
    - done      : 完整结果（answer/model/rounds/tools_used/session_id/disclaimer）

    异常（AINotConfiguredError / AIUpstreamError / AIResponseParseError）向上抛，
    由调用方决定映射为 HTTP 状态码（非流式）或 error 事件（流式）。
    只有整轮成功（含红线通过）才把问答记入会话历史。
    """
    config = web_config_service.get_effective_ai_config(db)
    if not (config["api_key"] and config["base_url"] and config["model"]):
        raise AINotConfiguredError(
            "AI 功能未配置：请在 Dashboard 的「AI API 配置」面板填写并保存，"
            "或在 .env 中配置 AI_API_KEY、AI_BASE_URL、AI_MODEL 后重启服务"
        )

    session_id, history = agent_session.get_or_create(session_id)
    yield {"type": "session", "session_id": session_id}

    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": user_message},
    ]
    tools_used: list[dict] = []
    rounds_used = 0

    for _ in range(AGENT_MAX_ROUNDS):
        rounds_used += 1
        yield {"type": "status", "stage": "thinking"}
        msg = await _chat(config, messages)

        # 无工具调用 → 视为最终回复：完整生成 → 红线检测 → 才允许流出
        if not msg.tool_calls:
            answer = (msg.content or "").strip()
            if not answer:
                raise AIUpstreamError("AI 服务返回了空内容")
            hit = _contains_forbidden_phrase(answer)
            if hit:
                # 红线策略（用户确认）：整次拒绝，不做片段丢弃；
                # 此时尚未流出任何文本，安全性不打折
                raise AIResponseParseError(
                    f"AI 回复包含预测性/推荐性表述（{hit}），已拒绝本次回复"
                )

            agent_session.append_exchange(session_id, user_message, answer)
            logger.info(
                "Agent 完成: %d 轮, 工具调用 %d 次", rounds_used, len(tools_used)
            )

            yield {"type": "status", "stage": "composing"}
            for piece in _chunk_text(answer):
                yield {"type": "delta", "text": piece}
                await asyncio.sleep(STREAM_CHUNK_INTERVAL)

            yield {
                "type": "done",
                "answer": answer,
                "model": config["model"],
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "rounds": rounds_used,
                "tools_used": tools_used,
                "session_id": session_id,
                "disclaimer": GLOBAL_DISCLAIMER,
            }
            return

        # 有工具调用 → 先把 assistant 消息（含 tool_calls）追加进上下文
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        # 逐个执行工具，结果以 role="tool" 追加（error 也如实交给模型自纠）
        for tc in msg.tool_calls:
            name = tc.function.name
            label = AGENT_TOOL_LABELS.get(name, name)
            yield {"type": "status", "stage": "tool", "label": label}
            result = await execute_tool(db, name, tc.function.arguments)
            try:
                parsed = json.loads(tc.function.arguments) if tc.function.arguments else {}
            except json.JSONDecodeError:
                parsed = {}
            tools_used.append({
                "tool": name,
                "arguments": parsed if isinstance(parsed, dict) else {},
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
            yield {"type": "tool_done", "label": label, "ok": "error" not in result}

    raise AIUpstreamError(
        f"Agent 连续 {AGENT_MAX_ROUNDS} 轮未得出最终回复，"
        "请缩小问题范围后重试（例如只分析一只基金）"
    )


async def run_agent(db, user_message: str, session_id: str | None = None) -> dict:
    """非流式 Agent 对话：消费 run_agent_events 事件流，返回最终结果 dict

    与 Phase 15 行为完全兼容（Phase 17 前端与既有测试继续可用），
    仅新增 session_id 字段；异常映射保持：AINotConfiguredError → 503，
    AIUpstreamError / AIResponseParseError → 502。
    """
    result: dict | None = None
    async for event in run_agent_events(db, user_message, session_id):
        if event["type"] == "done":
            result = event
    assert result is not None  # 事件流必然以 done 或异常结束
    return {
        "model": result["model"],
        "generated_at": result["generated_at"],
        "answer": result["answer"],
        "tools_used": result["tools_used"],
        "rounds": result["rounds"],
        "session_id": result["session_id"],
        "disclaimer": result["disclaimer"],
    }

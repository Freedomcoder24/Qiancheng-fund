"""Agent 对话 API（Phase 15，Phase 18 增加多轮会话与 SSE 流式端点）

只负责：参数校验、调用 agent_service、把业务异常转换成 HTTP 状态码或
SSE error 事件。Agent 循环与工具执行细节都在 agent_service / agent.tools 中。
"""
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.models.agent import AgentChatIn, AgentChatResponse
from app.services import agent_service, ai_service

router = APIRouter(prefix="/api/agent", tags=["Agent"])


def _strip_message(body: AgentChatIn) -> str:
    """校验并取出有效消息文本（空白 → 400）"""
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="请输入分析需求")
    return message


@router.post("/chat", response_model=AgentChatResponse, summary="Agent 对话分析（Tool Calling）")
async def agent_chat(body: AgentChatIn, db: Session = Depends(get_db)):
    """用户提出基金分析需求，Agent 自主判断需要哪些数据、调用对应工具查询，
    基于后端精确计算的数据综合分析后返回最终回复（一次性 JSON，非流式）。

    携带上一轮响应中的 session_id 可延续多轮上下文（Phase 18）；
    session_id 无效 / 过期时后端自动新建会话并返回新 id。

    Agent 可用的工具：基金基本信息 / 历史净值 / 区间表现、用户持仓 /
    账户汇总 / 持仓历史模拟回放（全部为只读查询，无任何交易操作）。

    - 消息为空白 → 400
    - 未配置 AI → 503
    - AI 上游调用失败 / 达到最大轮数 / 最终回复命中预测性红线 → 502
    """
    try:
        return await agent_service.run_agent(db, _strip_message(body), body.session_id or None)
    except ai_service.AINotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except (ai_service.AIUpstreamError, ai_service.AIResponseParseError) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.post("/chat/stream", summary="Agent 对话分析（SSE 流式，Tool Calling 过程可见）")
async def agent_chat_stream(body: AgentChatIn, db: Session = Depends(get_db)):
    """流式版对话：以 text/event-stream 推送事件（data: {JSON}\\n\\n）。

    事件类型（data 字段中的 type）：
    - session   会话就绪（携带 session_id，客户端后续回传）
    - status    阶段状态（thinking / tool + label / composing）
    - tool_done 单个工具执行完成
    - delta     最终回复增量文本（红线检测通过后才开始推送）
    - done      完整结果（answer / tools_used / rounds / session_id / disclaimer）
    - error     失败（未配置 AI / 上游失败 / 超轮数 / 红线拒绝），流随即结束

    红线安全性说明：最终回复先完整生成并通过 FORBIDDEN_PREDICTIVE_PHRASES
    检测，之后才分块推送——不存在"先流出违规文本再撤销"的情况。
    """
    message = _strip_message(body)

    async def event_stream():
        async def sse(event: dict) -> str:
            return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        try:
            async for event in agent_service.run_agent_events(
                db, message, body.session_id or None
            ):
                yield await sse(event)
        except ai_service.AINotConfiguredError as e:
            yield await sse({"type": "error", "detail": str(e)})
        except (ai_service.AIUpstreamError, ai_service.AIResponseParseError) as e:
            yield await sse({"type": "error", "detail": str(e)})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用反向代理缓冲（如有）
        },
    )

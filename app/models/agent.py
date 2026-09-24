"""Agent 对话相关的数据模型（Pydantic，Phase 15）"""
from pydantic import BaseModel, Field


class AgentChatIn(BaseModel):
    """Agent 对话请求体：用户的基金分析需求（一句话描述即可）

    session_id 可选（Phase 18 多轮会话）：首次留空由后端新建，
    之后回传同一 id 即延续上下文；id 无效/过期时后端自动新建并返回新 id。
    """

    message: str = Field(min_length=1, max_length=2000, description="分析需求，例如：帮我分析一下我现在的持仓")
    session_id: str = Field(default="", max_length=64, description="会话 ID（留空新建会话）")


class AgentToolCall(BaseModel):
    """Agent 实际执行过的一次工具调用（用于展示调用链与排查问题）"""

    tool: str                                   # 工具名
    arguments: dict = Field(default_factory=dict)  # 模型给出的参数


class AgentChatResponse(BaseModel):
    """Agent 对话响应

    answer 为模型基于工具返回数据给出的最终中文分析；
    tools_used / rounds 让调用方了解 Agent 的推理过程规模；
    session_id 供客户端在后续请求中回传以延续上下文（Phase 18）。
    """

    model: str
    generated_at: str
    answer: str
    tools_used: list[AgentToolCall] = Field(default_factory=list)
    rounds: int                                 # 模型调用轮数
    session_id: str = ""                        # 会话 ID（Phase 18 多轮上下文）
    # 固定免责声明（后端附带，不依赖模型输出）
    disclaimer: str = "以上内容基于历史数据及模型分析，仅用于投资决策辅助，不代表未来收益，不构成投资建议。基金投资有风险，过往表现不代表未来表现。系统不会自动进行任何交易。"

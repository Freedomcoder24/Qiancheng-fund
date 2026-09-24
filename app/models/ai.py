"""AI 分析相关的数据模型（Pydantic，Phase 6）

响应中的分析内容来自大模型输出，模型字段与 AI 约定的 JSON 结构一一对应；
免责声明由后端固定附带，前端直接展示，保证"仅供参考，不构成投资建议"
永远与 AI 内容一起出现。
"""
from pydantic import BaseModel, Field


class AIAnalysisResponse(BaseModel):
    """AI 分析结果

    三块内容与 Phase 6 指令对应：今日持仓总结 / 收益来源 / 风险提示。
    """

    model: str                      # 使用的模型名称（来自 .env AI_MODEL）
    generated_at: str               # 生成时间（YYYY-MM-DD HH:MM:SS，本地时区）
    data_date: str | None           # 分析所依据的最新已确认净值日期
    today_summary: str              # 今日持仓总结
    profit_sources: str             # 收益来源分析
    risk_warnings: list[str] = Field(default_factory=list)  # 风险提示列表
    # 固定免责声明（后端附带，不依赖模型输出）
    disclaimer: str = "AI 分析仅供参考，不构成投资建议；系统不会进行任何自动交易操作。"


# ---------------- Web AI API 配置（Phase 9） ----------------

class WebAIConfigIn(BaseModel):
    """保存 Web AI 配置的请求体

    api_key 必填；base_url / model 可留空（留空的字段回落使用 .env 配置）。
    """

    api_key: str = Field(min_length=1)
    base_url: str = ""
    model: str = ""


class AIConfigTestIn(BaseModel):
    """测试连接的请求体：三项均可选

    留空的字段使用当前生效配置（Web 优先，回退 .env）；也可用于"保存前先试"。
    """

    api_key: str = ""
    base_url: str = ""
    model: str = ""


class AIConfigTestResult(BaseModel):
    """测试连接结果（错误信息已由服务层抹去 Key）"""

    ok: bool
    message: str
    model: str = ""


# ---------------- Phase 12：目标结论 / 候选池解读 ----------------

class GoalConclusionResponse(BaseModel):
    """AI 目标结论（conclusion 只有三种枚举，AI 只解释不决策）"""

    model: str
    generated_at: str
    data_date: str | None = None
    conclusion: str        # near_target / target_achieved / risk_attention
    conclusion_label: str  # 接近目标 / 已达到目标 / 风险需要关注
    reason: str            # 结论依据（只解释数据与风险因素）
    risks: list[str] = Field(default_factory=list)
    low_confidence: bool = False  # 枚举非法回退 risk_attention 时为 True
    disclaimer: str = ""


class CandidateHighlight(BaseModel):
    """候选池解读中的单只基金说明（只解释为什么符合筛选条件）"""

    code: str
    reason: str


class CandidateAnalysisResponse(BaseModel):
    """AI 候选池解读（定位：解释为什么符合当前量化筛选条件，不是推荐）"""

    model: str
    generated_at: str
    nav_date: str | None = None
    overview: str
    highlights: list[CandidateHighlight] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)
    market_background: str = ""  # 定性背景，非实时信息
    disclaimer: str = ""

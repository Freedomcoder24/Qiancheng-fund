"""投资目标相关的数据模型（Pydantic，Phase 12）

目标收益只是用户自设的参考线：系统围绕目标做量化差距分析与风险展示，
不把目标机械等同于止盈条件，也不会据此生成任何卖出 / 买入指令。
"""
from pydantic import BaseModel, Field


class GoalSaveIn(BaseModel):
    """保存目标收益率的请求体（0 < x ≤ 500，越界 FastAPI 自动 422）"""

    target_return_rate: float = Field(gt=0, le=500, description="目标收益率（%）")


class GoalResponse(BaseModel):
    """目标设置状态（未设置时 is_set=false，rate 为 null）"""

    is_set: bool
    target_return_rate: float | None = None
    updated_at: str | None = None


class GoalHoldingItem(BaseModel):
    """单只持仓的目标视角指标（全部来自后端 Decimal 计算）"""

    fund_code: str
    fund_name: str
    nav_date: str | None = None        # 最新净值日期
    profit_rate: float                 # 当前收益率（%）
    goal_gap_percent: float | None = None   # 距离目标（百分点，负数=已超出）
    achieved: bool | None = None       # 是否已达到目标（仅状态展示，不产生卖出结论）
    market_value: float = 0.0          # 当前市值（元）
    drawdown_90d: float | None = None      # 近 90 天净值最大回撤（%）
    drawdown_180d: float | None = None     # 近 180 天净值最大回撤（%）
    volatility_days_30d: int | None = None  # 近 30 天 |日涨跌| >= 2% 的天数


class GoalAccountInfo(BaseModel):
    """账户级目标进度（当前收益 → 距离目标）"""

    total_invested: float              # 总投入（元）
    total_profit: float                # 累计收益（元）
    current_profit_rate: float         # 当前收益率（%）
    target_return_rate: float          # 目标收益率（%）
    gap_percent: float                 # 距离目标（百分点，负数=已超出）
    required_profit: float             # 还差多少收益（元，已达标时为 0）
    achieved: bool                     # 是否已达到目标（仅状态展示）
    progress_percent: float | None = None   # 目标达成进度（0~100%，目标<=0 时为 None）
    drawdown_90d: float | None = None       # 组合近 90 天回撤（持仓市值加权）
    volatility_days_30d: int | None = None  # 组合近 30 天大波动天数（各持仓最大值）


class GoalAnalysisResponse(BaseModel):
    """目标收益分析（纯后端量化，未设置目标时 target_set=false 友好降级）"""

    target_set: bool
    target_return_rate: float | None = None
    updated_at: str | None = None
    holding_count: int = 0
    data_date: str | None = None
    # 5 状态分类（Phase 13，互斥单选，只描述状态不生成交易指令）：
    # not_set / risk_attention / target_achieved / near_target / far_from_target
    status: str | None = None
    status_label: str | None = None
    account: GoalAccountInfo | None = None
    holdings: list[GoalHoldingItem] = Field(default_factory=list)
    data_issues: list[str] = Field(default_factory=list)
    # 固定免责声明（后端附带，与页面全局横幅文案一致）
    disclaimer: str = ""

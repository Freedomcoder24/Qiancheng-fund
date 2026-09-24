"""持仓 / 账户相关的数据模型（Pydantic）"""
from decimal import Decimal

from pydantic import BaseModel, Field


class PortfolioSummary(BaseModel):
    """账户概览数据（首页顶部卡片）"""

    holding_count: int          # 持仓基金数量
    total_invested: float       # 总投入金额（元）
    total_market_value: float   # 总市值 / 总资产（元）
    total_profit: float         # 累计收益（元）
    total_profit_rate: float    # 累计收益率（%）
    # 今日收益估算：基于最新两个交易日已确认净值计算，
    # 不是实时估值（估值数据源已失效），因此不用 realtime / today 等误导性命名
    latest_nav_change: float    # 账户今日收益估算（元）


class HoldingCreate(BaseModel):
    """添加持仓的请求体

    基金名称不需要用户填写：后端根据 fund_code 从数据源自动获取。
    """

    fund_code: str = Field(pattern=r"^\d{6}$", description="6 位基金代码")
    # Pydantic 会把输入（JSON 数字/字符串）转换为 Decimal，避免 float 精度问题
    shares: Decimal = Field(gt=0, description="持有份额，必须大于 0")
    cost_price: Decimal = Field(gt=0, description="持仓成本净值，必须大于 0")


class HoldingUpdate(BaseModel):
    """修改持仓的请求体：只允许修改份额和成本（换基金请删除后重新添加）"""

    shares: Decimal | None = Field(default=None, gt=0, description="持有份额")
    cost_price: Decimal | None = Field(default=None, gt=0, description="持仓成本净值")


class HoldingResponse(BaseModel):
    """单条持仓的完整数据（数据库持仓 + 实时计算结果）"""

    id: int
    fund_code: str
    fund_name: str
    shares: float                     # 持有份额
    cost_price: float                 # 持仓成本净值
    latest_nav: float | None          # 最新已确认净值
    latest_nav_date: str | None       # 最新净值日期
    nav_daily_change: float | None    # 最新净值日增长率（%，来自数据源）
    market_value: float               # 当前市值 = 份额 × 最新净值
    invested_amount: float            # 投入金额 = 份额 × 成本
    profit: float                     # 累计收益 = 市值 - 投入
    profit_rate: float                # 收益率（%）
    latest_nav_change: float          # 今日收益估算（元）= 份额 × 净值变化


class SuccessResponse(BaseModel):
    """通用成功响应（删除持仓等操作）"""

    success: bool


# ============ Phase 4：持仓历史模拟市值 ============


class SimulatedHistoryPoint(BaseModel):
    """历史模拟市值的单个数据点"""

    date: str              # 净值日期（YYYY-MM-DD）
    nav: float             # 当日单位净值
    market_value: float    # 模拟市值 = 当前份额 × 当日净值
    simulated_profit: float  # 模拟收益 = 模拟市值 - 投入金额（投入金额固定不变）


class SimulatedHistoryResponse(BaseModel):
    """持仓历史模拟市值回放结果

    ⚠️ 口径说明（重要）：这是"模拟"数据 —— 按当前持仓份额回放历史基金净值，
    未考虑历史申购、赎回、分红及份额变化，因此不代表真实历史账户资产。
    """

    holding_id: int            # 持仓记录 id
    fund_code: str             # 基金代码
    fund_name: str             # 基金名称
    period: int                # 回放区间天数（7 / 30 / 90 / 180）
    shares: float              # 当前持有份额（回放期间固定不变）
    invested_amount: float     # 投入金额 = 当前份额 × 成本（固定不变）
    points: list[SimulatedHistoryPoint]  # 模拟市值点（按时间正序，供画图）

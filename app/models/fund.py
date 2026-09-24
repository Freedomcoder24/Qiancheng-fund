"""基金相关的数据模型（Pydantic）

所有字段都允许为 None（接口返回空字符串时不会导致程序崩溃），
但 code / name 这类核心标识字段除外。
"""
from enum import Enum

from pydantic import BaseModel


class FundBrief(BaseModel):
    """基金简要信息（用于列表展示）"""

    code: str                    # 基金代码，例如 000001
    name: str                    # 基金名称
    market_value: float = 0.0    # 当前市值（元）
    today_change: float = 0.0    # 单日涨跌幅（%）


class FundBasicInfo(BaseModel):
    """基金基本信息（搜索接口返回）"""

    code: str                    # 基金代码
    name: str                    # 基金名称
    fund_type: str | None = None  # 基金类型，例如"混合型-偏股"


class FundNavItem(BaseModel):
    """历史净值单条记录"""

    date: str                             # 净值日期（YYYY-MM-DD）
    unit_nav: float | None = None         # 单位净值
    accumulated_nav: float | None = None  # 累计净值
    daily_change: float | None = None     # 日增长率（%）


class FundHistoryPage(BaseModel):
    """历史净值分页结果"""

    fund_code: str             # 基金代码
    page: int                  # 当前页码
    page_size: int             # 每页条数
    total_count: int           # 总记录数
    items: list[FundNavItem]   # 净值列表


class FundValuation(BaseModel):
    """实时估值数据

    注意：天天基金 fundgz 估值接口 2026-09 实测已失效（返回 notfound 页面），
    当前没有可用的实时估值数据源，此模型保留给未来接入备用数据源使用。
    """

    code: str                                # 基金代码
    name: str | None = None                  # 基金名称
    nav_date: str | None = None              # 最新净值日期
    unit_nav: float | None = None            # 最新单位净值
    estimated_nav: float | None = None       # 估算净值
    estimated_change: float | None = None    # 估算涨跌幅（%）
    estimate_time: str | None = None         # 估值时间


class ValuationResponse(BaseModel):
    """估值接口的统一返回格式：明确告诉前端估值是否可用"""

    valuation_available: bool            # 估值是否可用
    valuation: FundValuation | None = None


class FundDetail(BaseModel):
    """基金综合信息：基本信息 + 最新已确认净值 + 估值（若可用）"""

    code: str                                  # 基金代码
    name: str                                  # 基金名称
    fund_type: str | None = None               # 基金类型
    latest_nav: FundNavItem | None = None      # 最新已确认净值（来自历史净值接口）
    valuation_available: bool = False          # 实时估值是否可用
    valuation: FundValuation | None = None     # 实时估值数据（若可用）


# ============ Phase 4：基金历史表现分析 ============

class AnalysisPeriod(int, Enum):
    """允许的历史分析区间（天）

    说明：查询参数以字符串传入（如 "30"），Pydantic v2 的 Literal[7, 30, ...]
    不会做字符串到整数的转换（合法值也会报 422），而 int 枚举可以自动转换，
    且枚举成员本身是 int 子类，可直接参与 timedelta(days=period) 等计算。
    非法值（如 45）由 FastAPI 自动返回 422。
    """

    WEEK = 7        # 近一周
    MONTH = 30      # 近一月
    QUARTER = 90    # 近三月
    HALF_YEAR = 180 # 近半年


class NavPoint(BaseModel):
    """画折线图用的净值点（日期 + 单位净值），比 FundNavItem 精简"""

    date: str     # 净值日期（YYYY-MM-DD）
    nav: float    # 单位净值


class FundPerformance(BaseModel):
    """基金区间历史表现：区间收益 + 最大回撤 + 净值走势点

    注意：这里统计的是"基金净值本身的历史表现"，
    与用户账户的真实收益无关（不要和持仓收益混淆）。
    """

    fund_code: str          # 基金代码
    fund_name: str          # 基金名称
    period: int             # 区间天数（7 / 30 / 90 / 180）
    start_date: str         # 区间起点日期（该区间内最早的净值日期）
    end_date: str           # 区间终点日期（最新净值日期）
    start_nav: float        # 起点单位净值
    end_nav: float          # 终点单位净值
    period_return: float    # 区间收益率（%）
    max_drawdown: float     # 区间最大回撤（%，负数或 0）
    nav_points: list[NavPoint]  # 净值走势点（按时间正序，供画图）


# ============ Phase 12：候选池排行（东方财富排行接口） ============

class FundRankingItem(BaseModel):
    """排行接口单只基金：区间收益概览（候选池量化筛选的数据来源）"""

    code: str                        # 基金代码
    name: str                        # 基金名称
    fund_type: str | None = None     # 基金类别（由排行请求类别决定：股票型/混合型）
    nav_date: str | None = None      # 净值日期（YYYY-MM-DD）
    unit_nav: float | None = None    # 单位净值
    daily_change: float | None = None  # 日涨幅（%）
    return_3m: float | None = None   # 近 3 月收益（%）
    return_6m: float | None = None   # 近 6 月收益（%）
    return_1y: float | None = None   # 近 1 年收益（%）


class FundRankingPage(BaseModel):
    """排行接口分页结果"""

    fund_type: str               # 排行类别（gp=股票型 / hh=混合型）
    page: int
    page_size: int
    total_count: int
    items: list[FundRankingItem]

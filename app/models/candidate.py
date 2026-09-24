"""候选池（基金关注）相关的数据模型（Pydantic，Phase 12/13）

候选池定位（重要）：「符合当前量化筛选条件的候选基金」。
- 筛选完全基于公开历史数据（排行 + 历史净值），不引入任何主观推荐；
- 页面与 AI 全程禁止「推荐购买 / 值得买 / 最优」等表述；
- 不预测未来收益，不构成投资建议。
"""
from pydantic import BaseModel, Field, model_validator


class CandidateFilterSaveIn(BaseModel):
    """用户自定义筛选条件（Phase 13，可调维度仅两个，其余固定列展示）"""

    include_stock: bool = True   # 勾选股票型
    include_mixed: bool = True   # 勾选混合型
    # 近 1 年收益下限（%），0 = 不过滤；允许负数 = 放宽（含亏损的也算符合）
    min_return_1y: float = Field(default=0, ge=-100, le=1000)

    @model_validator(mode="after")
    def _at_least_one_type(self):
        if not self.include_stock and not self.include_mixed:
            raise ValueError("至少勾选一个基金类型（股票型 / 混合型）")
        return self


class CandidateFilterResponse(BaseModel):
    """当前生效的筛选条件"""

    is_set: bool = False                 # False = 默认条件（从未保存过）
    include_stock: bool = True
    include_mixed: bool = True
    min_return_1y: float = 0
    updated_at: str | None = None


class CandidateItem(BaseModel):
    """候选池单只基金：排行概览 + 近 180 天历史校验指标（后端计算）"""

    code: str                            # 基金代码
    name: str                            # 基金名称
    fund_type: str | None = None         # 类别（股票型 / 混合型，来自排行请求）
    nav_date: str | None = None          # 排行数据的净值日期
    unit_nav: float | None = None        # 单位净值
    daily_change: float | None = None    # 日涨幅（%）
    return_3m: float | None = None       # 近 3 月收益（%，排行接口）
    return_6m: float | None = None       # 近 6 月收益（%，排行接口）
    return_1y: float | None = None       # 近 1 年收益（%，排行接口）
    return_180d: float | None = None     # 近 180 天区间收益（%，后端按净值计算）
    max_drawdown_180d: float | None = None   # 近 180 天最大回撤（%，负数或 0）
    volatility_days_30d: int | None = None   # 近 30 天 |日涨跌| >= 2% 的天数
    history_start: str | None = None     # 历史校验区间起点
    history_end: str | None = None       # 历史校验区间终点


class CandidatePoolResponse(BaseModel):
    """候选池（符合当前量化筛选条件的候选基金）"""

    generated_at: str                    # 生成时间
    screening_rule: str                  # 筛选规则说明（明确公开口径）
    ranking_total: int = 0               # 两个排行类别的基金总数
    count: int = 0                       # 实际进入候选池的数量
    nav_date: str | None = None          # 候选池数据对应的最新净值日期
    candidates: list[CandidateItem] = Field(default_factory=list)
    data_issues: list[str] = Field(default_factory=list)
    disclaimer: str = ""

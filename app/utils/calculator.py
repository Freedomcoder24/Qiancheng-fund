"""
收益计算工具

所有纯数学计算集中在这里，全部使用 Decimal 精确计算（资金计算不用 float），
由 Python 精确完成，不交给 AI 计算。路由层和业务层不做任何算术运算。

约定：
- 金额（市值、收益、投入）保留 2 位小数
- 净值、净值差保留 4 位小数
- 收益率（%）保留 2 位小数
"""
from decimal import Decimal, ROUND_HALF_UP

# 常用量化精度：金额 2 位、净值 4 位
MONEY = Decimal("0.01")
NAV = Decimal("0.0001")

ZERO = Decimal("0")


def calc_invested_amount(shares: Decimal, cost_price: Decimal) -> Decimal:
    """投入金额 = 持有份额 × 持仓成本"""
    return (shares * cost_price).quantize(MONEY, rounding=ROUND_HALF_UP)


def calc_market_value(shares: Decimal, latest_nav: Decimal) -> Decimal:
    """当前市值 = 持有份额 × 最新已确认净值"""
    return (shares * latest_nav).quantize(MONEY, rounding=ROUND_HALF_UP)


def calc_profit(market_value: Decimal, invested_amount: Decimal) -> Decimal:
    """累计收益 = 当前市值 - 投入金额"""
    return (market_value - invested_amount).quantize(MONEY, rounding=ROUND_HALF_UP)


def calc_profit_rate(profit: Decimal, invested_amount: Decimal) -> Decimal:
    """收益率（%）= 累计收益 ÷ 投入金额 × 100

    投入金额为 0 时返回 0，避免除零错误。
    """
    if invested_amount == ZERO:
        return ZERO
    rate = profit / invested_amount * Decimal("100")
    return rate.quantize(MONEY, rounding=ROUND_HALF_UP)


def calc_nav_change(latest_nav: Decimal, prev_nav: Decimal) -> Decimal:
    """最新两个交易日的单位净值变化（最新净值 - 上一交易日净值）"""
    return (latest_nav - prev_nav).quantize(NAV, rounding=ROUND_HALF_UP)


def calc_nav_change_profit(shares: Decimal, nav_change: Decimal) -> Decimal:
    """今日收益估算 = 持有份额 × 单位净值变化

    注意：这不是实时估值收益（估值数据源已失效），
    是基于最新两个交易日已确认净值的估算，字段名统一叫 latest_nav_change。
    """
    return (shares * nav_change).quantize(MONEY, rounding=ROUND_HALF_UP)


def to_decimal(value) -> Decimal:
    """把用户输入 / 接口返回的数字字符串安全转换为 Decimal

    用字符串构造 Decimal 避免引入 float 二进制误差。
    非法输入统一抛出 ValueError，由调用方转换为 400 参数错误。
    """
    if value is None:
        raise ValueError("数值不能为空")
    text = str(value).strip().replace(",", "")
    if not text:
        raise ValueError("数值不能为空")
    try:
        return Decimal(text)
    except ArithmeticError as e:  # 包含 decimal.InvalidOperation（非法格式）
        raise ValueError(f"非法数值: {text!r}") from e


# ---------------- Phase 4：历史表现计算 ----------------

def calculate_period_return(start_nav: Decimal, end_nav: Decimal) -> Decimal:
    """区间收益率（%）= (最新净值 - 起始净值) ÷ 起始净值 × 100

    起始净值为 0 时返回 0，避免除零错误。结果保留 2 位小数。
    """
    if start_nav == ZERO:
        return ZERO
    return ((end_nav - start_nav) / start_nav * Decimal("100")).quantize(
        MONEY, rounding=ROUND_HALF_UP
    )


def calculate_max_drawdown(navs: list[Decimal]) -> Decimal:
    """最大回撤（%），基于净值时间序列计算。

    口径：遍历净值序列（时间正序），记录截至当前点的历史最高净值，
    每个点相对历史最高点的回撤 = (当前净值 - 历史最高) ÷ 历史最高，
    取所有回撤中的最小值（最深的一次）。单调上涨时为 0。

    注意：必须基于时间序列顺序计算，不能简单用"最高净值 - 最低净值"。
    返回负百分比或 0，保留 2 位小数。
    """
    if not navs:
        return ZERO
    peak = navs[0]
    max_dd = ZERO
    for nav in navs[1:]:
        if nav > peak:
            peak = nav  # 更新历史最高
        elif peak > ZERO:
            dd = (nav - peak) / peak  # 回撤为负数或 0
            if dd < max_dd:
                max_dd = dd
    return (max_dd * Decimal("100")).quantize(MONEY, rounding=ROUND_HALF_UP)


def calculate_historical_market_value(shares: Decimal, nav: Decimal) -> Decimal:
    """历史模拟市值 = 当前持仓份额 × 当日历史净值

    ⚠️ 这是"模拟"口径：按当前份额回放历史净值，
    未考虑历史申购赎回和份额变化，不代表真实历史账户资产。
    """
    return (shares * nav).quantize(MONEY, rounding=ROUND_HALF_UP)


# ---------------- Phase 12：目标收益计算 ----------------

def calc_goal_gap(current_rate: Decimal, target_rate: Decimal) -> Decimal:
    """距离目标（百分点）= 目标收益率 - 当前收益率

    结果为正表示还差多少个百分点达标；为负表示已超过目标多少个百分点。
    """
    return (target_rate - current_rate).quantize(MONEY, rounding=ROUND_HALF_UP)


def calc_required_profit(gap_rate: Decimal, invested: Decimal) -> Decimal:
    """还差多少收益（元）= 距离百分点 ÷ 100 × 投入金额

    gap_rate <= 0（已达标或超出目标）时返回 0，不显示负的资金缺口。
    """
    if gap_rate <= ZERO:
        return ZERO.quantize(MONEY, rounding=ROUND_HALF_UP)
    return (gap_rate / Decimal("100") * invested).quantize(MONEY, rounding=ROUND_HALF_UP)

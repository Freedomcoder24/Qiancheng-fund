"""
收益计算工具测试（纯 Decimal 计算，无网络、无数据库）

验收基准（指令第二十节的数据一致性要求）：
    份额 3000 × 成本 1.2   = 投入 3600.00
    份额 3000 × 净值 1.332 = 市值 3996.00
    3996.00 - 3600.00      = 收益 396.00
    396 / 3600 × 100       = 收益率 11.00%
"""
from decimal import Decimal

from app.utils.calculator import (
    calc_invested_amount,
    calc_market_value,
    calc_nav_change,
    calc_nav_change_profit,
    calc_profit,
    calc_profit_rate,
    calculate_historical_market_value,
    calculate_max_drawdown,
    calculate_period_return,
    to_decimal,
)

D = Decimal


class TestInvestedAmount:
    """测试 1：投入金额 = 份额 × 成本"""

    def test_basic(self):
        assert calc_invested_amount(D("3000"), D("1.2000")) == D("3600.00")

    def test_fractional(self):
        assert calc_invested_amount(D("1234.5678"), D("2.3456")) == D("2895.80")


class TestMarketValue:
    """测试 2：当前市值 = 份额 × 最新净值"""

    def test_basic(self):
        assert calc_market_value(D("3000"), D("1.3320")) == D("3996.00")


class TestProfit:
    """测试 3：累计收益 = 市值 - 投入"""

    def test_positive(self):
        assert calc_profit(D("3996.00"), D("3600.00")) == D("396.00")

    def test_negative(self):
        assert calc_profit(D("3500.00"), D("3600.00")) == D("-100.00")


class TestProfitRate:
    """测试 4 / 5：收益率与零投入保护"""

    def test_basic(self):
        assert calc_profit_rate(D("396.00"), D("3600.00")) == D("11.00")

    def test_negative_rate(self):
        assert calc_profit_rate(D("-100.00"), D("3600.00")) == D("-2.78")

    def test_zero_invested_no_divide_error(self):
        """测试 5：投入金额为 0 时返回 0，不发生除零错误"""
        assert calc_profit_rate(D("100.00"), D("0")) == D("0")


class TestNavChange:
    """今日收益估算 = 份额 ×（最新净值 - 上一交易日净值）"""

    def test_nav_change(self):
        assert calc_nav_change(D("1.3320"), D("1.3330")) == D("-0.0010")

    def test_nav_change_profit(self):
        assert calc_nav_change_profit(D("3000"), D("-0.0010")) == D("-3.00")


class TestDecimalPrecision:
    """测试 6：Decimal 精度与四舍五入（ROUND_HALF_UP，银行家舍入的反例）"""

    def test_half_up(self):
        # 0.125 保留 2 位：四舍五入应为 0.13（float 会给出 0.12）
        assert calc_profit_rate(D("0.125"), D("100")) == D("0.13")

    def test_no_float_binary_error(self):
        # 经典浮点误差案例：0.1 + 0.2 用 Decimal 计算应为精确值
        assert calc_profit(D("0.30"), D("0.10")) == D("0.20")


class TestToDecimal:
    """to_decimal：用户输入安全转换"""

    def test_from_str_and_float(self):
        assert to_decimal("3000.00") == D("3000.00")
        assert to_decimal(1.2) == D("1.2")
        assert to_decimal(" 1.3320 ") == D("1.3320")

    def test_invalid(self):
        import pytest

        with pytest.raises(ValueError):
            to_decimal("abc")
        with pytest.raises(ValueError):
            to_decimal("")
        with pytest.raises(ValueError):
            to_decimal(None)


# ============ Phase 4：区间收益 / 最大回撤 / 历史模拟市值 ============

class TestPeriodReturn:
    """区间收益率 =（终点净值 - 起点净值）÷ 起点净值 × 100"""

    def test_up(self):
        # 1.0 → 1.1，涨 10%
        assert calculate_period_return(D("1.0000"), D("1.1000")) == D("10.00")

    def test_down(self):
        # 2.0 → 1.5，跌 25%
        assert calculate_period_return(D("2.0000"), D("1.5000")) == D("-25.00")

    def test_zero_start_no_divide_error(self):
        """起始净值为 0 时返回 0，不发生除零错误"""
        assert calculate_period_return(D("0"), D("1.0000")) == D("0")


class TestMaxDrawdown:
    """最大回撤：基于时间正序净值序列，取相对历史最高点的最深跌幅"""

    def test_normal(self):
        # 峰值 2.0 跌到谷底 1.0 回撤 -50%，之后回升到 1.5 也不改变最大回撤
        assert calculate_max_drawdown([D("2.0"), D("1.0"), D("1.5")]) == D("-50.00")

    def test_monotonic_up_is_zero(self):
        """单调上涨没有回撤，返回 0"""
        assert calculate_max_drawdown([D("1.0"), D("1.1"), D("1.2")]) == D("0")

    def test_monotonic_down(self):
        # 一路下跌：2.0 → 1.0，最深回撤 -50%
        assert calculate_max_drawdown([D("2.0"), D("1.5"), D("1.0")]) == D("-50.00")


class TestHistoricalMarketValue:
    """历史模拟市值 = 当前持仓份额 × 当日历史净值（模拟口径）"""

    def test_basic(self):
        assert calculate_historical_market_value(D("3000"), D("1.3320")) == D("3996.00")


class TestPhase4DecimalPrecision:
    """Phase 4 计算的 Decimal 舍入精度（ROUND_HALF_UP）"""

    def test_rounding(self):
        # 1/3 的回撤 = -33.333...%，量化后应为 -33.33
        assert calculate_max_drawdown([D("3.0"), D("2.0")]) == D("-33.33")

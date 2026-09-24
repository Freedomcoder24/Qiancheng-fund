"""智能监控服务（Phase 7）：基于固定规则检查持仓风险

职责边界（重要）：
1. 所有数值（涨跌幅、占比、回撤、波动天数）全部由后端用 Decimal 精确计算，
   基于已有数据源与 calculator，AI 不参与任何数值计算；
2. 规则阈值作为常量明确写在代码顶部，改规则 = 改常量；
3. 只生成提醒，不做任何自动交易，不给出买卖建议。

数据口径：
- 单日涨跌幅：数据源提供的最新日增长率（HoldingResponse.nav_daily_change）
- 持仓集中度：单只基金当前市值 ÷ 账户总市值
- 历史最大回撤：单只基金近 90 天净值序列的峰值回撤（calculate_max_drawdown）
- 近期异常波动：近 30 天内 |日涨跌幅| >= 2% 的交易日数量
"""
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.fund import AnalysisPeriod
from app.models.monitor import MonitorAlert, MonitorResponse
from app.services import fund_service, portfolio_service
from app.services.fund_data_source import (
    DataSourceUnavailableError,
    FundNotFoundError,
)
from app.utils import calculator

logger = logging.getLogger(__name__)

# ============================================================
# 监控规则（明确写入代码；所有阈值为百分数的数值，例如 3 表示 3%）
# ============================================================

# ---- 规则 1：单日涨跌幅异常（最新交易日日涨跌幅的绝对值） ----
DAILY_CHANGE_INFO = Decimal("2")     # |x| >= 2% → 提示
DAILY_CHANGE_WARNING = Decimal("3")  # |x| >= 3% → 注意
DAILY_CHANGE_DANGER = Decimal("5")   # |x| >= 5% → 高风险

# ---- 规则 2：持仓集中度过高（单只基金市值 ÷ 账户总市值） ----
CONCENTRATION_INFO = Decimal("40")     # >= 40% → 提示
CONCENTRATION_WARNING = Decimal("60")  # >= 60% → 注意
CONCENTRATION_DANGER = Decimal("80")   # >= 80% → 高风险

# ---- 规则 3：历史最大回撤（近 90 天基金净值峰值回撤，取绝对值比较） ----
DRAWDOWN_INFO = Decimal("5")     # >= 5% → 提示
DRAWDOWN_WARNING = Decimal("10") # >= 10% → 注意
DRAWDOWN_DANGER = Decimal("20")  # >= 20% → 高风险
DRAWDOWN_PERIOD = AnalysisPeriod.QUARTER  # 90 天

# ---- 规则 4：近期异常波动（近 30 天内 |日涨跌幅| >= 阈值 的交易日数量） ----
VOLATILITY_CHANGE_THRESHOLD = Decimal("2")  # 单日波动计入阈值（%）
VOLATILITY_DAYS_INFO = 2      # >= 2 天 → 提示
VOLATILITY_DAYS_WARNING = 3   # >= 3 天 → 注意
VOLATILITY_DAYS_DANGER = 5    # >= 5 天 → 高风险
VOLATILITY_WINDOW_DAYS = 30   # 统计窗口（自然日，与区间净值口径一致）
# 窗口内有效日涨跌幅样本少于该值时视为数据不足，不做波动判断（不伪造数据）
MIN_VOLATILITY_SAMPLES = 5

# 回撤计算要求的最少净值点数（少于该值无法形成有意义的峰值回撤）
MIN_NAV_POINTS = 3

# 等级定义与排序权重（数值越小越靠前）
LEVEL_LABELS = {"info": "提示", "warning": "注意", "danger": "高风险"}
LEVEL_RANK = {"danger": 0, "warning": 1, "info": 2}

TYPE_LABELS = {
    "daily_change": "单日涨跌幅异常",
    "concentration": "持仓集中度过高",
    "max_drawdown": "历史最大回撤",
    "volatility": "近期异常波动",
}

PCT = calculator.MONEY  # 百分比统一保留 2 位小数


def _make_alert(
    alert_type: str, level: str, fund_code: str | None,
    fund_name: str | None, reason: str, detail: str,
) -> MonitorAlert:
    """按类型与等级构造提醒（等级中文标签统一在这里映射）"""
    return MonitorAlert(
        type=alert_type,
        type_label=TYPE_LABELS[alert_type],
        level=level,
        level_label=LEVEL_LABELS[level],
        fund_code=fund_code,
        fund_name=fund_name,
        reason=reason,
        detail=detail,
    )


def _tier(value: Decimal, info: Decimal, warning: Decimal, danger: Decimal) -> str | None:
    """分级：>= danger → 高风险；>= warning → 注意；>= info → 提示；否则 None

    传入值应为"绝对值 / 占比"这类越大越危险的量。
    """
    if value >= danger:
        return "danger"
    if value >= warning:
        return "warning"
    if value >= info:
        return "info"
    return None


# ---------------- 四类检查 ----------------

def _check_daily_change(holding) -> tuple[list[MonitorAlert], list[str]]:
    """规则 1：单日涨跌幅异常（数据源最新日增长率）"""
    alerts: list[MonitorAlert] = []
    if holding.nav_daily_change is None:
        return alerts, [
            f"{holding.fund_name}（{holding.fund_code}）未获取到最新日涨跌幅，"
            "单日涨跌幅检查跳过"
        ]

    change = Decimal(str(holding.nav_daily_change))
    level = _tier(abs(change), DAILY_CHANGE_INFO, DAILY_CHANGE_WARNING, DAILY_CHANGE_DANGER)
    if level is None:
        return alerts, []

    direction = "上涨" if change > 0 else "下跌"
    threshold = {
        "danger": DAILY_CHANGE_DANGER,
        "warning": DAILY_CHANGE_WARNING,
        "info": DAILY_CHANGE_INFO,
    }[level]
    alerts.append(_make_alert(
        "daily_change", level, holding.fund_code, holding.fund_name,
        reason=f"最新交易日单日{direction} {abs(change):.2f}%，"
               f"达到 {LEVEL_LABELS[level]} 级阈值（|涨跌幅| >= {threshold}%）",
        detail=f"最新日涨跌幅 {change:+.2f}%"
               + (f"（净值日期 {holding.latest_nav_date}）" if holding.latest_nav_date else ""),
    ))
    return alerts, []


def _check_concentration(holdings) -> tuple[list[MonitorAlert], list[str]]:
    """规则 2：持仓集中度过高（账户级，逐只计算市值占比）"""
    alerts: list[MonitorAlert] = []
    total = sum((Decimal(str(h.market_value)) for h in holdings), Decimal("0"))
    if total <= 0:
        return alerts, ["所有持仓的最新净值缺失，无法计算仓位集中度"]

    issues: list[str] = []
    for h in holdings:
        mv = Decimal(str(h.market_value))
        ratio = (mv / total * Decimal("100")).quantize(PCT, rounding="ROUND_HALF_UP")
        level = _tier(ratio, CONCENTRATION_INFO, CONCENTRATION_WARNING, CONCENTRATION_DANGER)
        if level is None:
            continue
        threshold = {
            "danger": CONCENTRATION_DANGER,
            "warning": CONCENTRATION_WARNING,
            "info": CONCENTRATION_INFO,
        }[level]
        alerts.append(_make_alert(
            "concentration", level, h.fund_code, h.fund_name,
            reason=f"该基金市值占账户总市值 {ratio:.2f}%，"
                   f"达到 {LEVEL_LABELS[level]} 级阈值（占比 >= {threshold}%）",
            detail=f"市值 {mv:,.2f} 元 / 账户总市值 {total:,.2f} 元，占比 {ratio:.2f}%",
        ))
    return alerts, issues


def _check_drawdown_and_volatility(
    holding, navs
) -> tuple[list[MonitorAlert], list[str]]:
    """规则 3 + 规则 4：最大回撤（近 90 天）与近期异常波动（近 30 天）

    navs 为 get_navs_for_period(DRAWDOWN_PERIOD) 的结果（日期倒序）。
    """
    alerts: list[MonitorAlert] = []
    issues: list[str] = []

    # ---- 规则 3：历史最大回撤（基于单位净值序列，时间正序） ----
    ordered = [item for item in reversed(navs) if item.unit_nav is not None]
    if len(ordered) < MIN_NAV_POINTS:
        issues.append(
            f"{holding.fund_name}（{holding.fund_code}）近 90 天有效净值点不足 "
            f"{MIN_NAV_POINTS} 个（实际 {len(ordered)} 个），最大回撤检查跳过"
        )
    else:
        nav_seq = [calculator.to_decimal(item.unit_nav) for item in ordered]
        drawdown = calculator.calculate_max_drawdown(nav_seq)
        level = _tier(abs(drawdown), DRAWDOWN_INFO, DRAWDOWN_WARNING, DRAWDOWN_DANGER)
        if level is not None:
            threshold = {
                "danger": DRAWDOWN_DANGER,
                "warning": DRAWDOWN_WARNING,
                "info": DRAWDOWN_INFO,
            }[level]
            alerts.append(_make_alert(
                "max_drawdown", level, holding.fund_code, holding.fund_name,
                reason=f"近 90 天基金净值最大回撤 {abs(drawdown):.2f}%，"
                       f"达到 {LEVEL_LABELS[level]} 级阈值（回撤 >= {threshold}%）",
                detail=f"区间 {ordered[0].date} ~ {ordered[-1].date}，"
                       f"最大回撤 {drawdown:.2f}%（基于基金净值峰值计算）",
            ))

    # ---- 规则 4：近期异常波动（近 30 天 |日涨跌幅| >= 阈值 的交易日数量） ----
    window_start = date.today() - timedelta(days=VOLATILITY_WINDOW_DAYS)
    big_days = 0       # |日涨跌幅| >= 阈值 的天数
    valid_days = 0     # 窗口内有有效日涨跌幅的天数
    max_abs_change = Decimal("0")
    for item in navs:  # 倒序不影响计数
        try:
            item_date = datetime.strptime(item.date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if item_date < window_start:
            continue
        if item.daily_change is None:
            continue
        valid_days += 1
        change = Decimal(str(item.daily_change))
        if abs(change) > max_abs_change:
            max_abs_change = abs(change)
        if abs(change) >= VOLATILITY_CHANGE_THRESHOLD:
            big_days += 1

    if valid_days < MIN_VOLATILITY_SAMPLES:
        issues.append(
            f"{holding.fund_name}（{holding.fund_code}）近 30 天有效日涨跌幅样本不足 "
            f"{MIN_VOLATILITY_SAMPLES} 个（实际 {valid_days} 个），异常波动检查跳过"
        )
    elif big_days >= VOLATILITY_DAYS_INFO:
        level = (
            "danger" if big_days >= VOLATILITY_DAYS_DANGER
            else "warning" if big_days >= VOLATILITY_DAYS_WARNING
            else "info"
        )
        threshold_days = {
            "danger": VOLATILITY_DAYS_DANGER,
            "warning": VOLATILITY_DAYS_WARNING,
            "info": VOLATILITY_DAYS_INFO,
        }[level]
        alerts.append(_make_alert(
            "volatility", level, holding.fund_code, holding.fund_name,
            reason=f"近 30 天内有 {big_days} 个交易日单日波动超过 "
                   f"{VOLATILITY_CHANGE_THRESHOLD}%，达到 {LEVEL_LABELS[level]} 级阈值"
                   f"（>= {threshold_days} 天）",
            detail=f"近 30 天共 {valid_days} 个有效交易日，"
                   f"最大单日波动 {max_abs_change:.2f}%",
        ))

    return alerts, issues


# ---------------- 主入口 ----------------

async def run_monitoring(db: Session) -> MonitorResponse:
    """执行全部监控规则，返回提醒列表（按等级从高到低排序）

    异常约定：
    - 账户持仓整体无法获取（数据源不可用）→ DataSourceUnavailableError 向上抛 → 503
    - 单只基金区间净值获取失败 → 记入 data_issues，不影响其他检查（不伪造数据）
    """
    # 持仓 + 最新净值（内含实时计算），数据源整体不可用时直接向上抛
    holdings = await portfolio_service.list_holdings(db)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not holdings:
        return MonitorResponse(
            generated_at=generated_at,
            data_date=None,
            checked_count=0,
            alert_count=0,
            summary="暂无持仓，添加持仓后即可启用智能监控",
        )

    alerts: list[MonitorAlert] = []
    issues: list[str] = []

    # 规则 2：集中度（账户级）
    concentration_alerts, concentration_issues = _check_concentration(holdings)
    alerts.extend(concentration_alerts)
    issues.extend(concentration_issues)

    # 规则 1 / 3 / 4：逐只基金检查
    for holding in holdings:
        change_alerts, change_issues = _check_daily_change(holding)
        alerts.extend(change_alerts)
        issues.extend(change_issues)

        # 区间净值（带缓存）：单只失败只记录问题，不中断整体检查
        try:
            navs = await fund_service.get_navs_for_period(
                holding.fund_code, DRAWDOWN_PERIOD
            )
        except (DataSourceUnavailableError, FundNotFoundError) as e:
            issues.append(
                f"{holding.fund_name}（{holding.fund_code}）区间净值获取失败，"
                f"最大回撤与异常波动检查跳过：{e}"
            )
            continue

        dd_alerts, dd_issues = _check_drawdown_and_volatility(holding, navs)
        alerts.extend(dd_alerts)
        issues.extend(dd_issues)

    # 排序：高风险 → 注意 → 提示；同级按基金代码稳定排序
    alerts.sort(key=lambda a: (LEVEL_RANK[a.level], a.fund_code or ""))

    data_date = max(
        (h.latest_nav_date for h in holdings if h.latest_nav_date), default=None
    )
    logger.info(
        "智能监控完成: 持仓 %d 只, 提醒 %d 条, 数据问题 %d 项",
        len(holdings), len(alerts), len(issues),
    )

    return MonitorResponse(
        generated_at=generated_at,
        data_date=data_date,
        checked_count=len(holdings),
        alert_count=len(alerts),
        summary="当前未发现明显异常" if not alerts else "",
        alerts=alerts,
        data_issues=issues,
    )

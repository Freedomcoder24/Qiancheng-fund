"""投资目标服务（Phase 12）：目标设置持久化 + 目标进度量化分析

职责边界（重要）：
1. 所有数值（收益率、差距、回撤、波动天数）全部由后端用 Decimal 精确计算，
   AI 只在后续步骤解读这些数据，绝不参与数值计算；
2. 目标收益只是用户自设的参考线：达到目标只显示「已达到目标」状态，
   不机械等同于止盈条件，不生成任何卖出 / 买入指令；
3. 数据不足或数据源异常记入 data_issues，不伪造数据。
"""
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.database.models import InvestmentGoal
from app.models.fund import AnalysisPeriod
from app.services import fund_service, portfolio_service
from app.services.fund_data_source import (
    DataSourceUnavailableError,
    FundNotFoundError,
)
from app.utils import calculator

logger = logging.getLogger(__name__)

# 全局免责声明（Phase 12 统一文案：页面横幅 / 面板 / AI 响应共用）
GLOBAL_DISCLAIMER = (
    "以上内容基于历史数据及模型分析，仅用于投资决策辅助，不代表未来收益，"
    "不构成投资建议。基金投资有风险，过往表现不代表未来表现。"
    "系统不会自动进行任何交易。"
)

# 波动 / 回撤统计口径（与 Phase 7 智能监控保持一致，不另立规则）
VOLATILITY_CHANGE_THRESHOLD = Decimal("2")   # 单日 |涨跌幅| >= 2% 计入大波动
MIN_VOLATILITY_SAMPLES = 5                   # 窗口内有效样本少于该值视为数据不足
MIN_NAV_POINTS = 3                           # 回撤要求的最少净值点数
VOLATILITY_WINDOW_DAYS = 30                  # 波动统计窗口（自然日）

PCT = calculator.MONEY  # 百分比统一保留 2 位小数


class GoalNotSetError(Exception):
    """用户尚未设置目标收益率（AI 目标结论需要目标作为前提）"""


# ---------------- 目标设置 CRUD（单行 upsert） ----------------

def get_goal(db: Session) -> InvestmentGoal | None:
    """读取目标行（单行，取 id 最小的一条）"""
    return db.query(InvestmentGoal).order_by(InvestmentGoal.id.asc()).first()


def save_goal(db: Session, rate: Decimal) -> InvestmentGoal:
    """保存 / 覆盖目标收益率（upsert 单行，调用前需完成 0 < rate ≤ 500 校验）"""
    row = get_goal(db)
    if row is None:
        row = InvestmentGoal(id=1)
        db.add(row)
    row.target_return_rate = rate.quantize(Decimal("0.0001"), rounding="ROUND_HALF_UP")
    row.updated_at = datetime.now()
    db.commit()
    db.refresh(row)
    logger.info("投资目标已保存: %s%%", row.target_return_rate)
    return row


# ---------------- 统计口径辅助（复用监控逻辑的数据口径） ----------------

def _volatility_days(navs, window_days: int) -> int | None:
    """近 window_days 天内 |日涨跌幅| >= 阈值 的天数

    有效样本不足时返回 None（数据不足，不伪造）。
    navs 为日期倒序的 FundNavItem 列表（与 fund_service 返回一致）。
    """
    window_start = date.today() - timedelta(days=window_days)
    big_days = 0
    valid_days = 0
    for item in navs:
        try:
            item_date = datetime.strptime(item.date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if item_date < window_start:
            continue
        if item.daily_change is None:
            continue
        valid_days += 1
        if abs(Decimal(str(item.daily_change))) >= VOLATILITY_CHANGE_THRESHOLD:
            big_days += 1
    if valid_days < MIN_VOLATILITY_SAMPLES:
        return None
    return big_days


def _drawdown(navs, window_days: int) -> Decimal | None:
    """近 window_days 天净值最大回撤（%；净值点不足返回 None）"""
    window_start = date.today() - timedelta(days=window_days)
    ordered: list[Decimal] = []
    for item in reversed(navs):  # 转时间正序
        if item.unit_nav is None:
            continue
        try:
            item_date = datetime.strptime(item.date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if item_date >= window_start:
            ordered.append(calculator.to_decimal(item.unit_nav))
    if len(ordered) < MIN_NAV_POINTS:
        return None
    return calculator.calculate_max_drawdown(ordered)


# ---------------- 目标进度量化分析 ----------------

# 5 状态分类阈值（Phase 13，明确写入代码常量；只描述状态，不生成交易指令）
GOAL_RISK_DRAWDOWN_THRESHOLD = Decimal("10")   # 90 天回撤 ≥ 10% 触发风险关注
GOAL_RISK_VOLATILITY_DAYS_THRESHOLD = 3        # 30 天大波动天数 ≥ 3 触发风险关注
GOAL_NEAR_PROGRESS_THRESHOLD = Decimal("50")   # 进度 ≥ 50% 视为接近目标


def classify_goal_status(
    *,
    target_set: bool,
    drawdown_90d: Decimal | None,
    volatility_days_30d: int | None,
    progress_percent: Decimal | None,
    achieved: bool,
) -> tuple[str, str]:
    """目标分析状态分类（互斥单选，按优先级）：

    1. not_set          未设置（target_set=False）
    2. risk_attention   风险需要关注（90 天回撤 ≥ 阈值 或 30 天大波动 ≥ 阈值；
                        两项均无数据不触发，交由 data_issues 说明）
    3. target_achieved  已达到目标（progress ≥ 100；仅描述状态，不等于止盈/卖出建议）
    4. near_target      接近目标（progress ≥ 50%）
    5. far_from_target  距离目标较远（其余）
    """
    if not target_set:
        return "not_set", "未设置"
    risk_hit = (
        (drawdown_90d is not None and drawdown_90d >= GOAL_RISK_DRAWDOWN_THRESHOLD)
        or (
            volatility_days_30d is not None
            and volatility_days_30d >= GOAL_RISK_VOLATILITY_DAYS_THRESHOLD
        )
    )
    if risk_hit:
        return "risk_attention", "风险需要关注"
    if (progress_percent is not None and progress_percent >= Decimal("100")) or (
        progress_percent is None and achieved
    ):
        return "target_achieved", "已达到目标"
    if progress_percent is not None and progress_percent >= GOAL_NEAR_PROGRESS_THRESHOLD:
        return "near_target", "接近目标"
    return "far_from_target", "距离目标较远"


async def build_goal_analysis(db: Session) -> dict:
    """围绕用户目标的后端量化分析（纯计算，无 AI 参与）

    返回结构见 GoalAnalysisResponse；未设置目标时 target_set=false 降级返回。
    单只基金净值获取失败不中断整体，缺失指标记入 data_issues。
    """
    goal = get_goal(db)
    holdings = await portfolio_service.list_holdings(db)
    data_issues: list[str] = []

    if goal is None or goal.target_return_rate is None:
        if not holdings:
            data_issues.append("暂无持仓，添加持仓后即可查看目标进度")
        status, status_label = classify_goal_status(
            target_set=False, drawdown_90d=None,
            volatility_days_30d=None, progress_percent=None, achieved=False,
        )
        return {
            "target_set": False,
            "target_return_rate": None,
            "updated_at": None,
            "holding_count": len(holdings),
            "data_date": None,
            "status": status,
            "status_label": status_label,
            "account": None,
            "holdings": [],
            "data_issues": data_issues,
            "disclaimer": GLOBAL_DISCLAIMER,
        }

    target: Decimal = goal.target_return_rate

    # ---- 账户级：当前收益 → 距离目标 ----
    summary = await portfolio_service.get_summary(db)
    current_rate = Decimal(str(summary.total_profit_rate))
    invested = Decimal(str(summary.total_invested))
    total_profit = Decimal(str(summary.total_profit))
    gap = calculator.calc_goal_gap(current_rate, target)
    required_profit = calculator.calc_required_profit(gap, invested)
    achieved = current_rate >= target

    # 目标达成进度：current / target × 100，截断在 0~100（目标 <= 0 时无意义）
    progress: Decimal | None = None
    if target > 0:
        progress = (current_rate / target * Decimal("100")).quantize(
            PCT, rounding="ROUND_HALF_UP"
        )
        progress = max(Decimal("0"), min(Decimal("100"), progress))

    if not holdings:
        data_issues.append("暂无持仓，添加持仓后即可查看目标进度")

    # ---- 持仓级：逐只 180 天净值 → 回撤（90/180）+ 30 天波动 ----
    holding_items: list[dict] = []
    drawdown_90_list: list[tuple[Decimal, Decimal]] = []  # (市值, 90d 回撤) 用于组合加权
    volatility_max: int | None = None

    for h in holdings:
        profit_rate = Decimal(str(h.profit_rate))
        h_gap = calculator.calc_goal_gap(profit_rate, target)

        drawdown_90: Decimal | None = None
        drawdown_180: Decimal | None = None
        vol_days: int | None = None
        try:
            # 一次拉 180 天，90 天窗口直接从同一序列过滤（少一半数据源请求）
            navs = await fund_service.get_navs_for_period(
                h.fund_code, AnalysisPeriod.HALF_YEAR
            )
            drawdown_180 = _drawdown(navs, 180)
            drawdown_90 = _drawdown(navs, 90)
            vol_days = _volatility_days(navs, VOLATILITY_WINDOW_DAYS)
        except (DataSourceUnavailableError, FundNotFoundError) as e:
            data_issues.append(
                f"{h.fund_name}（{h.fund_code}）区间净值获取失败，"
                f"回撤与波动指标缺失：{e}"
            )

        if drawdown_90 is None:
            data_issues.append(
                f"{h.fund_name}（{h.fund_code}）有效净值点不足，"
                f"近 90 天回撤无法计算（不伪造数据）"
            )
        else:
            drawdown_90_list.append((Decimal(str(h.market_value)), drawdown_90))

        if vol_days is None:
            data_issues.append(
                f"{h.fund_name}（{h.fund_code}）近 30 天有效日涨跌幅样本不足，"
                f"波动天数无法统计"
            )
        elif volatility_max is None or vol_days > volatility_max:
            volatility_max = vol_days

        holding_items.append({
            "fund_code": h.fund_code,
            "fund_name": h.fund_name,
            "nav_date": h.latest_nav_date,
            "profit_rate": h.profit_rate,
            "goal_gap_percent": float(h_gap),
            "achieved": profit_rate >= target,
            "market_value": h.market_value,
            "drawdown_90d": float(drawdown_90) if drawdown_90 is not None else None,
            "drawdown_180d": float(drawdown_180) if drawdown_180 is not None else None,
            "volatility_days_30d": vol_days,
        })

    # ---- 组合级回撤 / 波动（口径见字段注释；缺任一持仓则不伪造组合值） ----
    portfolio_drawdown_90: Decimal | None = None
    if drawdown_90_list:
        total_mv = sum((mv for mv, _ in drawdown_90_list), Decimal("0"))
        if total_mv > 0:
            portfolio_drawdown_90 = sum(
                (mv / total_mv * dd for mv, dd in drawdown_90_list), Decimal("0")
            ).quantize(PCT, rounding="ROUND_HALF_UP")

    dates = [h.latest_nav_date for h in holdings if h.latest_nav_date]
    data_date = max(dates) if dates else None

    logger.info(
        "目标分析完成: 目标=%s%% 当前=%s%% 持仓=%d 数据问题=%d",
        target, current_rate, len(holdings), len(data_issues),
    )

    status, status_label = classify_goal_status(
        target_set=True,
        drawdown_90d=portfolio_drawdown_90,
        volatility_days_30d=volatility_max,
        progress_percent=progress,
        achieved=achieved,
    )

    return {
        "target_set": True,
        "target_return_rate": float(target),
        "updated_at": goal.updated_at.strftime("%Y-%m-%d %H:%M:%S")
        if goal.updated_at else None,
        "holding_count": len(holdings),
        "data_date": data_date,
        "status": status,
        "status_label": status_label,
        "account": {
            "total_invested": summary.total_invested,
            "total_profit": summary.total_profit,
            "current_profit_rate": summary.total_profit_rate,
            "target_return_rate": float(target),
            "gap_percent": float(gap),
            "required_profit": float(required_profit),
            "achieved": achieved,
            "progress_percent": float(progress) if progress is not None else None,
            "drawdown_90d": float(portfolio_drawdown_90)
            if portfolio_drawdown_90 is not None else None,
            "volatility_days_30d": volatility_max,
        },
        "holdings": holding_items,
        "data_issues": data_issues,
        "disclaimer": GLOBAL_DISCLAIMER,
    }

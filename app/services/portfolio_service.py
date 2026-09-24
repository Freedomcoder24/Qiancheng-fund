"""
持仓服务：持仓 CRUD + 收益计算 + 账户汇总

数据流：
- 持仓数据（份额、成本）来自 SQLite
- 净值数据实时从基金数据源获取（Phase 2），不落库
- 市值、收益等全部用 calculator（Decimal）实时计算

注意：由于实时估值数据源已失效，本模块所有计算基于"最新已确认净值"。
"""
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import PortfolioHolding
from app.models.fund import FundNavItem
from app.models.portfolio import (
    HoldingCreate,
    HoldingResponse,
    HoldingUpdate,
    PortfolioSummary,
    SimulatedHistoryPoint,
    SimulatedHistoryResponse,
)
from app.services import fund_service
from app.utils import calculator

logger = logging.getLogger(__name__)


# ---------------- 自定义业务异常 ----------------

class HoldingNotFoundError(Exception):
    """持仓记录不存在"""


class DuplicateHoldingError(Exception):
    """同一基金已存在持仓（同一基金只允许一条持仓记录）"""


# ---------------- 内部工具 ----------------

async def _fetch_fund_data(fund_code: str) -> tuple[str, FundNavItem | None, FundNavItem | None]:
    """从数据源获取基金名称 + 最新两条净值记录

    返回 (基金名称, 最新净值记录, 上一交易日净值记录)
    基金不存在抛 FundNotFoundError，数据源不可用抛 DataSourceUnavailableError。
    """
    detail = await fund_service.get_fund_detail(fund_code)
    history = await fund_service.get_fund_history(fund_code, page=1, page_size=2)
    latest = history.items[0] if history.items else None
    prev = history.items[1] if len(history.items) > 1 else None
    return detail.name, latest, prev


def _build_holding_response(
    holding: PortfolioHolding,
    latest: FundNavItem | None,
    prev: FundNavItem | None,
) -> HoldingResponse:
    """组合数据库持仓 + 实时净值，用 Decimal 完成收益计算

    这是全项目唯一直接操作金额数值的核心函数。
    """
    shares: Decimal = holding.shares
    cost_price: Decimal = holding.cost_price

    # 投入金额（唯一不依赖数据源的确定值）
    invested_amount = calculator.calc_invested_amount(shares, cost_price)

    # 数据源返回的净值是 float（Pydantic 模型字段类型），
    # 进入 Decimal 计算前统一转换，保证资金运算全程无 float 参与
    latest_nav: Decimal | None = None
    prev_nav: Decimal | None = None
    if latest and latest.unit_nav is not None:
        latest_nav = calculator.to_decimal(latest.unit_nav)
    if prev and prev.unit_nav is not None:
        prev_nav = calculator.to_decimal(prev.unit_nav)

    # 最新净值与市值（净值拿不到时置 0，名称信息仍然可用）
    if latest_nav is not None:
        market_value = calculator.calc_market_value(shares, latest_nav)
        profit = calculator.calc_profit(market_value, invested_amount)
        profit_rate = calculator.calc_profit_rate(profit, invested_amount)
    else:
        market_value = profit = profit_rate = Decimal("0")

    # 今日收益估算：基于最新两个交易日已确认净值的变化
    latest_nav_change = Decimal("0")
    if latest_nav is not None and prev_nav is not None:
        nav_change = calculator.calc_nav_change(latest_nav, prev_nav)
        latest_nav_change = calculator.calc_nav_change_profit(shares, nav_change)

    return HoldingResponse(
        id=holding.id,
        fund_code=holding.fund_code,
        fund_name=holding.fund_name,
        shares=float(shares),
        cost_price=float(cost_price),
        latest_nav=float(latest_nav) if latest_nav is not None else None,
        latest_nav_date=latest.date if latest else None,
        nav_daily_change=latest.daily_change if latest else None,
        market_value=float(market_value),
        invested_amount=float(invested_amount),
        profit=float(profit),
        profit_rate=float(profit_rate),
        latest_nav_change=float(latest_nav_change),
    )


# ---------------- 持仓 CRUD ----------------

async def list_holdings(db: Session) -> list[HoldingResponse]:
    """获取全部持仓（每条都实时查询最新净值并计算收益）"""
    holdings = db.scalars(
        select(PortfolioHolding).order_by(PortfolioHolding.id)
    ).all()
    results = []
    for holding in holdings:
        _, latest, prev = await _fetch_fund_data(holding.fund_code)
        results.append(_build_holding_response(holding, latest, prev))
    return results


async def get_holding(db: Session, holding_id: int) -> HoldingResponse:
    """获取单条持仓，不存在抛 HoldingNotFoundError"""
    holding = db.get(PortfolioHolding, holding_id)
    if holding is None:
        raise HoldingNotFoundError(f"持仓 {holding_id} 不存在")
    _, latest, prev = await _fetch_fund_data(holding.fund_code)
    return _build_holding_response(holding, latest, prev)


async def create_holding(db: Session, data: HoldingCreate) -> HoldingResponse:
    """添加持仓：验证基金存在 → 自动获取名称 → 查重 → 保存"""
    fund_code = data.fund_code

    # 同一基金只允许一条持仓记录
    existing = db.scalar(
        select(PortfolioHolding).where(PortfolioHolding.fund_code == fund_code)
    )
    if existing is not None:
        raise DuplicateHoldingError(f"基金 {fund_code} 已经存在于持仓中")

    # 验证基金真实存在并自动获取名称（数据源异常向上抛给路由层）
    name, latest, prev = await _fetch_fund_data(fund_code)

    holding = PortfolioHolding(
        fund_code=fund_code,
        fund_name=name,
        shares=calculator.to_decimal(data.shares),
        cost_price=calculator.to_decimal(data.cost_price),
    )
    db.add(holding)
    db.commit()
    db.refresh(holding)
    logger.info("添加持仓: %s %s 份额=%s 成本=%s", fund_code, name, data.shares, data.cost_price)

    return _build_holding_response(holding, latest, prev)


async def update_holding(
    db: Session, holding_id: int, data: HoldingUpdate
) -> HoldingResponse:
    """修改持仓（份额 / 成本），基金代码不允许修改"""
    holding = db.get(PortfolioHolding, holding_id)
    if holding is None:
        raise HoldingNotFoundError(f"持仓 {holding_id} 不存在")

    if data.shares is not None:
        holding.shares = calculator.to_decimal(data.shares)
    if data.cost_price is not None:
        holding.cost_price = calculator.to_decimal(data.cost_price)

    db.commit()
    db.refresh(holding)
    logger.info("修改持仓: id=%s 份额=%s 成本=%s", holding_id, holding.shares, holding.cost_price)

    _, latest, prev = await _fetch_fund_data(holding.fund_code)
    return _build_holding_response(holding, latest, prev)


async def delete_holding(db: Session, holding_id: int) -> None:
    """删除持仓，不存在抛 HoldingNotFoundError"""
    holding = db.get(PortfolioHolding, holding_id)
    if holding is None:
        raise HoldingNotFoundError(f"持仓 {holding_id} 不存在")
    db.delete(holding)
    db.commit()
    logger.info("删除持仓: id=%s %s", holding_id, holding.fund_code)


# ---------------- 账户汇总 ----------------

async def get_summary(db: Session) -> PortfolioSummary:
    """账户汇总：基于全部持仓的实时计算结果求和"""
    holdings = await list_holdings(db)

    # HoldingResponse 中的金额已按两位小数量化，
    # 这里转回 Decimal 求和以保持精度（float 只是 JSON 输出格式）
    total_invested = sum((Decimal(str(h.invested_amount)) for h in holdings), Decimal("0"))
    total_market_value = sum((Decimal(str(h.market_value)) for h in holdings), Decimal("0"))
    total_profit = sum((Decimal(str(h.profit)) for h in holdings), Decimal("0"))
    latest_nav_change = sum((Decimal(str(h.latest_nav_change)) for h in holdings), Decimal("0"))

    total_profit_rate = calculator.calc_profit_rate(total_profit, total_invested)

    return PortfolioSummary(
        holding_count=len(holdings),
        total_invested=float(total_invested),
        total_market_value=float(total_market_value),
        total_profit=float(total_profit),
        total_profit_rate=float(total_profit_rate),
        latest_nav_change=float(latest_nav_change),
    )


# ---------------- Phase 4：持仓历史模拟市值 ----------------

async def get_holding_simulated_history(
    db: Session, holding_id: int, period: int
) -> SimulatedHistoryResponse:
    """持仓历史模拟市值回放

    ⚠️ 口径说明（重要）：这是"模拟"数据 —— 按当前持仓份额回放历史基金净值，
    未考虑历史申购、赎回、分红及份额变化，因此不代表真实历史账户资产。
    模拟收益 = 模拟市值 - 投入金额（投入金额按当前份额 × 成本固定不变）。
    """
    holding = db.get(PortfolioHolding, holding_id)
    if holding is None:
        raise HoldingNotFoundError(f"持仓 {holding_id} 不存在")

    shares: Decimal = holding.shares
    cost_price: Decimal = holding.cost_price
    invested = calculator.calc_invested_amount(shares, cost_price)

    # 区间净值（数据源返回倒序），转正序后逐点计算模拟市值
    navs = await fund_service.get_navs_for_period(holding.fund_code, period)
    points: list[SimulatedHistoryPoint] = []
    for item in reversed(navs):
        if item.unit_nav is None:
            continue  # 单位净值缺失的记录跳过，不影响整体走势
        nav = calculator.to_decimal(item.unit_nav)
        market_value = calculator.calculate_historical_market_value(shares, nav)
        simulated_profit = calculator.calc_profit(market_value, invested)
        points.append(
            SimulatedHistoryPoint(
                date=item.date,
                nav=float(nav),
                market_value=float(market_value),
                simulated_profit=float(simulated_profit),
            )
        )

    return SimulatedHistoryResponse(
        holding_id=holding.id,
        fund_code=holding.fund_code,
        fund_name=holding.fund_name,
        period=period,
        shares=float(shares),
        invested_amount=float(invested),
        points=points,
    )

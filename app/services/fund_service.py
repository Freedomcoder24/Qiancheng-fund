"""
基金业务层：数据源 → 数据清洗 → Pydantic 模型 → 统一返回

职责：
1. 持有当前使用的数据源实例（更换数据源只改这一处）
2. 提供简单的内存缓存，短时间内重复请求同一基金时直接用缓存
3. 组合数据源完成综合查询（例如基金详情 = 历史净值 + 搜索）

路由层不应直接接触 HTTP 数据源细节，一律通过本层调用。
"""
import logging
import time
from datetime import date, timedelta

from app.models.fund import (
    FundBasicInfo,
    FundDetail,
    FundHistoryPage,
    FundPerformance,
    FundRankingPage,
    FundValuation,
    NavPoint,
)
from app.services.fund_data_source import (
    DataSourceUnavailableError,
    EastMoneyFundSource,
    FundDataSource,
    FundNotFoundError,
)
from app.utils import calculator

logger = logging.getLogger(__name__)

# 当前使用的数据源。未来更换数据源（如官方开放 API）只需要改这一行。
data_source: FundDataSource = EastMoneyFundSource()

# ---------------- 简单内存缓存（不引入 Redis，保持 Phase 2 简单） ----------------
# 结构: { 缓存键: (过期时间戳, 缓存值) }
_cache: dict[str, tuple[float, object]] = {}

HISTORY_TTL = 300  # 历史净值缓存 5 分钟（盘中不会变化）
SEARCH_TTL = 300   # 搜索结果缓存 5 分钟
PERIOD_NAV_TTL = 300  # 区间净值缓存 5 分钟（Phase 4，历史数据盘中不会变化）
RANKING_TTL = 1800    # 排行结果缓存 30 分钟（Phase 12 候选池，排行数据非盘中敏感）


def _cache_get(key: str):
    """从缓存取值，过期或不存在返回 None"""
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.time() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value, ttl: int) -> None:
    """写入缓存，ttl 为有效期（秒）"""
    _cache[key] = (time.time() + ttl, value)


def clear_cache() -> None:
    """清空缓存（主要用于测试）"""
    _cache.clear()


# ---------------- 对外提供的业务方法 ----------------

def list_funds() -> list:
    """获取我持有的基金列表（Phase 1 保留：暂无持仓，返回空列表）"""
    return []


async def get_fund_history(
    fund_code: str, page: int = 1, page_size: int = 20
) -> FundHistoryPage:
    """查询基金历史净值（带缓存）"""
    cache_key = f"history:{fund_code}:{page}:{page_size}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("历史净值命中缓存: %s", cache_key)
        return cached  # type: ignore[return-value]

    result = await data_source.get_fund_history(fund_code, page, page_size)
    _cache_set(cache_key, result, HISTORY_TTL)
    return result


async def search_funds(keyword: str) -> list[FundBasicInfo]:
    """按关键词搜索基金（带缓存）"""
    cache_key = f"search:{keyword}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]

    result = await data_source.search_funds(keyword)
    _cache_set(cache_key, result, SEARCH_TTL)
    return result


async def get_fund_valuation(fund_code: str) -> FundValuation | None:
    """查询实时估值。当前数据源不支持估值（fundgz 已失效），返回 None"""
    return await data_source.get_fund_valuation(fund_code)


async def get_fund_ranking(
    fund_type: str, page: int = 1, page_size: int = 15
) -> FundRankingPage:
    """查询基金业绩排行（Phase 12 候选池，带 30 分钟缓存）"""
    cache_key = f"ranking:{fund_type}:{page}:{page_size}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("排行命中缓存: %s", cache_key)
        return cached  # type: ignore[return-value]

    result = await data_source.get_fund_ranking(fund_type, page, page_size)
    _cache_set(cache_key, result, RANKING_TTL)
    return result


async def get_fund_detail(fund_code: str) -> FundDetail:
    """基金综合信息：最新净值（历史净值第 1 条）+ 名称类型（搜索兜底）+ 估值

    注意：名称来自搜索接口的精确匹配，如果搜索接口暂时不可用，
    详情仍然返回（名称显示为代码），不影响核心净值数据。
    """
    history = await get_fund_history(fund_code, page=1, page_size=1)
    latest_nav = history.items[0] if history.items else None

    valuation = await get_fund_valuation(fund_code)

    name: str | None = None
    fund_type: str | None = None
    try:
        matches = await search_funds(fund_code)
        exact = next((m for m in matches if m.code == fund_code), None)
        if exact is not None:
            name = exact.name
            fund_type = exact.fund_type
    except DataSourceUnavailableError:
        # 搜索失败不影响详情展示，只是没有名称
        logger.warning("获取基金 %s 名称时搜索接口不可用", fund_code)

    return FundDetail(
        code=fund_code,
        name=name or fund_code,  # 搜不到名称时直接显示代码
        fund_type=fund_type,
        latest_nav=latest_nav,
        valuation_available=valuation is not None,
        valuation=valuation,
    )


# ---------------- Phase 4：基金历史表现分析 ----------------

async def get_navs_for_period(fund_code: str, period: int) -> list:
    """查询基金最近 period 天的净值列表（带缓存，按日期倒序：最新在前）

    区间定义：start = 今天 - period 天，end = 今天。
    数据源会自动跳过周末和节假日（非交易日没有净值记录）。
    """
    end = date.today()
    start = end - timedelta(days=period)
    cache_key = f"navs_range:{fund_code}:{start.isoformat()}:{end.isoformat()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        logger.debug("区间净值命中缓存: %s", cache_key)
        return cached  # type: ignore[return-value]

    result = await data_source.get_navs_by_date_range(
        fund_code, start.isoformat(), end.isoformat()
    )
    _cache_set(cache_key, result, PERIOD_NAV_TTL)
    return result


async def get_fund_performance(fund_code: str, period: int) -> FundPerformance:
    """基金区间历史表现：区间收益率 + 最大回撤 + 净值走势点

    口径说明：这里统计的是"基金净值本身"的历史表现，
    与用户账户的真实持仓收益无关（持仓历史见 portfolio_service）。
    """
    navs = await get_navs_for_period(fund_code, period)

    # 按时间正序排列，并过滤掉单位净值缺失的记录
    ordered = [item for item in reversed(navs) if item.unit_nav is not None]
    if not ordered:
        raise DataSourceUnavailableError(f"基金 {fund_code} 区间内没有有效单位净值")

    # 名称获取失败不影响表现计算（前端显示代码即可）
    name: str | None = None
    try:
        matches = await search_funds(fund_code)
        exact = next((m for m in matches if m.code == fund_code), None)
        if exact is not None:
            name = exact.name
    except (DataSourceUnavailableError, FundNotFoundError):
        logger.warning("获取基金 %s 名称时搜索接口不可用", fund_code)

    # 净值序列转 Decimal 后计算，避免 float 参与资金运算
    nav_seq = [calculator.to_decimal(item.unit_nav) for item in ordered]
    period_return = calculator.calculate_period_return(nav_seq[0], nav_seq[-1])
    max_drawdown = calculator.calculate_max_drawdown(nav_seq)

    return FundPerformance(
        fund_code=fund_code,
        fund_name=name or fund_code,
        period=period,
        start_date=ordered[0].date,
        end_date=ordered[-1].date,
        start_nav=float(nav_seq[0]),
        end_nav=float(nav_seq[-1]),
        period_return=float(period_return),
        max_drawdown=float(max_drawdown),
        nav_points=[NavPoint(date=item.date, nav=item.unit_nav) for item in ordered],
    )

"""候选池服务（Phase 12）：基于公开历史数据的量化筛选

定位与边界（重要）：
1. 候选池 = 「符合当前量化筛选条件的候选基金」，筛选完全由后端完成：
   东方财富公开排行（股票型 / 混合型各取近 1 年收益前 15）合并去重，
   再逐只用近 180 天公开净值校验区间收益 / 最大回撤 / 30 天波动；
2. 不做任何主观推荐，不预测未来收益，页面与 AI 禁止
   「推荐购买 / 值得买 / 最优」等表述；
3. 单只基金历史数据获取失败只跳过并记 data_issues，不中断整体，
   排行接口整体失败向上抛 DataSourceUnavailableError（路由层转 503）。
"""
import asyncio
import logging
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from app.database.models import CandidateFilter
from app.models.fund import AnalysisPeriod
from app.services import fund_service
from app.services.fund_data_source import (
    DataSourceUnavailableError,
    FundNotFoundError,
)
from app.services.goal_service import GLOBAL_DISCLAIMER
from app.utils import calculator

logger = logging.getLogger(__name__)

# ---------------- 筛选参数（明确写入代码，改规则 = 改常量） ----------------

CANDIDATE_TOP_N = 15        # gp / hh 排行各取前 N 只
HISTORY_PERIOD = AnalysisPeriod.HALF_YEAR  # 历史校验区间：180 天
MAX_CONCURRENCY = 5         # 历史净值并发请求数（控制对数据源的压力）
MIN_NAV_POINTS = 3          # 回撤要求的最少净值点数（不足则剔除）
VOLATILITY_CHANGE_THRESHOLD = Decimal("2")  # 单日 |涨跌幅| >= 2% 计入大波动
MIN_VOLATILITY_SAMPLES = 5  # 波动窗口有效样本下限（不足记 None，不伪造）

FUND_TYPE_STOCK = "股票型"
FUND_TYPE_MIXED = "混合型"
DEFAULT_MIN_RETURN_1Y = Decimal("0")  # 默认近 1 年收益下限：0 = 不过滤

# 排行接口 fund_type 参数 → 类别中文名（用于过滤与规则文案）
FT_TO_TYPE = {"gp": FUND_TYPE_STOCK, "hh": FUND_TYPE_MIXED}


def build_screening_rule(
    include_stock: bool, include_mixed: bool, min_return_1y: Decimal
) -> str:
    """按用户当前筛选条件动态生成规则说明（Phase 13）

    必须保留「不构成任何推荐」整句（页面定位与既有测试依赖），
    且文案本身不得含任何推荐性 / 预测性表述。
    """
    types = []
    if include_stock:
        types.append(FUND_TYPE_STOCK)
    if include_mixed:
        types.append(FUND_TYPE_MIXED)
    min_text = f"{min_return_1y:.2f}" if min_return_1y > 0 else "0（不过滤）"
    return (
        f"按当前筛选条件：基金类型={'+'.join(types)}，近 1 年收益下限 {min_text}%；"
        f"东方财富公开排行（所选类别各取近 1 年收益前 {CANDIDATE_TOP_N}）合并去重，"
        f"逐只用近 {HISTORY_PERIOD} 天公开净值校验区间收益、最大回撤与近 30 天波动。"
        "候选池仅表示符合当前量化筛选条件，不构成任何推荐，不代表未来表现。"
    )


def get_filter(db: Session) -> dict:
    """读取当前生效的筛选条件；无记录时返回默认（全勾 + 0 + is_set=False）"""
    row = db.query(CandidateFilter).filter(CandidateFilter.id == 1).first()
    if row is None:
        return {
            "is_set": False,
            "include_stock": True,
            "include_mixed": True,
            "min_return_1y": DEFAULT_MIN_RETURN_1Y,
        }
    return {
        "is_set": row.is_set,
        "include_stock": row.include_stock,
        "include_mixed": row.include_mixed,
        "min_return_1y": row.min_return_1y,
    }


def save_filter(
    db: Session, include_stock: bool, include_mixed: bool, min_return_1y: Decimal
) -> CandidateFilter:
    """保存筛选条件（单行 upsert id=1），min_return_1y 保留 4 位小数"""
    row = db.query(CandidateFilter).filter(CandidateFilter.id == 1).first()
    if row is None:
        row = CandidateFilter(id=1)
        db.add(row)
    row.include_stock = include_stock
    row.include_mixed = include_mixed
    row.min_return_1y = min_return_1y.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    row.is_set = True
    row.updated_at = datetime.now()
    db.commit()
    db.refresh(row)
    return row


def _volatility_days(navs) -> int | None:
    """近 30 天内 |日涨跌幅| >= 阈值 的天数（口径与监控一致；样本不足返回 None）"""
    from datetime import date, datetime, timedelta

    window_start = date.today() - timedelta(days=30)
    big_days = 0
    valid_days = 0
    for item in navs:
        try:
            item_date = datetime.strptime(item.date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if item_date < window_start or item.daily_change is None:
            continue
        valid_days += 1
        if abs(Decimal(str(item.daily_change))) >= VOLATILITY_CHANGE_THRESHOLD:
            big_days += 1
    if valid_days < MIN_VOLATILITY_SAMPLES:
        return None
    return big_days


async def get_candidates(count: int = 20, db: Session | None = None) -> dict:
    """候选池生成管线：筛选条件 → 排行 → 类型/收益过滤 → 逐只历史校验

    - count：候选池上限（路由层校验 5~30）；
    - db：读取用户筛选条件（Phase 13）；None 时用默认条件（全勾 + 不过滤收益）；
    - 排行接口失败 → DataSourceUnavailableError 向上抛；
    - 单只历史失败 → 跳过并记 data_issues，不影响其他候选；
    - 过滤后不足 count 时如实返回少量结果，绝不从最差基金补位。
    """
    # 0. 读取筛选条件（存库，Phase 13；无记录 = 默认全勾 + 0）
    flt = get_filter(db) if db is not None else {
        "is_set": False,
        "include_stock": True,
        "include_mixed": True,
        "min_return_1y": DEFAULT_MIN_RETURN_1Y,
    }
    include_stock: bool = flt["include_stock"]
    include_mixed: bool = flt["include_mixed"]
    min_return_1y: Decimal = Decimal(str(flt["min_return_1y"]))

    # 1. 排行：只请求勾选的类别（fund_service 内带 30 分钟缓存）
    fetch_tasks = []
    requested_fts = [
        ft for ft, name in FT_TO_TYPE.items()
        if (name == FUND_TYPE_STOCK and include_stock)
        or (name == FUND_TYPE_MIXED and include_mixed)
    ]
    for ft in requested_fts:
        fetch_tasks.append(fund_service.get_fund_ranking(ft, 1, CANDIDATE_TOP_N))
    rankings = await asyncio.gather(*fetch_tasks)

    # 2. 合并去重（同代码保留首次出现，即类别排序更靠前的那条）
    seen: dict[str, object] = {}
    for ranking in rankings:
        for item in list(ranking.items):
            seen.setdefault(item.code, item)

    # 3. 先类型显式过滤（防御），再按近 1 年收益下限过滤；
    #    过滤发生在历史校验之前，避免浪费数据源请求
    data_issues: list[str] = []
    filtered: list[object] = []
    skipped_by_filter = 0
    for item in seen.values():
        if item.fund_type not in (FUND_TYPE_STOCK, FUND_TYPE_MIXED):
            skipped_by_filter += 1
            continue
        if item.return_1y is None:
            skipped_by_filter += 1
            data_issues.append(
                f"{item.name}（{item.code}）近 1 年收益数据缺失，未进入候选池"
            )
            continue
        if Decimal(str(item.return_1y)) < min_return_1y:
            skipped_by_filter += 1
            continue
        filtered.append(item)

    if skipped_by_filter and min_return_1y > 0:
        data_issues.append(
            f"按当前筛选条件（近 1 年收益 ≥ {min_return_1y:.2f}%），"
            f"{skipped_by_filter} 只排行基金未进入候选池"
        )
    elif skipped_by_filter:
        data_issues.append(
            f"按当前筛选条件（基金类型），{skipped_by_filter} 只排行基金未进入候选池"
        )

    ranked = filtered[:count]  # 不足 count 如实返回少量结果，不补位

    # 4. 并发逐只拉 180 天历史并计算指标（Semaphore 控制并发）
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def enrich(item) -> dict | None:
        async with semaphore:
            try:
                navs = await fund_service.get_navs_for_period(item.code, HISTORY_PERIOD)
            except (DataSourceUnavailableError, FundNotFoundError) as e:
                data_issues.append(
                    f"{item.name}（{item.code}）历史净值获取失败，已从候选池移除：{e}"
                )
                return None

        ordered = [i for i in reversed(navs) if i.unit_nav is not None]
        if len(ordered) < MIN_NAV_POINTS:
            data_issues.append(
                f"{item.name}（{item.code}）近 {HISTORY_PERIOD} 天有效净值点不足"
                f"（实际 {len(ordered)} 个），无法完成历史校验，已从候选池移除"
            )
            return None

        nav_seq = [calculator.to_decimal(i.unit_nav) for i in ordered]
        period_return = calculator.calculate_period_return(nav_seq[0], nav_seq[-1])
        max_drawdown = calculator.calculate_max_drawdown(nav_seq)
        vol_days = _volatility_days(navs)
        if vol_days is None:
            data_issues.append(
                f"{item.name}（{item.code}）近 30 天有效日涨跌幅样本不足，"
                f"波动天数无法统计"
            )

        return {
            "code": item.code,
            "name": item.name,
            "fund_type": item.fund_type,
            "nav_date": item.nav_date,
            "unit_nav": item.unit_nav,
            "daily_change": item.daily_change,
            "return_3m": item.return_3m,
            "return_6m": item.return_6m,
            "return_1y": item.return_1y,
            "return_180d": float(period_return),
            "max_drawdown_180d": float(max_drawdown),
            "volatility_days_30d": vol_days,
            "history_start": ordered[0].date,
            "history_end": ordered[-1].date,
        }

    results = await asyncio.gather(*(enrich(item) for item in ranked))
    candidates = [r for r in results if r is not None]

    nav_dates = [c["nav_date"] for c in candidates if c["nav_date"]]
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ranking_total = sum(r.total_count for r in rankings)
    logger.info(
        "候选池生成完成: 排行 %s → 过滤后 %d → 候选 %d 只, 数据问题 %d 项",
        [ft for ft in requested_fts], len(ranked), len(candidates), len(data_issues),
    )

    return {
        "generated_at": generated_at,
        "screening_rule": build_screening_rule(
            include_stock, include_mixed, min_return_1y
        ),
        "ranking_total": ranking_total,
        "count": len(candidates),
        "nav_date": max(nav_dates) if nav_dates else None,
        "candidates": candidates,
        "data_issues": data_issues,
        "disclaimer": GLOBAL_DISCLAIMER,
    }

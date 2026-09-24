"""基金相关的 API 路由

只负责：参数校验、调用业务层、把业务异常转换成对应的 HTTP 状态码。
不包含任何数据源细节（JSONP、字段转换等都在 service / data source 层）。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database.database import get_db

from app.models.fund import (
    AnalysisPeriod,
    FundBasicInfo,
    FundBrief,
    FundDetail,
    FundHistoryPage,
    FundPerformance,
    ValuationResponse,
)
from app.models.candidate import CandidatePoolResponse
from app.services import candidate_service, fund_service
from app.services.fund_data_source import (
    DataSourceUnavailableError,
    FundNotFoundError,
)

router = APIRouter(prefix="/api/funds", tags=["基金"])
logger = logging.getLogger(__name__)


def _validate_fund_code(fund_code: str) -> None:
    """校验基金代码格式（国内公募基金代码为 6 位数字），不符合返回 400"""
    if not (fund_code.isdigit() and len(fund_code) == 6):
        raise HTTPException(status_code=400, detail="基金代码应为 6 位数字，例如 000001")


@router.get("", response_model=list[FundBrief], summary="获取我的基金列表")
def list_funds():
    """返回当前持有的基金列表。

    Phase 1 还没有持仓数据，返回空列表；
    Phase 3 接入持仓管理后，这里会返回真实持仓。
    """
    return fund_service.list_funds()


# 注意：/search 与 /candidates 必须注册在 /{fund_code} 之前，
# 否则会被当成基金代码
@router.get("/search", response_model=list[FundBasicInfo], summary="搜索基金")
async def search_funds(
    keyword: str = Query(..., min_length=1, max_length=20, description="基金代码或名称关键词")
):
    """按关键词搜索基金，支持代码或名称模糊匹配，返回基金列表。"""
    try:
        return await fund_service.search_funds(keyword.strip())
    except DataSourceUnavailableError as e:
        # 数据源不可用：503 Service Unavailable
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get(
    "/candidates",
    response_model=CandidatePoolResponse,
    summary="候选池（符合当前量化筛选条件的候选基金）",
)
async def get_candidates(
    count: int = Query(20, ge=5, le=30, description="候选池数量上限"),
    db: Session = Depends(get_db),
):
    """基于公开历史数据的量化候选池：按用户筛选条件（Phase 13，存库）
    请求所选类别排行（各取近 1 年收益前 15），按类型 + 近 1 年收益下限过滤，
    合并去重后逐只用近 180 天公开净值校验区间收益 / 最大回撤 / 30 天波动。

    候选池仅表示符合当前筛选条件，不构成任何推荐，不预测未来收益。

    - 单只基金历史失败 → 200，说明放在 data_issues（跳过该只）
    - 过滤后不足 count → 如实返回少量结果（不从最差补位）
    - 排行接口整体不可用 → 503
    """
    try:
        return await candidate_service.get_candidates(count, db)
    except DataSourceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/{fund_code}/history", response_model=FundHistoryPage, summary="查询基金历史净值")
async def get_fund_history(
    fund_code: str,
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    page_size: int = Query(20, ge=1, le=50, description="每页条数，最多 50"),
):
    """分页返回基金历史净值（按日期倒序，最新在前）。"""
    _validate_fund_code(fund_code)
    try:
        return await fund_service.get_fund_history(fund_code, page, page_size)
    except FundNotFoundError as e:
        # 基金不存在：404 Not Found
        raise HTTPException(status_code=404, detail=str(e)) from e
    except DataSourceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/{fund_code}/valuation", response_model=ValuationResponse, summary="查询基金实时估值")
async def get_fund_valuation(fund_code: str):
    """返回实时估值。

    当前 fundgz 估值接口已失效（2026-09 实测），没有可用估值数据源，
    因此返回 valuation_available=false，绝不伪造估值数据。
    """
    _validate_fund_code(fund_code)
    valuation = await fund_service.get_fund_valuation(fund_code)
    return ValuationResponse(
        valuation_available=valuation is not None,
        valuation=valuation,
    )


@router.get(
    "/{fund_code}/performance",
    response_model=FundPerformance,
    summary="查询基金区间历史表现（收益 + 最大回撤）",
)
async def get_fund_performance(
    fund_code: str,
    # AnalysisPeriod 限定 period 只能是 7 / 30 / 90 / 180，
    # 传其他值 FastAPI 自动返回 422
    period: AnalysisPeriod = Query(AnalysisPeriod.MONTH, description="区间天数"),
):
    """返回基金净值在指定区间的收益率、最大回撤和净值走势点。

    注意：这是基金净值本身的历史表现，不代表任何账户的真实历史收益。
    """
    _validate_fund_code(fund_code)
    try:
        return await fund_service.get_fund_performance(fund_code, period)
    except FundNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except DataSourceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/{fund_code}", response_model=FundDetail, summary="查询基金综合信息")
async def get_fund_detail(fund_code: str):
    """返回基金基本信息 + 最新已确认净值 + 估值（若可用）。"""
    _validate_fund_code(fund_code)
    try:
        return await fund_service.get_fund_detail(fund_code)
    except FundNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except DataSourceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

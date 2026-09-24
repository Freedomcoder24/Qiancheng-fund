"""持仓 / 账户相关的 API 路由

只负责：参数校验（Pydantic 完成）、调用业务层、把业务异常转换成 HTTP 状态码。
收益计算逻辑在 calculator（Decimal），业务流程在 portfolio_service。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.models.fund import AnalysisPeriod
from app.models.portfolio import (
    HoldingCreate,
    HoldingResponse,
    HoldingUpdate,
    PortfolioSummary,
    SimulatedHistoryResponse,
    SuccessResponse,
)
from app.services import portfolio_service
from app.services.fund_data_source import (
    DataSourceUnavailableError,
    FundNotFoundError,
)
from app.services.portfolio_service import DuplicateHoldingError, HoldingNotFoundError

router = APIRouter(prefix="/api/portfolio", tags=["持仓"])


@router.get("/summary", response_model=PortfolioSummary, summary="获取账户概览")
async def get_summary(db: Session = Depends(get_db)):
    """返回账户总投入、总市值、累计收益、收益率和今日收益估算。"""
    try:
        return await portfolio_service.get_summary(db)
    except DataSourceUnavailableError as e:
        # 净值数据源不可用时汇总无法计算，返回 503
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/holdings", response_model=list[HoldingResponse], summary="获取全部持仓")
async def list_holdings(db: Session = Depends(get_db)):
    """返回所有持仓的完整数据（含实时计算的市值和收益）。"""
    try:
        return await portfolio_service.list_holdings(db)
    except DataSourceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post(
    "/holdings",
    response_model=HoldingResponse,
    status_code=201,
    summary="添加持仓",
)
async def create_holding(data: HoldingCreate, db: Session = Depends(get_db)):
    """添加持仓。基金名称根据代码自动从数据源获取，无需手动填写。

    - 基金不存在 → 404
    - 重复添加同一基金 → 409
    - 数据源不可用 → 503
    """
    try:
        return await portfolio_service.create_holding(db, data)
    except DuplicateHoldingError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except FundNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except DataSourceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/holdings/{holding_id}", response_model=HoldingResponse, summary="获取单条持仓")
async def get_holding(holding_id: int, db: Session = Depends(get_db)):
    """返回单条持仓的完整数据。持仓不存在 → 404。"""
    try:
        return await portfolio_service.get_holding(db, holding_id)
    except HoldingNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except DataSourceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.put(
    "/holdings/{holding_id}",
    response_model=HoldingResponse,
    summary="修改持仓",
)
async def update_holding(
    holding_id: int, data: HoldingUpdate, db: Session = Depends(get_db)
):
    """修改持仓的份额 / 成本。持仓不存在 → 404。换基金请删除后重新添加。"""
    try:
        return await portfolio_service.update_holding(db, holding_id, data)
    except HoldingNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except DataSourceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.delete(
    "/holdings/{holding_id}",
    response_model=SuccessResponse,
    summary="删除持仓",
)
async def delete_holding(holding_id: int, db: Session = Depends(get_db)):
    """删除持仓。持仓不存在 → 404。"""
    try:
        await portfolio_service.delete_holding(db, holding_id)
        return SuccessResponse(success=True)
    except HoldingNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ---------------- Phase 4：持仓历史模拟市值 ----------------
# 注意：本路由带子路径 /history，必须注册在它前面不影响匹配
# （/holdings/{holding_id} 只匹配两段路径，二者不冲突）

@router.get(
    "/holdings/{holding_id}/history",
    response_model=SimulatedHistoryResponse,
    summary="持仓历史模拟市值回放",
)
async def get_holding_history(
    holding_id: int,
    # AnalysisPeriod 限定 period 只能是 7 / 30 / 90 / 180，
    # 传其他值 FastAPI 自动返回 422
    period: AnalysisPeriod = Query(AnalysisPeriod.MONTH, description="回放区间天数"),
    db: Session = Depends(get_db),
):
    """按当前持仓份额回放历史基金净值，得到模拟市值曲线。

    ⚠️ 重要口径：模拟市值 = 当前份额 × 历史净值，未考虑历史申购、赎回、
    分红及份额变化，因此不代表真实历史账户资产。
    """
    try:
        return await portfolio_service.get_holding_simulated_history(db, holding_id, period)
    except HoldingNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except FundNotFoundError as e:
        # 区间内没有净值数据（基金代码错误或区间太早）
        raise HTTPException(status_code=404, detail=str(e)) from e
    except DataSourceUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

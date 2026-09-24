"""候选池筛选条件相关的 API 路由（Phase 13）

只负责：参数校验、调用业务层、把业务异常转换成 HTTP 状态码。
筛选条件存库（candidate_filters 单行 upsert），只影响量化筛选，
不构成任何推荐；系统不会据此自动交易。
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.models.candidate import CandidateFilterResponse, CandidateFilterSaveIn
from app.services import candidate_service

router = APIRouter(prefix="/api/candidates", tags=["候选池筛选条件"])


@router.get("/filter", response_model=CandidateFilterResponse, summary="获取筛选条件")
def get_filter(db: Session = Depends(get_db)):
    """返回当前生效的筛选条件；从未保存过时 is_set=false（默认全勾 + 不过滤）。"""
    flt = candidate_service.get_filter(db)
    return CandidateFilterResponse(
        is_set=flt["is_set"],
        include_stock=flt["include_stock"],
        include_mixed=flt["include_mixed"],
        min_return_1y=float(flt["min_return_1y"]),
        updated_at=None,
    )


@router.put("/filter", response_model=CandidateFilterResponse, summary="保存筛选条件")
def save_filter(body: CandidateFilterSaveIn, db: Session = Depends(get_db)):
    """保存筛选条件（单行 upsert；至少勾选一个基金类型，越界由 Pydantic 返回 422）。"""
    row = candidate_service.save_filter(
        db,
        body.include_stock,
        body.include_mixed,
        Decimal(str(body.min_return_1y)),
    )
    return CandidateFilterResponse(
        is_set=row.is_set,
        include_stock=row.include_stock,
        include_mixed=row.include_mixed,
        min_return_1y=float(row.min_return_1y),
        updated_at=row.updated_at.strftime("%Y-%m-%d %H:%M:%S")
        if row.updated_at else None,
    )

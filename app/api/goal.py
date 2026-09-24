"""投资目标相关的 API 路由（Phase 12）

只负责：参数校验、调用业务层、把业务异常转换成 HTTP 状态码。
目标进度计算在 goal_service（Decimal），AI 不参与数值计算；
目标收益只是参考线，系统不据此生成任何交易指令。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.models.goal import GoalAnalysisResponse, GoalResponse, GoalSaveIn
from app.services import goal_service
from app.utils import calculator

router = APIRouter(prefix="/api/goal", tags=["投资目标"])


@router.get("", response_model=GoalResponse, summary="获取目标收益率设置")
def get_goal(db: Session = Depends(get_db)):
    """返回目标设置状态；未设置时 is_set=false（不伪造默认目标）。"""
    row = goal_service.get_goal(db)
    if row is None or row.target_return_rate is None:
        return GoalResponse(is_set=False)
    return GoalResponse(
        is_set=True,
        target_return_rate=float(row.target_return_rate),
        updated_at=row.updated_at.strftime("%Y-%m-%d %H:%M:%S")
        if row.updated_at else None,
    )


@router.put("", response_model=GoalResponse, summary="保存目标收益率")
def save_goal(body: GoalSaveIn, db: Session = Depends(get_db)):
    """保存 / 修改目标收益率（0 < x ≤ 500，越界由 Pydantic 返回 422）。"""
    try:
        rate = calculator.to_decimal(body.target_return_rate)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if rate <= 0 or rate > 500:
        raise HTTPException(
            status_code=422, detail="目标收益率必须是大于 0 且不超过 500 的百分数"
        )
    row = goal_service.save_goal(db, rate)
    return GoalResponse(
        is_set=True,
        target_return_rate=float(row.target_return_rate),
        updated_at=row.updated_at.strftime("%Y-%m-%d %H:%M:%S")
        if row.updated_at else None,
    )


@router.get("/analysis", response_model=GoalAnalysisResponse, summary="目标收益分析")
async def get_goal_analysis(db: Session = Depends(get_db)):
    """围绕目标的账户级 / 持仓级量化分析（当前收益 → 距离目标 → 风险指标）。

    - 未设置目标 → 200 + target_set=false（友好降级，引导设置，不 400）
    - 无持仓 / 数据不足 → 200，说明放入 data_issues（不伪造数据）
    - 达到目标只显示状态，不生成任何卖出结论
    """
    return await goal_service.build_goal_analysis(db)

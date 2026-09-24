"""智能监控 API 路由（Phase 7）

只负责：调用监控服务、把业务异常转换成 HTTP 状态码。
监控规则与数值计算在 monitor_service（Decimal），AI 不参与。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.models.monitor import MonitorResponse
from app.services import monitor_service
from app.services.fund_data_source import (
    DataSourceUnavailableError,
    FundNotFoundError,
)

router = APIRouter(prefix="/api", tags=["智能监控"])


@router.get("/monitor", response_model=MonitorResponse, summary="持仓智能监控检查")
async def run_monitor(db: Session = Depends(get_db)):
    """按固定规则检查全部持仓：单日涨跌幅异常 / 持仓集中度 / 历史最大回撤 / 近期异常波动。

    - 无持仓 → 200（summary 提示添加持仓）
    - 无异常 → 200（summary 为"当前未发现明显异常"）
    - 单只基金数据不足/接口异常 → 200，具体说明放在 data_issues（不伪造数据）
    - 账户数据整体无法获取 → 503
    """
    try:
        return await monitor_service.run_monitoring(db)
    except (DataSourceUnavailableError, FundNotFoundError) as e:
        # 与 AI 分析路由同一约定：数据源整体不可用 → 503
        raise HTTPException(status_code=503, detail=str(e)) from e

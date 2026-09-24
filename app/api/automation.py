"""后台自动化 API 路由（Phase 8）

只负责：查询最近快照 / 最新日报、手动触发一轮自动检查；
定时循环、快照落库、日报生成逻辑全部在 automation_service。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.database.models import DailyReport, MonitorSnapshot
from app.models.automation import AutoRunResponse, DailyReportOut, MonitorSnapshotOut
from app.services import automation_service

router = APIRouter(prefix="/api/auto", tags=["自动化"])


@router.get(
    "/latest",
    response_model=MonitorSnapshotOut | None,
    summary="最近一次自动检查快照",
)
def latest_snapshot(db: Session = Depends(get_db)):
    """返回最近一次后台自动检查的快照（含执行时间 / 提醒内容 / 数据问题）。

    尚未执行过自动检查时返回 null（前端显示引导文案，不报错）。
    """
    row = (
        db.query(MonitorSnapshot)
        .order_by(MonitorSnapshot.executed_at.desc(), MonitorSnapshot.id.desc())
        .first()
    )
    return automation_service.snapshot_to_out(row) if row else None


@router.get(
    "/daily-report",
    response_model=DailyReportOut | None,
    summary="最新 AI 每日报告",
)
def latest_daily_report(db: Session = Depends(get_db)):
    """返回最近一份 AI 每日报告（含生成时间 / 内容 / 免责声明）。

    一份都没有时返回 null（当天报告由定时任务自动生成，每天最多一份）。
    """
    row = (
        db.query(DailyReport)
        .order_by(DailyReport.report_date.desc(), DailyReport.id.desc())
        .first()
    )
    return automation_service.report_to_out(row) if row else None


@router.post(
    "/run",
    response_model=AutoRunResponse,
    summary="手动触发一次自动检查（与定时任务同一逻辑）",
)
async def run_once(db: Session = Depends(get_db)):
    """立即执行一轮：智能监控 → 保存快照 → AI 日报（当天已有则跳过）。

    与后台定时任务共用 run_auto_check；各阶段失败不抛 5xx，
    而是记录在 errors 字段中如实返回（与指令"失败不能导致任务崩溃"一致）。
    """
    return await automation_service.run_auto_check(db)

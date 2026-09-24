"""后台自动化相关的数据模型（Pydantic，Phase 8）

快照 / 日报的内容全部来自后端已有计算（monitor_service / ai_service），
这里只负责 API 出入参结构；提醒内容复用 Phase 7 的 MonitorAlert。
"""
from pydantic import BaseModel, Field

from app.models.monitor import MonitorAlert


class MonitorSnapshotOut(BaseModel):
    """最近一次自动检查快照"""

    executed_at: str                        # 执行时间（YYYY-MM-DD HH:MM:SS，本地时区）
    checked_count: int                      # 本次检查的持仓数量
    alert_count: int                        # 触发的提醒数量
    summary: str = ""                       # 无提醒时的整体说明
    alerts: list[MonitorAlert] = Field(default_factory=list)  # 提醒内容（与 /api/monitor 同结构）
    data_issues: list[str] = Field(default_factory=list)      # 数据问题（不伪造数据）


class DailyReportContent(BaseModel):
    """AI 每日报告的三块内容（与 Phase 6 AI 分析输出结构一致）"""

    today_summary: str = ""
    profit_sources: str = ""
    risk_warnings: list[str] = Field(default_factory=list)


class DailyReportOut(BaseModel):
    """AI 每日报告（每天最多一份）"""

    report_date: str        # 报告所属日期（YYYY-MM-DD）
    generated_at: str       # 生成时间（YYYY-MM-DD HH:MM:SS）
    model: str              # 生成时使用的模型名
    data_date: str | None   # 依据的最新已确认净值日期
    content: DailyReportContent
    disclaimer: str         # 免责声明（后端固定附带，与内容一起保存）


class AutoRunResponse(BaseModel):
    """手动触发一次自动检查的返回（与定时任务执行同一逻辑）"""

    snapshot: MonitorSnapshotOut | None = None       # 本次保存的快照
    daily_report_generated: bool = False             # 本次是否新生成了日报（当天已有则为 False）
    daily_report: DailyReportOut | None = None       # 本次新生成的日报
    errors: list[str] = Field(default_factory=list)  # 各阶段失败说明（不中断、不伪造数据）

"""后台自动化服务（Phase 8）：定时执行监控 + 生成 AI 每日报告

职责边界（重要）：
1. 定时任务基于 asyncio 后台协程实现（不引入新依赖），在 FastAPI lifespan
   中启动 / 停止，不阻塞主线程；
2. 每轮检查 = 智能监控（monitor_service，数值全部后端 Decimal 计算）+ 保存快照
   + AI 每日报告（复用 ai_service.run_analysis，每天最多一份）；
3. 任一阶段失败（AI / 数据源 / 落库）只记录日志与 errors，绝不中断任务循环，
   下一轮继续——失败本身如实写入快照的 data_issues，不伪造数据；
4. 不做自动交易，不产生买卖建议。

配置项（.env，均有默认值，不硬编码在逻辑里）：
- AUTO_TASK_ENABLED              是否启用后台任务（默认 true）
- AUTO_MONITOR_INTERVAL_MINUTES  检查间隔分钟数（默认 60，最小 1；开发环境勿设过短）
- AUTO_DAILY_REPORT_ENABLED      是否自动生成 AI 日报（默认 true）
"""
import asyncio
import json
import logging
import os
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.database.database import SessionLocal
from app.database.models import DailyReport, MonitorSnapshot
from app.models.automation import (
    AutoRunResponse,
    DailyReportContent,
    DailyReportOut,
    MonitorSnapshotOut,
)
from app.models.monitor import MonitorAlert, MonitorResponse
from app.services import ai_service, monitor_service

logger = logging.getLogger(__name__)

# ---------------- 配置默认值 ----------------

DEFAULT_INTERVAL_MINUTES = 60   # 开发环境默认 1 小时一轮，避免频繁调用外部接口与 AI
MIN_INTERVAL_MINUTES = 1        # 间隔下限（防止误配成秒级狂调数据源）

_TRUTHY = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _env_interval() -> float:
    raw = os.getenv("AUTO_MONITOR_INTERVAL_MINUTES", "").strip()
    if not raw:
        return DEFAULT_INTERVAL_MINUTES
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "AUTO_MONITOR_INTERVAL_MINUTES 配置无效（%r），使用默认 %s 分钟",
            raw, DEFAULT_INTERVAL_MINUTES,
        )
        return DEFAULT_INTERVAL_MINUTES
    if value < MIN_INTERVAL_MINUTES:
        logger.warning(
            "AUTO_MONITOR_INTERVAL_MINUTES=%s 小于下限，已按 %s 分钟执行",
            raw, MIN_INTERVAL_MINUTES,
        )
        return float(MIN_INTERVAL_MINUTES)
    return value


def get_scheduler_config() -> dict:
    """读取定时任务配置（每次调用时读取，便于测试与运行期观察）"""
    return {
        "enabled": _env_bool("AUTO_TASK_ENABLED", True),
        "interval_minutes": _env_interval(),
        "daily_report_enabled": _env_bool("AUTO_DAILY_REPORT_ENABLED", True),
    }


# ---------------- ORM 行 → Pydantic 输出 ----------------

def snapshot_to_out(row: MonitorSnapshot) -> MonitorSnapshotOut:
    """快照行转 API 输出（JSON 文本还原为结构化数据）"""
    return MonitorSnapshotOut(
        executed_at=row.executed_at.strftime("%Y-%m-%d %H:%M:%S"),
        checked_count=row.checked_count,
        alert_count=row.alert_count,
        summary=row.summary or "",
        alerts=[MonitorAlert(**item) for item in json.loads(row.alerts_json or "[]")],
        data_issues=json.loads(row.data_issues_json or "[]"),
    )


def report_to_out(row: DailyReport) -> DailyReportOut:
    """日报行转 API 输出"""
    content = json.loads(row.content_json or "{}")
    return DailyReportOut(
        report_date=row.report_date,
        generated_at=row.generated_at.strftime("%Y-%m-%d %H:%M:%S"),
        model=row.model or "",
        data_date=row.data_date,
        content=DailyReportContent(
            today_summary=content.get("today_summary", ""),
            profit_sources=content.get("profit_sources", ""),
            risk_warnings=content.get("risk_warnings", []) or [],
        ),
        disclaimer=row.disclaimer or "",
    )


# ---------------- 快照与日报落库 ----------------

def save_monitor_snapshot(db: Session, result: MonitorResponse) -> MonitorSnapshot:
    """把一次监控结果保存为快照"""
    row = MonitorSnapshot(
        executed_at=datetime.now(),
        checked_count=result.checked_count,
        alert_count=result.alert_count,
        summary=result.summary,
        alerts_json=json.dumps(
            [alert.model_dump() for alert in result.alerts], ensure_ascii=False
        ),
        data_issues_json=json.dumps(result.data_issues, ensure_ascii=False),
    )
    db.add(row)
    db.commit()
    return row


def save_failure_snapshot(db: Session, message: str) -> MonitorSnapshot:
    """监控整体失败时也保存一份"失败记录"快照（如实记录，不伪造提醒）"""
    row = MonitorSnapshot(
        executed_at=datetime.now(),
        checked_count=0,
        alert_count=0,
        summary="",
        alerts_json="[]",
        data_issues_json=json.dumps([f"自动检查未完成：{message}"], ensure_ascii=False),
    )
    db.add(row)
    db.commit()
    return row


async def generate_daily_report(db: Session) -> DailyReport | None:
    """生成今天的 AI 每日报告（每天最多一份）

    完全复用 Phase 6 的 ai_service.run_analysis（同样的数据组装 / prompt /
    模型调用 / JSON 解析 / 免责声明），只是把结果持久化到 daily_reports 表。
    当天已有报告时直接返回 None，不再调用模型（避免频繁消耗 AI 调用）。
    """
    today = date.today().isoformat()
    existing = (
        db.query(DailyReport).filter(DailyReport.report_date == today).first()
    )
    if existing is not None:
        logger.info("今日（%s）AI 日报已存在，跳过生成", today)
        return None

    result = await ai_service.run_analysis(db)
    row = DailyReport(
        report_date=today,
        generated_at=datetime.now(),
        model=result.model,
        data_date=result.data_date,
        content_json=json.dumps(
            {
                "today_summary": result.today_summary,
                "profit_sources": result.profit_sources,
                "risk_warnings": result.risk_warnings,
            },
            ensure_ascii=False,
        ),
        disclaimer=result.disclaimer,
    )
    db.add(row)
    db.commit()
    logger.info("AI 每日报告已生成并保存（%s）", today)
    return row


# ---------------- 单轮自动检查（定时任务与手动触发共用） ----------------

async def run_auto_check(db: Session) -> AutoRunResponse:
    """执行一轮自动检查：智能监控 → 保存快照 → AI 日报（当天一份）

    错误隔离约定（指令 7）：
    - 监控失败（如数据源不可用）→ 保存"失败记录"快照，继续日报阶段；
    - 快照落库失败 → 记入 errors，继续日报阶段；
    - 日报任何失败（未配置 / 无持仓 / 上游异常 / 解析失败）→ 记入 errors，
      不影响已保存的快照；
    - 任何异常都不会向上传播导致任务循环退出。
    """
    errors: list[str] = []
    snapshot_out: MonitorSnapshotOut | None = None

    # ---- 阶段 1：智能监控 + 快照 ----
    try:
        result = await monitor_service.run_monitoring(db)
    except Exception as e:  # noqa: BLE001 任务循环内不允许异常逃逸
        logger.error("自动监控执行失败（本轮已记录）: %s", e)
        errors.append(f"监控检查失败：{e}")
        try:
            row = save_failure_snapshot(db, str(e))
            snapshot_out = snapshot_to_out(row)
        except Exception as db_err:  # noqa: BLE001
            logger.error("失败快照保存失败: %s", db_err)
            errors.append(f"监控快照保存失败：{db_err}")
    else:
        try:
            row = save_monitor_snapshot(db, result)
            snapshot_out = snapshot_to_out(row)
        except Exception as db_err:  # noqa: BLE001
            logger.error("监控快照保存失败: %s", db_err)
            errors.append(f"监控快照保存失败：{db_err}")

    # ---- 阶段 2：AI 每日报告（当天最多一份） ----
    report_out: DailyReportOut | None = None
    generated = False
    if get_scheduler_config()["daily_report_enabled"]:
        try:
            row = await generate_daily_report(db)
            if row is not None:
                generated = True
                report_out = report_to_out(row)
        except Exception as e:  # noqa: BLE001
            # 未配置 AI / 无持仓 / 上游失败 / 解析失败等都归到这里，只记录不中断
            logger.warning("AI 每日报告生成失败（不影响监控快照）: %s", e)
            errors.append(f"AI 每日报告生成失败：{e}")

    return AutoRunResponse(
        snapshot=snapshot_out,
        daily_report_generated=generated,
        daily_report=report_out,
        errors=errors,
    )


# ---------------- 后台循环与生命周期 ----------------

# 当前后台任务句柄（模块级单例；lifespan 启动 / 停止时维护）
_task: asyncio.Task | None = None

# 启动后首轮检查的短延迟（秒）：让 lifespan / 数据库完全就绪后再跑第一轮，
# 避免服务刚起就打外部接口；首轮之后才按完整间隔循环。
FIRST_RUN_DELAY_SECONDS = 5.0


async def _run_tick() -> None:
    """执行一轮检查（独立封装，异常不逃逸，供首轮与定时循环复用）"""
    db = SessionLocal()
    try:
        await run_auto_check(db)
    except asyncio.CancelledError:
        raise  # 停止信号必须继续向上抛，保证任务能被正常取消
    except Exception:  # noqa: BLE001 兜底：任何异常都不允许杀死循环
        logger.exception("自动检查轮次出现未预期异常，将在下一轮继续")
    finally:
        db.close()


async def _auto_loop(interval_minutes: float) -> None:
    """定时循环（Phase 9 优化首轮时机）

    - 启动后只等 FIRST_RUN_DELAY_SECONDS 就执行首轮检查，不必等完整间隔；
    - 首轮就在本循环协程内执行（不另起任务），执行完才开始计时间隔，
      天然避免与正常定时循环重复执行。
    """
    interval_seconds = interval_minutes * 60
    logger.info(
        "自动检查循环开始运行：启动后 %.0f 秒执行首轮，之后每 %.1f 分钟一轮",
        FIRST_RUN_DELAY_SECONDS, interval_minutes,
    )
    await asyncio.sleep(FIRST_RUN_DELAY_SECONDS)
    await _run_tick()
    while True:
        await asyncio.sleep(interval_seconds)
        await _run_tick()


def start_scheduler() -> None:
    """应用启动时调用：按配置启动后台任务（重复调用安全）"""
    global _task
    config = get_scheduler_config()
    if not config["enabled"]:
        logger.info("后台自动检查任务未启用（AUTO_TASK_ENABLED=false）")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_auto_loop(config["interval_minutes"]))
    logger.info(
        "后台自动检查任务已启动：间隔 %s 分钟，AI 日报%s",
        config["interval_minutes"],
        "开启" if config["daily_report_enabled"] else "关闭",
    )


async def stop_scheduler() -> None:
    """应用关闭时调用：取消后台任务并等待其退出"""
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass  # 正常取消路径
    _task = None
    logger.info("后台自动检查任务已停止")

"""
数据库表模型（SQLAlchemy ORM）

表清单：
- PortfolioHolding（Phase 3）：用户持仓
- MonitorSnapshot（Phase 8）：每次自动监控保存一份快照
- DailyReport（Phase 8）：AI 每日报告（每天最多一份）
- WebAiConfig（Phase 9）：Web 页面保存的 AI API 配置（单行，Key 加密存储）
 - InvestmentGoal（Phase 12）：用户设定的目标收益率（单行，默认 NULL=未设置）

设计原则：
- 持仓收益、净值等会变化的数据一律实时计算，不落库；
- Phase 8 起快照 / 日报属于"历史记录"性质，按要求持久化保存。
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.database import Base


class PortfolioHolding(Base):
    """持仓表：用户持有的某只基金"""

    __tablename__ = "portfolio_holdings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    fund_code: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    # 基金名称在添加时自动从数据源获取并保存，避免每次展示都要再查询
    fund_name: Mapped[str] = mapped_column(String(100))

    # Numeric(asdecimal=True)：读取时返回 Decimal，避免 float 精度问题
    # shares 保留 4 位小数（份额），cost_price 保留 4 位小数（净值）
    shares: Mapped[Decimal] = mapped_column(Numeric(18, 4, asdecimal=True))
    cost_price: Mapped[Decimal] = mapped_column(Numeric(18, 4, asdecimal=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class MonitorSnapshot(Base):
    """监控快照表（Phase 8）：后台定时任务每次自动检查后保存一份

    提醒内容 / 数据问题以 JSON 文本存储（结构与 Phase 7 的
    MonitorAlert 完全一致），读取时由 automation_service 解析回 Pydantic。
    """

    __tablename__ = "monitor_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 执行时间由应用写入本地时间（不使用 server_default，避免 SQLite UTC 偏差）
    executed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)

    checked_count: Mapped[int] = mapped_column(default=0)   # 本次检查的持仓数量
    alert_count: Mapped[int] = mapped_column(default=0)     # 触发的提醒数量
    summary: Mapped[str] = mapped_column(Text, default="")  # 无提醒时的整体说明
    alerts_json: Mapped[str] = mapped_column(Text, default="[]")        # 提醒内容（JSON 数组）
    data_issues_json: Mapped[str] = mapped_column(Text, default="[]")   # 数据问题（JSON 数组）


class DailyReport(Base):
    """AI 每日报告表（Phase 8）：每天最多一份，复用 Phase 6 AI 分析逻辑生成

    report_date 唯一约束保证"每天一份"；content_json 保存分析三块内容
    （today_summary / profit_sources / risk_warnings），免责声明固定后端附带。
    """

    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 报告所属日期（YYYY-MM-DD），唯一：当天已有报告则不再重复生成
    report_date: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    model: Mapped[str] = mapped_column(String(100), default="")   # 生成时使用的模型名
    data_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # 依据的净值日期
    content_json: Mapped[str] = mapped_column(Text, default="{}")  # 报告内容（JSON）
    disclaimer: Mapped[str] = mapped_column(Text, default="")      # 免责声明（固定文案）


class WebAiConfig(Base):
    """Web AI API 配置表（Phase 9）：Dashboard 保存的配置，单行（id=1）

    安全约定（重要）：
    - API Key 使用 FUND_PILOT_SECRET_KEY 派生的 Fernet 密钥加密后存入
      encrypted_api_key，数据库中绝不出现明文 Key；
    - base_url / model 不含敏感信息，明文保存（便于页面回显）；
    - 删除该行即"清除 Web 配置"，自动回退使用 .env 中的配置。
    """

    __tablename__ = "web_ai_configs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 加密后的 API Key（Fernet token，base64 文本）；未保存 Key 时为空串
    encrypted_api_key: Mapped[str] = mapped_column(Text, default="")
    base_url: Mapped[str] = mapped_column(String(255), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class InvestmentGoal(Base):
    """投资目标表（Phase 12）：用户设定的目标收益率，单行（id=1）

    设计要点：
    - target_return_rate 为百分数（如 5 表示 5%），默认 NULL = 未设置，
      不伪造默认目标，未设置时目标分析功能友好降级为引导设置；
    - 目标收益只是用户自设的参考线，系统不会据此生成任何卖出 / 止盈指令。
    """

    __tablename__ = "investment_goals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    target_return_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4, asdecimal=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class CandidateFilter(Base):
    """候选池筛选条件表（Phase 13）：用户自定义的简单筛选条件，单行（id=1）

    设计要点：
    - 可调维度仅两个：基金类型勾选（股票型 / 混合型）+ 近 1 年收益下限（%），
      其余指标（近 6 月 / 近 3 月 / 回撤 / 波动）作为固定数据列展示；
    - is_set=False 表示从未保存过（前端显示「默认条件」），默认值 = 全勾 + 0
      （不过滤收益），行为与 Phase 12 完全一致；
    - 条件只影响候选池的量化筛选，不构成任何推荐。
    """

    __tablename__ = "candidate_filters"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    include_stock: Mapped[bool] = mapped_column(default=True)   # 勾选股票型
    include_mixed: Mapped[bool] = mapped_column(default=True)   # 勾选混合型
    min_return_1y: Mapped[Decimal] = mapped_column(
        Numeric(8, 4, asdecimal=True), default=Decimal("0")
    )   # 近 1 年收益下限（%），0 = 不过滤
    is_set: Mapped[bool] = mapped_column(default=False)         # 是否保存过自定义条件
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

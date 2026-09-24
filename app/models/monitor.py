"""智能监控相关的数据模型（Pydantic，Phase 7）

监控结果全部由后端固定规则计算生成（calculator + 现有服务数据），
AI 不参与任何数值计算；提醒只做风险提示，不构成买卖建议。
"""
from pydantic import BaseModel, Field


class MonitorAlert(BaseModel):
    """单条监控提醒

    type 使用稳定英文标识（前端/测试判断用），
    type_label 为中文类型名（直接展示用）。
    """

    type: str           # daily_change / concentration / max_drawdown / volatility
    type_label: str     # 单日涨跌幅异常 / 持仓集中度过高 / 历史最大回撤 / 近期异常波动
    level: str          # info / warning / danger
    level_label: str    # 提示 / 注意 / 高风险
    fund_code: str | None = None   # 账户级提醒（如集中度）也带基金代码；无关联时为 None
    fund_name: str | None = None
    reason: str         # 触发原因（人话说明）
    detail: str         # 具体数据（触发时的真实数值）


class MonitorResponse(BaseModel):
    """智能监控检查结果"""

    generated_at: str              # 检查时间（YYYY-MM-DD HH:MM:SS，本地时区）
    data_date: str | None          # 依据的最新已确认净值日期（所有持仓中最新的）
    checked_count: int             # 本次检查的持仓数量
    alert_count: int               # 触发的提醒数量
    summary: str                   # 无提醒时的整体说明（有提醒时为空字符串）
    alerts: list[MonitorAlert] = Field(default_factory=list)  # 按等级排序（高风险在前）
    data_issues: list[str] = Field(default_factory=list)      # 数据不足/接口异常说明（不伪造数据）
    disclaimer: str = (
        "监控提醒由固定规则自动生成，仅供参考，不构成投资建议，也不构成任何买卖建议；"
        "系统不会进行任何自动交易操作。"
    )

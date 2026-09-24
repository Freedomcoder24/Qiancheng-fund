"""大盘行情服务（Phase 20）：A股主要指数的实时 / 最新收盘行情

数据源：腾讯公开行情接口（qt.gtimg.cn，文本协议，GBK 编码）——国内直连稳定、
无需鉴权。非官方开放 API、无 SLA 保证，失败时如实抛 DataSourceUnavailableError，
绝不伪造行情数据。只做只读查询，不落库。

字段布局（v_sh000001="..." 以 ~ 分割，实测验证）：
  [1] 指数名称  [2] 代码  [3] 当前点位
  [30] 更新时间 YYYYMMDDHHMMSS  [31] 涨跌点  [32] 涨跌幅（%）

市场状态口径（如实、不猜测交易日历）：
- 更新时间为今天且距现在 < 10 分钟 → "交易中（数据实时更新）"
- 更新时间为今天但更早           → "今日已收盘（显示最新数据）"
- 更新时间不是今天               → "休市（非交易日，显示最近交易日数据）"
精确的节假日判断超出范围，更新时间本身即事实来源。
"""
import logging
import re
from datetime import datetime

import httpx

from app.services.fund_data_source import DataSourceUnavailableError

logger = logging.getLogger(__name__)

# 支持的指数（腾讯行情符号：前缀 sh=上海 sz=深圳）
SUPPORTED_INDEXES: list[dict] = [
    {"name": "上证指数", "code": "000001", "symbol": "sh000001"},
    {"name": "深证成指", "code": "399001", "symbol": "sz399001"},
    {"name": "创业板指", "code": "399006", "symbol": "sz399006"},
]

# 请求行情的时间差阈值（秒）：在此之内视为"交易中实时更新"
LIVE_WINDOW_SECONDS = 600

_MARKET_URL = "https://qt.gtimg.cn/q="


def _market_status(update_dt: datetime) -> str:
    """根据行情更新时间给出如实的市场状态说明"""
    now = datetime.now()
    if update_dt.date() == now.date():
        if (now - update_dt).total_seconds() < LIVE_WINDOW_SECONDS:
            return "交易中（数据实时更新）"
        return "今日已收盘（显示最新数据）"
    return "休市（非交易日，显示最近交易日数据）"


def _parse_row(name: str, body: str) -> dict:
    """解析单条指数行情文本（字段缺失 / 非数字时如实抛错）"""
    parts = body.split("~")
    if len(parts) < 33:
        raise DataSourceUnavailableError(f"{name}：行情字段不完整")

    def field(idx: int, label: str) -> str:
        value = parts[idx].strip()
        if not value:
            raise DataSourceUnavailableError(f"{name}：{label}数据暂不可用")
        return value

    raw_time = field(30, "更新时间")  # YYYYMMDDHHMMSS
    try:
        update_dt = datetime.strptime(raw_time, "%Y%m%d%H%M%S")
    except ValueError:
        raise DataSourceUnavailableError(f"{name}：更新时间格式无法解析") from None

    try:
        current = float(field(3, "当前点位"))
        change_point = float(field(31, "涨跌点"))
        change_percent = float(field(32, "涨跌幅"))
    except ValueError:
        raise DataSourceUnavailableError(f"{name}：行情数值无法解析") from None

    return {
        "name": name,
        "code": field(2, "代码"),
        "current_point": round(current, 2),
        "change_point": round(change_point, 2),
        "change_percent": round(change_percent, 2),
        "update_time": update_dt.strftime("%Y-%m-%d %H:%M:%S"),
    }


async def get_market_indexes(
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """查询上证指数 / 深证成指 / 创业板指的行情

    返回 {market_status, indexes: [{name, code, current_point, change_point,
    change_percent, update_time}], data_issues}。
    数据源不可用抛 DataSourceUnavailableError；单只指数缺失不中断
    整体查询，写入 data_issues 如实说明。
    """
    symbols = ",".join(item["symbol"] for item in SUPPORTED_INDEXES)
    try:
        async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
            resp = await client.get(_MARKET_URL + symbols)
            resp.raise_for_status()
            text = resp.content.decode("gbk", errors="replace")
    except (httpx.HTTPError, LookupError) as e:
        logger.error("大盘行情请求失败: %s", type(e).__name__)
        raise DataSourceUnavailableError(f"大盘行情数据源请求失败：{e}") from e

    rows: dict[str, str] = {}
    for line in text.strip().splitlines():
        # 格式：v_sh000001="1~上证指数~..."（正则提取引号内内容，规避行尾 '";' 残留）
        if "=" not in line or '"' not in line:
            continue
        key, _, body = line.partition("=")
        matched = re.search(r'"(.*)"', body)
        if matched is None:
            continue
        rows[key.strip().lower().removeprefix("v_")] = matched.group(1)

    indexes: list[dict] = []
    issues: list[str] = []
    for item in SUPPORTED_INDEXES:
        body = rows.get(item["symbol"])
        if not body:
            issues.append(f"{item['name']}：数据源未返回行情")
            continue
        try:
            indexes.append(_parse_row(item["name"], body))
        except DataSourceUnavailableError as e:
            issues.append(str(e))

    if not indexes and not issues:
        raise DataSourceUnavailableError("大盘行情数据源返回了空数据")
    if not indexes and issues:
        # 全部失败时把问题汇总抛出（错误信息如实）
        raise DataSourceUnavailableError("；".join(issues))

    # 整体状态取自更新时间最新的那只指数（三只指数更新时间基本一致）
    latest = max(indexes, key=lambda i: i["update_time"])
    market_status = _market_status(
        datetime.strptime(latest["update_time"], "%Y-%m-%d %H:%M:%S")
    )

    logger.info("大盘行情完成: %d 只指数, 状态=%s, 问题=%d",
                len(indexes), market_status, len(issues))
    return {
        "market_status": market_status,
        "indexes": indexes,
        "data_issues": issues,
    }

"""Agent Tool 注册表（Phase 15）

职责：把现有 service 层函数封装为 OpenAI Tool Calling 可用的工具。
- 每个工具 = 一份 JSON Schema 定义（给模型看）+ 一个执行器（实际调用 service）；
- 不实现任何业务逻辑，全部直接调用 fund_service / portfolio_service 现有函数，
  数据源缓存、Decimal 计算等既有机制自动生效；
- 执行器不抛异常：所有失败（参数非法 / 基金不存在 / 数据源不可用）都转成
  {"error": ...} 返回给模型，让它自行纠正或如实告知用户，不打断 Agent 循环。
"""
import json
import logging

from sqlalchemy.orm import Session

from app.models.fund import AnalysisPeriod
from app.services import fund_service, market_service, portfolio_service
from app.services.fund_data_source import (
    DataSourceUnavailableError,
    FundNotFoundError,
)
from app.services.portfolio_service import HoldingNotFoundError

logger = logging.getLogger(__name__)

# 表现 / 回放类工具支持的区间（单一来源：AnalysisPeriod 枚举 7 / 30 / 90 / 180）
PERIOD_ENUM = [p.value for p in AnalysisPeriod]
VALID_PERIODS = tuple(PERIOD_ENUM)


class ToolParamError(Exception):
    """工具参数非法（信息会作为 error 返回给模型，让它自行纠正）"""


# ---------------- 工具定义（OpenAI tools 参数格式） ----------------

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_fund_detail",
            "description": "查询单只基金的基本信息：基金名称、基金类型、最新已确认净值及净值日期。fund_code 为 6 位数字基金代码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fund_code": {
                        "type": "string",
                        "description": "6 位数字基金代码，例如 000001",
                    }
                },
                "required": ["fund_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fund_history",
            "description": "分页查询单只基金的历史净值（按日期倒序，最新在前）。注意是分页数据：page 从 1 开始，page_size 每页最多 50 条，只返回当前页，不代表全部历史。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fund_code": {
                        "type": "string",
                        "description": "6 位数字基金代码",
                    },
                    "page": {
                        "type": "integer",
                        "description": "页码，从 1 开始，默认 1",
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "每页条数（1-50），默认 20",
                    },
                },
                "required": ["fund_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fund_performance",
            "description": "查询单只基金在指定区间的历史表现：区间收益率、最大回撤、起止净值与净值走势点。period 仅支持 7 / 30 / 90 / 180（天）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fund_code": {
                        "type": "string",
                        "description": "6 位数字基金代码",
                    },
                    "period": {
                        "type": "integer",
                        "description": "区间天数，只能是 7 / 30 / 90 / 180 之一，默认 30",
                        "enum": PERIOD_ENUM,
                    },
                },
                "required": ["fund_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_holdings",
            "description": "查询用户当前全部持仓：每条包含基金代码、名称、份额、成本、最新净值、市值、投入金额、累计收益、收益率、今日收益估算。分析用户持仓前应先调用本工具。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_portfolio_summary",
            "description": "查询用户账户汇总：持仓数量、总投入、总市值、累计收益、累计收益率、今日收益估算。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_holding_history",
            "description": "查询单条持仓在指定区间的历史模拟市值回放。holding_id 需先通过 get_holdings 获取；period 仅支持 7 / 30 / 90 / 180（天）。注意：这是按当前份额回放历史净值的模拟数据，不代表真实历史账户资产。",
            "parameters": {
                "type": "object",
                "properties": {
                    "holding_id": {
                        "type": "integer",
                        "description": "持仓记录 ID（来自 get_holdings 返回的 id 字段）",
                    },
                    "period": {
                        "type": "integer",
                        "description": "回放区间天数，只能是 7 / 30 / 90 / 180 之一，默认 30",
                        "enum": PERIOD_ENUM,
                    },
                },
                "required": ["holding_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_index",
            "description": "查询A股大盘指数行情：上证指数、深证成指、创业板指的当前点位、涨跌点、涨跌幅与更新时间，并附市场状态（交易中/已收盘/休市）。不传 index 时返回全部三个指数。",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "string",
                        "description": "可选。要查询的指数名称：上证指数 / 深证成指 / 创业板指；留空返回全部",
                    }
                },
            },
        },
    },
]

# 工具名 → 执行器
TOOL_EXECUTORS = {}


def _executor(name: str):
    """注册装饰器：把执行器函数挂到 TOOL_EXECUTORS 表"""
    def wrap(fn):
        TOOL_EXECUTORS[name] = fn
        return fn
    return wrap


# ---------------- 参数校验小工具 ----------------

def _check_fund_code(value) -> str | None:
    """校验基金代码格式，非法时返回错误信息（国内公募基金为 6 位数字）"""
    if not isinstance(value, str) or not (value.isdigit() and len(value) == 6):
        return f"fund_code 必须是 6 位数字字符串，收到：{value!r}"
    return None


def _to_int(value, name: str) -> int:
    """把模型给出的参数安全转成 int（模型可能传字符串 / 浮点数）"""
    if value is None:
        raise ToolParamError(f"缺少参数 {name}")
    if isinstance(value, bool):
        raise ToolParamError(f"参数 {name} 必须是整数，收到布尔值")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolParamError(f"参数 {name} 必须是整数，收到：{value!r}") from None


def _check_period(period: int) -> str | None:
    """校验区间参数，非法时返回错误信息"""
    if period not in VALID_PERIODS:
        return f"period 仅支持 {' / '.join(map(str, VALID_PERIODS))}，收到 {period}"
    return None


# ---------------- 各工具执行器（直接调用现有 service） ----------------

@_executor("get_fund_detail")
async def _tool_get_fund_detail(db: Session, args: dict) -> dict:
    err = _check_fund_code(args.get("fund_code"))
    if err:
        return {"error": err}
    detail = await fund_service.get_fund_detail(args["fund_code"])
    return detail.model_dump()


@_executor("get_fund_history")
async def _tool_get_fund_history(db: Session, args: dict) -> dict:
    err = _check_fund_code(args.get("fund_code"))
    if err:
        return {"error": err}
    page = _to_int(args.get("page", 1), "page")
    page_size = _to_int(args.get("page_size", 20), "page_size")
    if page < 1:
        return {"error": f"page 必须从 1 开始，收到 {page}"}
    if not (1 <= page_size <= 50):
        return {"error": f"page_size 必须在 1-50 之间，收到 {page_size}"}
    result = await fund_service.get_fund_history(args["fund_code"], page, page_size)
    return result.model_dump()


@_executor("get_fund_performance")
async def _tool_get_fund_performance(db: Session, args: dict) -> dict:
    err = _check_fund_code(args.get("fund_code"))
    if err:
        return {"error": err}
    period = _to_int(args.get("period", 30), "period")
    err = _check_period(period)
    if err:
        return {"error": err}
    result = await fund_service.get_fund_performance(args["fund_code"], period)
    return result.model_dump()


@_executor("get_holdings")
async def _tool_get_holdings(db: Session, args: dict) -> dict:
    holdings = await portfolio_service.list_holdings(db)
    return {
        "count": len(holdings),
        "holdings": [h.model_dump() for h in holdings],
    }


@_executor("get_portfolio_summary")
async def _tool_get_portfolio_summary(db: Session, args: dict) -> dict:
    summary = await portfolio_service.get_summary(db)
    return summary.model_dump()


@_executor("get_holding_history")
async def _tool_get_holding_history(db: Session, args: dict) -> dict:
    holding_id = _to_int(args.get("holding_id"), "holding_id")
    period = _to_int(args.get("period", 30), "period")
    err = _check_period(period)
    if err:
        return {"error": err}
    result = await portfolio_service.get_holding_simulated_history(
        db, holding_id, period
    )
    return result.model_dump()


@_executor("get_market_index")
async def _tool_get_market_index(db: Session, args: dict) -> dict:
    result = await market_service.get_market_indexes()
    wanted = str(args.get("index") or "").strip()
    if wanted:
        matched = [
            item for item in result["indexes"]
            if wanted in item["name"] or wanted in item["code"]
        ]
        if not matched:
            supported = " / ".join(item["name"] for item in market_service.SUPPORTED_INDEXES)
            return {"error": f"不支持的指数：{wanted}，支持查询：{supported}"}
        result["indexes"] = matched
    return result


# ---------------- 统一执行入口 ----------------

async def execute_tool(db: Session, name: str, arguments) -> dict:
    """执行一个工具调用，永远返回 JSON 可序列化的 dict（不抛异常）

    arguments 是模型给出的参数（JSON 字符串或已解析的 dict）。
    任何失败都转成 {"error": ...}：模型能据此自行纠正参数或换工具，
    Agent 循环不会因为单个工具失败而中断。
    """
    try:
        if isinstance(arguments, str):
            args = json.loads(arguments) if arguments.strip() else {}
        else:
            args = arguments
        if not isinstance(args, dict):
            return {"error": "工具参数必须是 JSON 对象"}

        executor = TOOL_EXECUTORS.get(name)
        if executor is None:
            return {"error": f"未知工具：{name}，可用工具：{list(TOOL_EXECUTORS)}"}

        return await executor(db, args)
    except json.JSONDecodeError:
        return {"error": "工具参数不是合法 JSON"}
    except ToolParamError as e:
        return {"error": str(e)}
    except (FundNotFoundError, HoldingNotFoundError) as e:
        return {"error": str(e)}
    except DataSourceUnavailableError as e:
        return {"error": f"基金数据源暂时不可用：{e}"}
    except Exception as e:  # 兜底：工具异常不打包给模型，也不打断 Agent 循环
        logger.error("Agent 工具 %s 执行异常: %s", name, type(e).__name__)
        return {"error": f"工具执行失败：{type(e).__name__}"}

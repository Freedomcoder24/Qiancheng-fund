"""
AI 分析服务（Phase 6）：调用 OpenAI 兼容 API 分析账户数据

职责边界（重要）：
1. 净值、收益、市值等所有数字全部由后端（calculator / portfolio_service）
   用 Decimal 精确计算后作为结构化数据提供给模型；
2. AI 只负责"解读和总结"，prompt 中明确要求禁止自行计算净值/收益；
3. AI 不做任何交易操作，输出仅供用户参考。

设计原则：
- 配置优先级（Phase 9）：Web 配置（web_ai_configs 表）优先，字段级回退 .env
  （AI_API_KEY / AI_BASE_URL / AI_MODEL）；API Key 绝不返回给前端，只给脱敏形式；
- 模型输出约定为严格 JSON，解析失败给出明确异常而不是静默出错；
- 使用 AsyncOpenAI，与项目其他 service 的 async 风格保持一致。
"""
import json
import logging
import os
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from openai import AsyncOpenAI

from app.models.ai import AIAnalysisResponse
from app.models.fund import AnalysisPeriod
from app.services import fund_service, goal_service, portfolio_service, web_config_service
from app.services.goal_service import GLOBAL_DISCLAIMER
from app.utils import calculator

logger = logging.getLogger(__name__)

# 分析持仓基金近期表现使用的区间（近 30 天，与前端默认选中区间一致）
ANALYSIS_PERFORMANCE_PERIOD = AnalysisPeriod.MONTH

# 模型调用超时（秒）：AI 生成较慢，给足时间但避免请求无限挂起
AI_TIMEOUT_SECONDS = 60.0
# 限制输出长度，控制 token 成本（约 800 tokens，足够三段分析）
AI_MAX_TOKENS = 1000


# ---------------- 自定义业务异常（路由层映射为不同 HTTP 状态码） ----------------

class AINotConfiguredError(Exception):
    """AI 未配置（.env 缺少 AI_API_KEY / AI_BASE_URL / AI_MODEL）"""


class NoHoldingsError(Exception):
    """账户没有任何持仓，无法分析"""


class AIUpstreamError(Exception):
    """AI 上游服务调用失败（网络 / 鉴权 / 限流等）"""


class AIResponseParseError(Exception):
    """模型返回内容无法解析为约定的 JSON 结构"""


# ---------------- 配置状态 ----------------

def get_ai_status(db=None) -> dict:
    """检查当前生效的 AI 配置状态（Web 配置优先，回退 .env）。

    只返回是否已配置、模型名、Base URL 与脱敏 Key，绝不返回 API Key 本身。
    """
    cfg = web_config_service.get_effective_ai_config(db)
    api_key = cfg["api_key"]
    configured = bool(api_key and cfg["base_url"] and cfg["model"])

    return {
        "configured": configured,
        "model": cfg["model"],
        "base_url": cfg["base_url"],
        "api_key_masked": web_config_service.mask_api_key(api_key) if api_key else "",
        "key_source": cfg["key_source"],
        "secret_key_ready": web_config_service.secret_key_ready(),
    }


# ---------------- 组装分析上下文（数据全部来自后端计算） ----------------

async def gather_analysis_context(db) -> dict:
    """汇总发送给模型的结构化数据

    组成：
    - account_summary：账户汇总（总投入 / 市值 / 收益 / 今日估算）
    - holdings：每条持仓的完整收益数据 + 市值占比（后端 Decimal 计算）
    - performance_30d：每只持仓基金近 30 天区间收益与最大回撤
    """
    summary = await portfolio_service.get_summary(db)
    if summary.holding_count == 0:
        raise NoHoldingsError("暂无持仓数据，请先添加持仓再进行 AI 分析")

    holdings = await portfolio_service.list_holdings(db)

    # 市值占比：单只持仓市值 ÷ 总市值（后端算好，AI 只引用不计算）
    total_mv = Decimal(str(summary.total_market_value))
    holding_items = []
    for h in holdings:
        weight = Decimal("0")
        if total_mv > 0:
            weight = (
                (calculator.to_decimal(h.market_value) / total_mv * 100)
                .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
        holding_items.append({
            "fund_code": h.fund_code,
            "fund_name": h.fund_name,
            "market_value": h.market_value,
            "market_weight_percent": float(weight),
            "invested_amount": h.invested_amount,
            "profit": h.profit,
            "profit_rate": h.profit_rate,
            "latest_nav": h.latest_nav,
            "latest_nav_date": h.latest_nav_date,
            "nav_daily_change_percent": h.nav_daily_change,
            "latest_nav_change": h.latest_nav_change,
        })

    # 每只持仓基金近 30 天表现（区间收益 + 最大回撤）
    performance_items = []
    for h in holdings:
        perf = await fund_service.get_fund_performance(
            h.fund_code, ANALYSIS_PERFORMANCE_PERIOD
        )
        performance_items.append({
            "fund_code": perf.fund_code,
            "period_days": perf.period,
            "period_return_percent": perf.period_return,
            "max_drawdown_percent": perf.max_drawdown,
        })

    # 最新已确认净值日期（取所有持仓中最新的日期）
    dates = [h.latest_nav_date for h in holdings if h.latest_nav_date]
    data_date = max(dates) if dates else None

    return {
        "data_date": data_date,
        "account_summary": {
            "holding_count": summary.holding_count,
            "total_invested": summary.total_invested,
            "total_market_value": summary.total_market_value,
            "total_profit": summary.total_profit,
            "total_profit_rate": summary.total_profit_rate,
            "latest_nav_change": summary.latest_nav_change,
        },
        "holdings": holding_items,
        "performance_30d": performance_items,
    }


# ---------------- Prompt 构造 ----------------

# 候选池解读红线：禁止根据历史数据推断未来收益的预测性表述（Phase 12）。
# 解析层强制检查：出现即拒绝该条 AI 输出，历史数据只用于描述和解释。
FORBIDDEN_PREDICTIVE_PHRASES = (
    "有望上涨", "预计会继续上涨", "预计上涨", "预计会上涨",
    "盈利概率", "未来表现较好", "未来表现会", "未来收益可期",
    "值得买", "推荐购买", "建议买入", "建议卖出", "建议加仓", "建议减仓",
    "必涨", "一定盈利", "值得买入", "值得关注买入",
)


def _contains_forbidden_phrase(text: str) -> str | None:
    """检查文本是否命中预测性/推荐性红线措辞，命中返回命中的短语"""
    if not text:
        return None
    for phrase in FORBIDDEN_PREDICTIVE_PHRASES:
        if phrase in text:
            return phrase
    return None


SYSTEM_PROMPT = """你是个人基金账户分析助手。用户会提供一份由后端系统精确计算好的账户结构化数据。

你必须遵守以下规则：
1. 所有数字（净值、收益、市值、涨跌幅）只能来自提供的数据，禁止自行计算或编造任何数字；引用时必须与数据完全一致。
2. 你只做信息解读与风险提示，禁止给出"买入 / 卖出 / 加仓 / 减仓"等任何具体交易指令。
3. 不确定的信息不要猜测，明确说明数据中未提供。
4. 输出必须是严格的 JSON 对象，不要输出 markdown 代码块或其他任何文字，字段如下：
   {
     "today_summary": "今日持仓总结（基于最新净值和今日收益估算，150 字以内）",
     "profit_sources": "收益来源分析（哪些持仓贡献了主要收益或亏损，150 字以内）",
     "risk_warnings": ["风险提示1", "风险提示2"]
   }
   risk_warnings 为字符串数组，0 到 5 条，内容基于仓位集中度、亏损持仓、回撤等数据。"""


def build_analysis_prompt(context: dict) -> str:
    """把后端计算好的结构化数据转成发送给模型的用户消息"""
    data_json = json.dumps(context, ensure_ascii=False, indent=2)
    return (
        "以下是账户的结构化数据（所有金额单位为元，收益率单位为%，"
        "data_date 为最新已确认净值日期，非实时数据）。"
        "latest_nav_change 是基于最新两个交易日净值变化估算的今日收益。\n\n"
        f"{data_json}\n\n"
        "请按系统指令的 JSON 格式输出分析结果。"
    )


# ---------------- Phase 12：目标结论 / 候选池解读的 System Prompt ----------------

# 目标结论枚举（用户确认的三种状态；AI 不得输出交易指令）
GOAL_CONCLUSION_ENUMS = ("near_target", "target_achieved", "risk_attention")
GOAL_CONCLUSION_LABELS = {
    "near_target": "接近目标",
    "target_achieved": "已达到目标",
    "risk_attention": "风险需要关注",
}

GOAL_SYSTEM_PROMPT = """你是个人基金投资目标分析助手。用户已设定一个目标收益率，
系统会提供由后端精确计算好的目标进度结构化数据（当前收益、距离目标、
历史表现、最大回撤、波动天数等）。

你必须遵守以下规则：
1. 所有数字只能引用提供的结构化数据，禁止自行计算或编造任何数字；引用时必须与数据完全一致。
2. conclusion 字段只能输出以下三个枚举之一（禁止输出其他任何值）：
   - near_target：接近目标（尚未达标但差距较小）
   - target_achieved：已达到目标（仅描述状态，绝不等于卖出或止盈建议）
   - risk_attention：风险需要关注（回撤 / 波动 / 收益明显落后等情况）
3. 你只解释数据与风险因素，绝不替用户做交易决定：全文禁止出现
   "应该买 / 应该卖 / 建议买入 / 建议卖出 / 建议止盈 / 加仓 / 减仓 / 赎回"
   等任何交易指令，也不要暗示用户应该采取某种交易行动。
4. 达到目标只说明"已达到目标"这一状态及其数据依据，不得生成卖出结论。
5. 不确定的信息不要猜测，明确说明数据中未提供。
6. 输出必须是严格的 JSON 对象，不要输出 markdown 代码块或其他任何文字，字段如下：
   {
     "conclusion": "near_target | target_achieved | risk_attention 之一",
     "reason": "结论依据（解释当前收益、距离目标、历史表现等数据，200 字以内）",
     "risks": ["风险因素1", "风险因素2"]
   }
   risks 为字符串数组，0 到 5 条，只能基于提供的回撤 / 波动 / 收益数据。"""

CANDIDATE_SYSTEM_PROMPT = """你是基金候选池解读助手。系统会提供一份由后端
基于公开历史数据量化筛选出的候选基金列表（仅表示符合当前筛选条件）。

你必须遵守以下规则：
1. 所有数字只能引用提供的结构化数据，禁止自行计算或编造任何数字。
2. 候选池不是推荐：全文禁止出现"推荐购买 / 值得买 / 值得关注买入 / 最优 /
   必涨 / 一定盈利"等表述，也不得预测未来收益；只能解释每只基金
   "为什么符合当前量化筛选条件"（例如历史区间收益、回撤、波动等数据特征）。
3. 红线：禁止根据历史数据推断或暗示未来上涨、盈利概率或收益结果。
   禁止出现"后续有望上涨 / 预计会继续上涨 / 盈利概率较高 / 未来表现较好"
   等预测性表述；历史数据只用于描述和解释（例如"近一年历史表现较高 /
   符合当前量化筛选条件 / 近期历史波动较大 / 历史回撤较明显"），
   不用于预测未来收益。
4. 不给出任何买入 / 卖出 / 加仓 / 减仓等交易指令。
5. market_background 只能做定性描述（宏观 / 行业背景），并注明这是来自
   你（模型）的公开知识、非实时信息、可能过时；禁止编造具体新闻、日期或数字。
6. 不确定的信息不要猜测，明确说明数据中未提供。
7. 输出必须是严格的 JSON 对象，不要输出 markdown 代码块或其他任何文字，字段如下：
   {
     "overview": "候选池整体说明（基于筛选规则与列表数据的总体特征，150 字以内）",
     "highlights": [
       {"code": "基金代码（必须来自提供的列表）",
        "reason": "为什么符合当前筛选条件（只引用该基金提供的数据，80 字以内）"}
     ],
     "cautions": ["共同风险提示1", "共同风险提示2"],
     "market_background": "宏观 / 行业定性背景（注明非实时，100 字以内）"
   }
   highlights 为数组（1 到 6 条，选择列表中数据特征较有代表性的若干只），
   cautions 为字符串数组（0 到 4 条，基于回撤 / 波动数据）。"""


# ---------------- 模型调用与解析 ----------------

async def _call_model(
    user_prompt: str,
    config: dict | None = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> str:
    """调用 OpenAI 兼容 API，返回模型文本输出

    config 为生效配置（Web 优先）；不传时按调用时解析（配置可能在运行期被修改），
    保持只传 user_prompt 的旧调用方式兼容。system_prompt 默认为账户分析提示词，
    Phase 12 的目标结论 / 候选池解读传入各自的专属提示词。
    API Key 只在服务端内存中使用，不会进入日志或响应
    （异常信息会先抹去 Key 再输出）。
    """
    if config is None:
        config = web_config_service.get_effective_ai_config()
    api_key = config["api_key"]
    base_url = config["base_url"]
    model = config["model"]

    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=AI_TIMEOUT_SECONDS)
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=AI_MAX_TOKENS,
            temperature=0.3,  # 低温度：数字分析要求稳定、贴近给定数据
        )
    except Exception as e:  # openai 的各类异常统一转为业务异常
        logger.error("AI 上游调用失败: %s", type(e).__name__)
        # 上游错误文本可能回显请求信息，先抹去 Key 再对外
        detail = str(e).replace(api_key, "***") if api_key else str(e)
        raise AIUpstreamError(f"AI 服务调用失败：{detail}") from e

    content = resp.choices[0].message.content if resp.choices else None
    if not content or not content.strip():
        raise AIUpstreamError("AI 服务返回了空内容")
    return content


# ---------------- 测试连接（最小化请求，验证配置可用） ----------------

TEST_CONNECTION_TIMEOUT_SECONDS = 15.0
TEST_CONNECTION_MAX_TOKENS = 10  # 最小化请求：只要求回复一两个词，消耗极少 Token


async def test_connection(api_key: str, base_url: str, model: str) -> dict:
    """用最小化 AI 请求验证配置是否可用（"测试连接"按钮）

    发送一条只要求回复 OK 的短消息，max_tokens=10，尽量少消耗 Token。
    返回 {ok, message, model}；错误信息会先抹去 API Key 再返回。
    """
    client = AsyncOpenAI(
        api_key=api_key, base_url=base_url, timeout=TEST_CONNECTION_TIMEOUT_SECONDS
    )
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "连接测试：请只回复 OK"}],
            max_tokens=TEST_CONNECTION_MAX_TOKENS,
        )
    except Exception as e:  # noqa: BLE001 任何失败都转成可展示的提示，不抛 500
        logger.error("AI 测试连接失败: %s", type(e).__name__)
        detail = str(e).replace(api_key, "***") if api_key else str(e)
        return {"ok": False, "message": f"连接失败：{detail}", "model": model}

    content = resp.choices[0].message.content if resp.choices else None
    if not content or not content.strip():
        return {"ok": False, "message": "连接成功但模型返回了空内容", "model": model}
    return {
        "ok": True,
        "message": f"连接成功，模型 {model} 已正常响应（本次测试消耗了少量 Token）",
        "model": model,
    }


def _extract_json_dict(text: str) -> dict:
    """从模型输出文本中提取 JSON 对象（共享剥壳逻辑）

    容错：部分模型会额外包裹 ```json ... ``` 代码块，先剥壳再解析；
    仍失败时提取首个 { 到最后一个 } 之间的内容尝试一次，
    失败抛 AIResponseParseError。
    """
    cleaned = text.strip()

    # 剥掉 markdown 代码块外壳
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # 再尝试提取大括号之间的 JSON 片段
        brace_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if brace_match:
            try:
                data = json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass

    if not isinstance(data, dict):
        raise AIResponseParseError("AI 返回的内容无法解析为约定格式，请重试")
    return data


def _parse_ai_json(text: str) -> dict:
    """解析账户分析的模型输出为约定的 JSON 结构（缺失字段给安全默认值）"""
    data = _extract_json_dict(text)

    today_summary = data.get("today_summary")
    profit_sources = data.get("profit_sources")
    warnings = data.get("risk_warnings")

    return {
        "today_summary": str(today_summary).strip() if today_summary else "",
        "profit_sources": str(profit_sources).strip() if profit_sources else "",
        "risk_warnings": [
            str(w).strip() for w in warnings if str(w).strip()
        ] if isinstance(warnings, list) else [],
    }


def _parse_goal_json(text: str) -> dict:
    """解析目标结论的模型输出

    conclusion 枚举非法时保守回退 risk_attention（风险需要关注），
    并附低置信标记，绝不把未知结论当作正常状态展示。
    """
    data = _extract_json_dict(text)

    conclusion = str(data.get("conclusion") or "").strip()
    low_confidence = False
    if conclusion not in GOAL_CONCLUSION_ENUMS:
        conclusion = "risk_attention"  # 保守缺省：未知输出按风险关注处理
        low_confidence = True

    reason = str(data.get("reason") or "").strip()
    risks_raw = data.get("risks")

    return {
        "conclusion": conclusion,
        "conclusion_label": GOAL_CONCLUSION_LABELS[conclusion],
        "reason": reason,
        "risks": [
            str(w).strip() for w in risks_raw if str(w).strip()
        ] if isinstance(risks_raw, list) else [],
        "low_confidence": low_confidence,
    }


def _parse_candidate_json(text: str, valid_codes: set[str]) -> dict:
    """解析候选池解读的模型输出

    安全过滤（Phase 12 红线）：
    - highlights 里代码不在候选池内的条目直接丢弃（防止模型编造基金）；
    - overview / highlights / cautions / market_background 命中预测性或
      推荐性红线措辞时：整体字段（overview / market_background）命中 →
      拒绝整次输出；单条（highlight reason / caution）命中 → 只丢弃该条。
    """
    data = _extract_json_dict(text)

    overview = str(data.get("overview") or "").strip()
    hit = _contains_forbidden_phrase(overview)
    if hit:
        raise AIResponseParseError(f"AI 输出包含预测性/推荐性表述（{hit}），已拒绝本次解读")

    highlights_raw = data.get("highlights")
    highlights: list[dict] = []
    if isinstance(highlights_raw, list):
        for h in highlights_raw:
            if not isinstance(h, dict):
                continue
            code = str(h.get("code") or "").strip()
            reason = str(h.get("reason") or "").strip()
            if code in valid_codes and reason and not _contains_forbidden_phrase(reason):
                highlights.append({"code": code, "reason": reason})

    cautions_raw = data.get("cautions")
    cautions = [
        str(w).strip() for w in cautions_raw
        if str(w).strip() and not _contains_forbidden_phrase(str(w))
    ] if isinstance(cautions_raw, list) else []

    background = str(data.get("market_background") or "").strip()
    hit = _contains_forbidden_phrase(background)
    if hit:
        raise AIResponseParseError(f"AI 输出包含预测性/推荐性表述（{hit}），已拒绝本次解读")

    return {
        "overview": overview,
        "highlights": highlights,
        "cautions": cautions,
        "market_background": background,
    }


# ---------------- 主流程 ----------------

async def run_analysis(db) -> AIAnalysisResponse:
    """AI 分析主流程：检查配置 → 组装数据 → 调用模型 → 解析返回

    异常与 HTTP 状态码的映射在路由层完成：
    AINotConfiguredError → 503，NoHoldingsError → 400，
    AIUpstreamError / AIResponseParseError → 502，数据源异常 → 503。
    """
    status = get_ai_status(db)
    if not status["configured"]:
        raise AINotConfiguredError(
            "AI 功能未配置：请在 Dashboard 的「AI API 配置」面板填写并保存，"
            "或在 .env 中配置 AI_API_KEY、AI_BASE_URL、AI_MODEL 后重启服务"
        )

    context = await gather_analysis_context(db)
    prompt = build_analysis_prompt(context)
    logger.info("AI 分析开始: %d 条持仓，模型 %s",
                len(context["holdings"]), status["model"])

    content = await _call_model(prompt)
    parsed = _parse_ai_json(content)

    logger.info("AI 分析完成")
    return AIAnalysisResponse(
        model=status["model"],
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        data_date=context["data_date"],
        today_summary=parsed["today_summary"],
        profit_sources=parsed["profit_sources"],
        risk_warnings=parsed["risk_warnings"],
    )


# ---------------- Phase 12：目标结论 / 候选池解读 ----------------

async def run_goal_conclusion(db) -> dict:
    """AI 目标结论：解读后端已算好的目标进度数据，只解释不决策

    前提：用户已设置目标（未设置抛 GoalNotSetError → 路由层 400）；
    AI 输出的 conclusion 只允许 near_target / target_achieved /
    risk_attention 三个枚举之一，非法值保守回退 risk_attention；
    AI 只解释数据与风险因素，绝不产生交易指令。
    """
    status = get_ai_status(db)
    if not status["configured"]:
        raise AINotConfiguredError(
            "AI 功能未配置：请在 Dashboard 的「AI API 配置」面板填写并保存，"
            "或在 .env 中配置 AI_API_KEY、AI_BASE_URL、AI_MODEL 后重启服务"
        )

    analysis = await goal_service.build_goal_analysis(db)
    if not analysis["target_set"]:
        raise goal_service.GoalNotSetError(
            "尚未设置投资目标：请先在「我的投资目标」面板设置目标收益率"
        )

    prompt = (
        "以下是用户设定目标后的目标进度结构化数据（后端精确计算，"
        "金额单位为元，收益率单位为%，data_date 为最新已确认净值日期）。\n"
        "请只解读数据与风险因素，conclusion 只能输出约定枚举之一，"
        "禁止任何交易指令，达到目标也只是状态描述。\n\n"
        f"{json.dumps(analysis, ensure_ascii=False, indent=2)}\n\n"
        "请按系统指令的 JSON 格式输出目标结论。"
    )

    logger.info("AI 目标结论开始: 模型 %s", status["model"])
    content = await _call_model(prompt, system_prompt=GOAL_SYSTEM_PROMPT)
    parsed = _parse_goal_json(content)
    logger.info("AI 目标结论完成: %s", parsed["conclusion"])

    return {
        "model": status["model"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_date": analysis["data_date"],
        "conclusion": parsed["conclusion"],
        "conclusion_label": parsed["conclusion_label"],
        "reason": parsed["reason"],
        "risks": parsed["risks"],
        "low_confidence": parsed["low_confidence"],
        "disclaimer": GLOBAL_DISCLAIMER,
    }


async def run_candidate_analysis(candidates: dict, db=None) -> dict:
    """AI 候选池解读：解释列表中基金为什么符合当前量化筛选条件

    输入为 candidate_service.get_candidates() 的返回结构（后端量化数据）。
    AI 只解释入选原因与共同风险，禁止推荐购买 / 预测收益；
    highlights 中代码不在候选池内的条目会被丢弃（防编造）。
    db 必须由路由层传入（Web 配置优先于 .env）；
    缺省 None 时会自建会话读真实库，仅供无请求上下文的场景使用。
    """
    status = get_ai_status(db)
    if not status["configured"]:
        raise AINotConfiguredError(
            "AI 功能未配置：请在 Dashboard 的「AI API 配置」面板填写并保存，"
            "或在 .env 中配置 AI_API_KEY、AI_BASE_URL、AI_MODEL 后重启服务"
        )

    valid_codes = {c["code"] for c in candidates["candidates"]}
    # 发给模型的精简结构（只送必要字段，控制 token）
    payload = {
        "screening_rule": candidates["screening_rule"],
        "nav_date": candidates["nav_date"],
        "candidates": candidates["candidates"],
        "data_issues": candidates["data_issues"],
    }
    prompt = (
        "以下是后端基于公开历史数据量化筛选出的候选基金列表结构化数据"
        "（收益率单位为%，净值日期见 nav_date，非实时数据）。\n"
        "候选池不是推荐：请只解释每只基金为什么符合当前筛选条件，"
        "禁止出现推荐购买 / 值得买 / 最优等表述，禁止预测未来收益，"
        "禁止任何交易指令；market_background 需注明非实时。\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请按系统指令的 JSON 格式输出解读结果。"
    )

    logger.info("AI 候选池解读开始: 候选 %d 只, 模型 %s",
                len(valid_codes), status["model"])
    content = await _call_model(prompt, system_prompt=CANDIDATE_SYSTEM_PROMPT)
    parsed = _parse_candidate_json(content, valid_codes)
    logger.info("AI 候选池解读完成: highlights %d 条", len(parsed["highlights"]))

    return {
        "model": status["model"],
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "nav_date": candidates["nav_date"],
        "overview": parsed["overview"],
        "highlights": parsed["highlights"],
        "cautions": parsed["cautions"],
        "market_background": parsed["market_background"],
        "disclaimer": GLOBAL_DISCLAIMER,
    }

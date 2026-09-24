"""AI 分析相关的 API 路由（Phase 6 正式实现 GPT 分析功能；Phase 9 增加配置管理；
Phase 12 增加目标结论 / 候选池解读）"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database.database import get_db
from app.models.ai import (
    AIAnalysisResponse,
    AIConfigTestIn,
    AIConfigTestResult,
    CandidateAnalysisResponse,
    GoalConclusionResponse,
    WebAIConfigIn,
)
from app.services import ai_service, candidate_service, goal_service, web_config_service
from app.services.fund_data_source import (
    DataSourceUnavailableError,
    FundNotFoundError,
)

router = APIRouter(prefix="/api/ai", tags=["AI 分析"])


@router.get("/status", summary="获取 AI 配置状态")
def get_status(db: Session = Depends(get_db)):
    """检查当前生效的 AI 配置（Web 配置优先，回退 .env）。

    只返回"是否已配置"和脱敏 Key，不会返回 API Key 本身，保证安全。
    """
    return ai_service.get_ai_status(db)


# ---------------- Web AI API 配置（Phase 9） ----------------


@router.get("/config", summary="获取 Web AI 配置状态")
def get_config(db: Session = Depends(get_db)):
    """返回生效配置状态：是否已配置、模型、Base URL、脱敏 Key、
    Key 来源（web / env）、加密密钥是否就绪。

    绝不返回完整 API Key（响应中只有前 3 位 + **** + 后 4 位）。
    """
    return ai_service.get_ai_status(db)


@router.put("/config", summary="保存 Web AI 配置（Key 加密存储）")
def save_config(body: WebAIConfigIn, db: Session = Depends(get_db)):
    """保存 Web 配置（优先于 .env）。API Key 用 FUND_PILOT_SECRET_KEY
    派生的密钥加密后存入 SQLite，数据库 / 日志 / 响应中均无明文。

    - 未配置 FUND_PILOT_SECRET_KEY → 400（明确提示，禁止保存）
    """
    try:
        web_config_service.save_web_config(
            db, body.api_key.strip(), body.base_url, body.model
        )
    except web_config_service.SecretKeyMissingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return ai_service.get_ai_status(db)


@router.delete("/config", summary="清除 Web AI 配置（回退 .env）")
def clear_config(db: Session = Depends(get_db)):
    """删除 Web 配置行，之后所有 AI 功能自动回退使用 .env 配置。"""
    web_config_service.delete_web_config(db)
    return ai_service.get_ai_status(db)


@router.post("/config/test", response_model=AIConfigTestResult, summary="测试 AI 连接")
async def test_config(body: AIConfigTestIn, db: Session = Depends(get_db)):
    """用最小化 AI 请求（max_tokens=10，回复一个词）验证配置可用性，
    会消耗少量 Token。请求体留空的字段使用当前生效配置。
    """
    effective = ai_service.get_ai_status(db)
    api_key = body.api_key.strip() or (
        web_config_service.get_effective_ai_config(db)["api_key"]
    )
    base_url = body.base_url.strip() or effective["base_url"]
    model = body.model.strip() or effective["model"]

    if not (api_key and base_url and model):
        raise HTTPException(
            status_code=400,
            detail="配置不完整：请填写 API Key、Base URL 和模型名后再测试连接",
        )
    return await ai_service.test_connection(api_key, base_url, model)


@router.post(
    "/analysis",
    response_model=AIAnalysisResponse,
    summary="生成 AI 账户分析",
)
async def create_analysis(db: Session = Depends(get_db)):
    """将后端已计算好的账户汇总、持仓收益、近期基金表现发送给模型，
    返回今日持仓总结 / 收益来源 / 风险提示三部分内容。

    AI 只解读后端提供的数据，不自行计算净值或收益，也不产生任何交易指令。

    - 未配置 AI（.env 缺失）→ 503
    - 账户无持仓 → 400
    - AI 上游调用失败 / 返回格式无法解析 → 502
    - 组装数据时基金数据源不可用 → 503
    """
    try:
        return await ai_service.run_analysis(db)
    except ai_service.AINotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except ai_service.NoHoldingsError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except (ai_service.AIUpstreamError, ai_service.AIResponseParseError) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except (DataSourceUnavailableError, FundNotFoundError) as e:
        # 组装上下文需要读取净值数据（持仓收益 / 近期表现）
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post(
    "/goal-conclusion",
    response_model=GoalConclusionResponse,
    summary="生成 AI 目标结论（只解释数据与风险，不做交易决定）",
)
async def create_goal_conclusion(db: Session = Depends(get_db)):
    """基于后端已算好的目标进度数据生成目标结论。

    conclusion 只输出 near_target / target_achieved / risk_attention 之一；
    AI 只解释数据与风险因素，不产生任何买入 / 卖出 / 止盈指令，
    达到目标只描述状态，不生成卖出结论。

    - 未设置投资目标 → 400（先去「我的投资目标」设置）
    - 未配置 AI → 503
    - AI 上游调用失败 / 返回格式无法解析 → 502
    - 组装数据时基金数据源不可用 → 503
    """
    try:
        return await ai_service.run_goal_conclusion(db)
    except goal_service.GoalNotSetError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ai_service.AINotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except (ai_service.AIUpstreamError, ai_service.AIResponseParseError) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except (DataSourceUnavailableError, FundNotFoundError) as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post(
    "/candidate-analysis",
    response_model=CandidateAnalysisResponse,
    summary="生成 AI 候选池解读（解释入选原因，不是推荐）",
)
async def create_candidate_analysis(db: Session = Depends(get_db)):
    """对当前量化候选池生成 AI 解读：只解释每只基金为什么符合筛选条件，
    禁止「推荐购买 / 值得买 / 最优」等表述，不预测未来收益。

    候选池实时重新筛选（排行 30 分钟缓存 + 历史净值 5 分钟缓存）。
    db 注入当前请求会话，AI 配置按 Web 配置优先于 .env 解析。

    - 未配置 AI → 503
    - AI 上游调用失败 / 返回格式无法解析 → 502
    - 排行 / 历史净值数据源不可用 → 503
    """
    try:
        candidates = await candidate_service.get_candidates(20, db)
        return await ai_service.run_candidate_analysis(candidates, db)
    except ai_service.AINotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except (ai_service.AIUpstreamError, ai_service.AIResponseParseError) as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except (DataSourceUnavailableError, FundNotFoundError) as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

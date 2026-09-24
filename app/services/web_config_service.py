"""Web AI API 配置服务（Phase 9）：加密存取 + 配置优先级解析

职责边界（重要）：
1. Web 配置保存在 SQLite 的 web_ai_configs 表（单行 id=1），服务重启后自动恢复；
2. API Key 加密存储：加密密钥由环境变量 FUND_PILOT_SECRET_KEY 派生
   （sha256 → Fernet key），该变量不配置时禁止保存，绝不硬编码密钥；
3. 数据库 / 日志 / API 响应 / 前端中 Key 一律不出现明文：对外只提供
   "脱敏 Key"（前 3 位 + **** + 后 4 位）；
4. 配置优先级：Web 配置（web_ai_configs 行存在）优先于 .env；字段级回退——
   Web 行中某字段为空时该字段回落到 .env；清除 Web 配置 = 删除该行。
"""
import base64
import hashlib
import logging
import os
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from app.database.database import SessionLocal
from app.database.models import WebAiConfig

logger = logging.getLogger(__name__)

# 加密密钥来源环境变量（禁止硬编码密钥本身）
SECRET_KEY_ENV = "FUND_PILOT_SECRET_KEY"


class SecretKeyMissingError(Exception):
    """未配置 FUND_PILOT_SECRET_KEY，无法加密保存 API Key"""


# ---------------- 加密密钥与加解密 ----------------

def secret_key_ready() -> bool:
    """是否已配置本机加密密钥环境变量"""
    return bool(os.getenv(SECRET_KEY_ENV, "").strip())


def _fernet() -> Fernet:
    """由 FUND_PILOT_SECRET_KEY 派生 Fernet 密钥

    secret 原文不做存储；sha256 摘要 base64 后即为 Fernet 要求的 32 字节 key。
    """
    secret = os.getenv(SECRET_KEY_ENV, "").strip()
    if not secret:
        raise SecretKeyMissingError(
            f"未配置环境变量 {SECRET_KEY_ENV}，无法加密保存 API Key；"
            f"请先在 .env 或系统环境变量中设置 {SECRET_KEY_ENV} 后重启服务"
        )
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_api_key(api_key: str) -> str:
    """加密 API Key（调用前需确保 secret key 已配置）"""
    return _fernet().encrypt(api_key.encode("utf-8")).decode("utf-8")


def decrypt_api_key(token: str) -> str | None:
    """解密 API Key；密钥不匹配或数据损坏时返回 None（不让异常逃逸）"""
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        # 常见于：保存后更换了 FUND_PILOT_SECRET_KEY
        logger.error("Web API Key 解密失败（加密密钥可能已变更），该配置将被忽略")
        return None


def mask_api_key(api_key: str) -> str:
    """脱敏：保留前 3 位和后 4 位，中间用 **** 代替（过短时全部打码）"""
    key = api_key.strip()
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:3]}****{key[-4:]}"


# ---------------- Web 配置行的存取 ----------------

def get_web_config(db: Session) -> WebAiConfig | None:
    """读取 Web 配置行（单行，取 id 最小的一条）"""
    return db.query(WebAiConfig).order_by(WebAiConfig.id.asc()).first()


def save_web_config(db: Session, api_key: str, base_url: str, model: str) -> WebAiConfig:
    """保存 / 覆盖 Web 配置（upsert 单行）

    - api_key 必填并加密存储；base_url / model 可为空串（该字段回落 .env）；
    - 未配置 FUND_PILOT_SECRET_KEY 时抛 SecretKeyMissingError（路由层转 400）。
    """
    row = get_web_config(db)
    if row is None:
        row = WebAiConfig(id=1)
        db.add(row)
    row.encrypted_api_key = encrypt_api_key(api_key)
    row.base_url = base_url.strip()
    row.model = model.strip()
    row.updated_at = datetime.now()
    db.commit()
    logger.info("Web AI 配置已保存（Key 已加密存储，页面仅显示脱敏形式）")
    return row


def delete_web_config(db: Session) -> bool:
    """清除 Web 配置（删除行，之后自动回退 .env）；返回是否存在过"""
    row = get_web_config(db)
    if row is None:
        return False
    db.delete(row)
    db.commit()
    logger.info("Web AI 配置已清除，回退使用 .env 配置")
    return True


# ---------------- 配置优先级解析（Web 优先，字段级回退 .env） ----------------

def get_effective_ai_config(db: Session | None = None) -> dict:
    """解析当前生效的 AI 配置

    返回 {api_key, base_url, model, key_source}：
    - key_source："web"（Key 来自 Web 配置）/ "env"（来自 .env）/ ""（都未配置）；
    - Web 行存在时逐字段优先，字段为空或 Key 解密失败时回落 .env 对应字段。
    """
    own_session = db is None
    if own_session:  # 供无请求上下文的调用方（如 _call_model）使用
        db = SessionLocal()
    try:
        row = get_web_config(db)
        web_key = decrypt_api_key(row.encrypted_api_key) if row and row.encrypted_api_key else None

        env_key = os.getenv("AI_API_KEY", "").strip()
        env_base = os.getenv("AI_BASE_URL", "").strip()
        env_model = os.getenv("AI_MODEL", "").strip()

        web_base = (row.base_url.strip() if row else "")
        web_model = (row.model.strip() if row else "")

        if web_key:
            key_source = "web"
        elif env_key:
            key_source = "env"
        else:
            key_source = ""

        return {
            "api_key": web_key or env_key,
            "base_url": web_base or env_base,
            "model": web_model or env_model,
            "key_source": key_source,
        }
    finally:
        if own_session:
            db.close()

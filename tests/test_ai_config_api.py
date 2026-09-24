"""Phase 9 Web AI API 配置测试（内存 SQLite + mock 模型调用，全 mock 不依赖网络）

覆盖点：
- 未配置 FUND_PILOT_SECRET_KEY 时禁止保存 Web API Key（400 明确提示）
- Key 加密存储：数据库无明文、可解密还原、API 响应只含脱敏形式（Key 绝不泄露）
- 配置优先级：Web 配置优先于 .env，字段级回退；清除 Web 配置后恢复 .env
- 持久化 / 重启恢复：保存后用全新会话 + 重新构造的密钥解密成功
- 测试连接：最小化请求 mock 成功 / 失败（失败信息抹去 Key）
- Phase 8 优化：启动后首轮检查短延迟即执行，不必等完整间隔
"""
import asyncio
import base64
import hashlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cryptography.fernet import Fernet

from app.database.database import Base, get_db
from app.database.models import WebAiConfig
from app.main import app
from app.services import ai_service, automation_service, web_config_service

# ---------------- 内存数据库 ----------------

test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestSession = sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


# ---------------- 测试数据 ----------------

SECRET = "unit-test-secret"
RAW_KEY = "sk-unit-test-abcdef1234567890"
ENV_AI = {
    "AI_API_KEY": "sk-env-key-999888777",
    "AI_BASE_URL": "https://env.test/v1",
    "AI_MODEL": "env-model",
}


def _make_client(monkeypatch):
    Base.metadata.create_all(bind=test_engine)
    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture()
def client_no_secret(monkeypatch):
    """未配置 FUND_PILOT_SECRET_KEY 场景（指令 7）"""
    monkeypatch.delenv("FUND_PILOT_SECRET_KEY", raising=False)
    yield from _make_client(monkeypatch)


@pytest.fixture()
def client(monkeypatch):
    """已配置加密密钥场景"""
    monkeypatch.setenv("FUND_PILOT_SECRET_KEY", SECRET)
    yield from _make_client(monkeypatch)


def save_config(client: TestClient, **overrides) -> dict:
    body = {
        "api_key": RAW_KEY,
        "base_url": "https://web.test/v1",
        "model": "web-model",
        **overrides,
    }
    resp = client.put("/api/ai/config", json=body)
    assert resp.status_code == 200
    return resp.json()


# ---------------- 保存与加密存储（指令 5 / 6 / 7 / 8） ----------------


def test_save_without_secret_key_rejected(client_no_secret):
    """测试 1：未配置 FUND_PILOT_SECRET_KEY → 400 明确提示，不落库"""
    resp = client_no_secret.put(
        "/api/ai/config",
        json={"api_key": RAW_KEY, "base_url": "https://x/v1", "model": "m"},
    )
    assert resp.status_code == 400
    assert "FUND_PILOT_SECRET_KEY" in resp.json()["detail"]
    assert RAW_KEY not in resp.text

    db = TestSession()
    try:
        assert db.query(WebAiConfig).count() == 0
    finally:
        db.close()


def test_save_encrypts_key_and_masks_response(client):
    """测试 2：保存后数据库无明文 Key、可解密还原，响应只含脱敏 Key"""
    status = save_config(client)

    assert status["configured"] is True
    assert status["key_source"] == "web"
    assert status["secret_key_ready"] is True
    # 响应任何位置都不出现完整 Key，只允许脱敏形式
    assert RAW_KEY not in str(status)
    assert status["api_key_masked"] == web_config_service.mask_api_key(RAW_KEY)
    assert "****" in status["api_key_masked"]

    db = TestSession()
    try:
        row = db.query(WebAiConfig).first()
        assert row is not None
        assert row.encrypted_api_key != RAW_KEY          # 数据库无明文
        assert RAW_KEY not in row.encrypted_api_key
        assert web_config_service.decrypt_api_key(row.encrypted_api_key) == RAW_KEY
        assert row.base_url == "https://web.test/v1"     # 非敏感字段明文保存
        assert row.model == "web-model"
    finally:
        db.close()

    # 读取接口同样只返回脱敏 Key
    got = client.get("/api/ai/config").json()
    assert RAW_KEY not in str(got)
    assert got["api_key_masked"] == status["api_key_masked"]


def test_restart_recovery(client):
    """测试 3：重启恢复——新会话读取 + 用环境变量重新派生密钥解密成功"""
    save_config(client)

    db = TestSession()
    try:
        row = db.query(WebAiConfig).order_by(WebAiConfig.id.asc()).first()
        assert row is not None
    finally:
        db.close()

    # 模拟服务重启：用 .env 中的 secret 重新构造 Fernet 也能解密
    fernet = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET.encode()).digest()))
    assert fernet.decrypt(row.encrypted_api_key.encode()).decode() == RAW_KEY


# ---------------- 配置优先级（指令 3） ----------------


def test_web_config_priority_over_env(client, monkeypatch):
    """测试 4：Web 配置优先于 .env；Web 未填的字段回落 .env"""
    for key, value in ENV_AI.items():
        monkeypatch.setenv(key, value)

    # Web 只填 Key 和模型，Base URL 留空 → 该字段回落 .env
    status = save_config(client, base_url="")
    assert status["key_source"] == "web"
    assert status["model"] == "web-model"            # Web 优先
    assert status["base_url"] == ENV_AI["AI_BASE_URL"]  # 字段级回退

    # 生效配置解析：Key 来自 Web（解密后），其余字段按优先级合并
    db = TestSession()
    try:
        cfg = web_config_service.get_effective_ai_config(db)
    finally:
        db.close()
    assert cfg["api_key"] == RAW_KEY
    assert cfg["key_source"] == "web"
    assert cfg["model"] == "web-model"

    # /api/ai/status（Phase 6 接口）同样反映 Web 配置
    got = client.get("/api/ai/status").json()
    assert got["configured"] is True
    assert got["model"] == "web-model"


def test_clear_restores_env(client, monkeypatch):
    """测试 5：清除 Web 配置后恢复使用 .env；再保存可覆盖"""
    for key, value in ENV_AI.items():
        monkeypatch.setenv(key, value)

    save_config(client)
    assert client.delete("/api/ai/config").status_code == 200

    status = client.get("/api/ai/config").json()
    assert status["key_source"] == "env"
    assert status["model"] == ENV_AI["AI_MODEL"]
    assert status["api_key_masked"] == web_config_service.mask_api_key(ENV_AI["AI_API_KEY"])
    assert RAW_KEY not in str(status)

    db = TestSession()
    try:
        assert db.query(WebAiConfig).count() == 0
    finally:
        db.close()

    # 清除后可以再次保存（删除行后重新 upsert）
    again = save_config(client)
    assert again["key_source"] == "web"


def test_env_used_when_no_web_config(client, monkeypatch):
    """测试 6：从未保存 Web 配置时 → 生效配置完全来自 .env"""
    for key, value in ENV_AI.items():
        monkeypatch.setenv(key, value)
    status = client.get("/api/ai/config").json()
    assert status["configured"] is True
    assert status["key_source"] == "env"
    assert status["model"] == ENV_AI["AI_MODEL"]
    assert RAW_KEY not in str(status)


# ---------------- 测试连接（指令 2） ----------------


def test_config_test_success_minimal_request(client, monkeypatch):
    """测试 7：测试连接用最小化请求（max_tokens 极小）并成功返回"""
    for key, value in ENV_AI.items():
        monkeypatch.setenv(key, value)

    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
            )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    monkeypatch.setattr(ai_service, "AsyncOpenAI", lambda **kw: fake_client)

    resp = client.post("/api/ai/config/test", json={})  # 留空 → 用生效配置
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert ENV_AI["AI_MODEL"] in data["message"]
    assert RAW_KEY not in resp.text
    # 最小化请求：只要求回复一个词，max_tokens 极小（控制 Token 消耗）
    assert captured["max_tokens"] <= 20


def test_config_test_failure_masks_key(client, monkeypatch):
    """测试 8：测试连接失败时 ok=false，错误信息抹去 Key"""
    def boom(**kwargs):
        raise RuntimeError(f"401 unauthorized for key {RAW_KEY}")

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=boom))
    )
    monkeypatch.setattr(ai_service, "AsyncOpenAI", lambda **kw: fake_client)

    resp = client.post(
        "/api/ai/config/test",
        json={"api_key": RAW_KEY, "base_url": "https://web.test/v1", "model": "web-model"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "连接失败" in data["message"]
    assert RAW_KEY not in resp.text          # Key 已被抹去


def test_config_test_incomplete_400(client, monkeypatch):
    """测试 9：无任何可用配置（env 缺失且无 Web 配置）→ 400 明确提示"""
    for name in ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    resp = client.post("/api/ai/config/test", json={})
    assert resp.status_code == 400
    assert "配置不完整" in resp.json()["detail"]


# ---------------- Phase 8 优化：首轮检查不等完整间隔 ----------------


def test_first_run_runs_soon_after_start(monkeypatch):
    """测试 10：启动后首轮在短延迟内执行一次（而非等待完整间隔），且不重复"""
    monkeypatch.setenv("AUTO_TASK_ENABLED", "true")
    monkeypatch.setattr(automation_service, "FIRST_RUN_DELAY_SECONDS", 0.05)

    calls = []

    async def fake_run_auto_check(db):
        calls.append(1)

    monkeypatch.setattr(automation_service, "run_auto_check", fake_run_auto_check)

    async def scenario():
        task = asyncio.create_task(automation_service._auto_loop(60))  # 间隔 60 分钟
        await asyncio.sleep(0.3)
        assert len(calls) == 1  # 远小于 60 分钟，首轮已执行且只执行一次
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

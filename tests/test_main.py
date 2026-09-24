"""
基础接口测试

运行方式（项目根目录下执行）：
    pytest tests/
"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_check():
    """健康检查接口应返回 ok"""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_portfolio_summary():
    """账户概览接口应返回正常结构（Phase 3 起字段来自真实持仓计算）"""
    resp = client.get("/api/portfolio/summary")
    # 注意：若本地数据库已有持仓，此接口会请求真实基金数据源
    assert resp.status_code in (200, 503)  # 503 = 数据源临时不可用
    if resp.status_code == 200:
        data = resp.json()
        assert "holding_count" in data
        assert "total_market_value" in data
        assert "total_profit" in data
        assert "latest_nav_change" in data


def test_funds_list():
    """基金列表接口应返回列表"""
    resp = client.get("/api/funds")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_index_page():
    """首页应能正常返回 HTML"""
    resp = client.get("/")
    assert resp.status_code == 200

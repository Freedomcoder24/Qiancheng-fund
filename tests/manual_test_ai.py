"""
Phase 6 AI 分析手动端到端验证脚本（发真实请求，不进 pytest）

验证内容：
1. GET /api/ai/status：配置状态正常且不泄漏 Key
2. POST /api/ai/analysis：真实持仓 → 真实基金数据 → 真实 AI API → 结构化结果

运行方式（项目根目录）：
    & .venv\\Scripts\\python.exe tests\\manual_test_ai.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


async def main():
    client = TestClient(app)

    print("=" * 60)
    print("1) GET /api/ai/status")
    resp = client.get("/api/ai/status")
    print(f"   HTTP {resp.status_code} -> {resp.json()}")
    assert resp.status_code == 200
    assert resp.json()["configured"] is True, "请在 .env 配置 AI_API_KEY / AI_BASE_URL / AI_MODEL"
    assert "sk-" not in resp.text, "status 响应泄漏了 API Key！"
    print("   ✓ 配置状态正常，响应未泄漏 API Key")

    print("=" * 60)
    print("2) POST /api/ai/analysis（真实 AI 调用，约需 10~60 秒）")
    resp = client.post("/api/ai/analysis")
    print(f"   HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"   失败详情: {resp.json().get('detail')}")
        return
    data = resp.json()
    print(f"   模型: {data['model']}")
    print(f"   生成时间: {data['generated_at']}")
    print(f"   数据日期: {data['data_date']}")
    print(f"   [今日持仓总结] {data['today_summary']}")
    print(f"   [收益来源] {data['profit_sources']}")
    print(f"   [风险提示] {'；'.join(data['risk_warnings']) or '（无）'}")
    print(f"   [免责声明] {data['disclaimer']}")
    assert data["today_summary"] and data["profit_sources"], "AI 返回内容为空"
    assert "不构成投资建议" in data["disclaimer"]
    print("   ✓ 端到端验证通过")


if __name__ == "__main__":
    asyncio.run(main())

"""
Phase 18 Agent 多轮会话 + SSE 流式 真实端到端验证（发真实请求，不进 pytest）

⚠️ 消耗真实 Token（gpt-5.6 两轮对话）；全程只读 Tool，不写数据库

验证内容：
1. 第一轮（真实流式）："帮我看看基金 000001"
   - SSE 事件序：session → status → (tool 状态) → delta* → done
   - delta 增量真实逐步到达（记录首块延迟与块数，证明非一次性返回）
2. 第二轮（同一 session_id，指代理解）："它最近90天表现怎么样？"
   - 后端携带第一轮历史；回答应围绕 000001 的近 90 天数据展开
3. Session 基线：第二轮响应 session_id 与第一轮一致
4. 数据库验收前后持仓一致（全程只读）

运行方式（项目根目录）：
    & .venv\\Scripts\\python.exe tests\\manual_test_agent_stream.py
"""
import asyncio
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["AUTO_TASK_ENABLED"] = "false"  # 先于 load_dotenv，阻后台任务写真实库
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
          "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"

from dotenv import load_dotenv
load_dotenv()

import httpx
import uvicorn
from app.main import app

PORT = 8018
BASE = f"http://127.0.0.1:{PORT}"

server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()

for _ in range(60):
    try:
        if httpx.get(f"{BASE}/api/health", trust_env=False, timeout=2).status_code == 200:
            break
    except Exception:
        pass
    time.sleep(0.3)
else:
    raise SystemExit("server did not start")


def stream_turn(client: httpx.Client, message: str, session_id: str = "") -> dict:
    """发起一轮流式对话，逐事件打印并返回汇总"""
    print("=" * 64)
    print(f"用户：{message}（session_id={session_id or '新建'}）")
    start = time.time()
    summary = {"events": [], "deltas": [], "session_id": "", "done": None,
               "first_delta_at": None, "tool_labels": []}

    with client.stream(
        "POST", f"{BASE}/api/agent/chat/stream",
        json={"message": message, "session_id": session_id},
        timeout=300,
    ) as resp:
        print(f"HTTP {resp.status_code} ({resp.headers.get('content-type')})")
        assert resp.status_code == 200
        buf = ""
        for chunk in resp.iter_text():
            buf += chunk
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                block = block.strip()
                if not block.startswith("data:"):
                    continue
                ev = json.loads(block[5:].strip())
                etype = ev.get("type")
                summary["events"].append(etype)

                if etype == "session":
                    summary["session_id"] = ev["session_id"]
                    print(f"  [session] id={ev['session_id'][:8]}...")
                elif etype == "status":
                    if ev.get("stage") == "tool":
                        summary["tool_labels"].append(ev.get("label"))
                        print(f"  [status ] 正在{ev.get('label')}...")
                    else:
                        print(f"  [status ] {ev.get('stage')}")
                elif etype == "tool_done":
                    print(f"  [tool   ] {'完成' if ev.get('ok') else '失败'}：{ev.get('label')}")
                elif etype == "delta":
                    if summary["first_delta_at"] is None:
                        summary["first_delta_at"] = time.time() - start
                    summary["deltas"].append(ev["text"])
                elif etype == "done":
                    summary["done"] = ev
                elif etype == "error":
                    print(f"  [error  ] {ev.get('detail')}")

    answer = summary["done"]["answer"] if summary["done"] else "".join(summary["deltas"])
    print(f"  流式证据: delta {len(summary['deltas'])} 块, "
          f"首块 {summary['first_delta_at'] and round(summary['first_delta_at'], 1)}s, "
          f"总耗时 {round(time.time() - start, 1)}s")
    print(f"  工具调用链: {summary['done']['tools_used'] if summary['done'] else '(未完成)'}")
    print(f"  Agent 回复:\n{answer}")
    return summary


async def main():
    print("0) 前置检查：持仓基线")
    with httpx.Client(trust_env=False, timeout=60) as client:
        holdings_before = client.get(f"{BASE}/api/portfolio/holdings").json()
        print(f"   真实持仓 {len(holdings_before)} 只: "
              + ", ".join(h["fund_code"] for h in holdings_before))

        # 第一轮：建立上下文（真实流式）
        t1 = stream_turn(client, "帮我看看基金 000001")
        assert t1["done"], "第一轮未正常完成"
        assert t1["events"][0] == "session"
        assert t1["events"][-1] == "done"
        assert len(t1["deltas"]) >= 2, "delta 应分块到达（流式）"
        assert "error" not in t1["events"]
        sid = t1["session_id"]

        # 第二轮：同一会话，指代理解（"它" = 000001）
        t2 = stream_turn(client, "它最近90天表现怎么样？", session_id=sid)
        assert t2["done"], "第二轮未正常完成"
        assert t2["session_id"] == sid, "第二轮 session_id 应与第一轮一致"
        answer2 = t2["done"]["answer"]
        assert "90" in answer2 or "近90" in answer2.replace(" ", ""), \
            f"第二轮回答应围绕近 90 天展开，实际：{answer2[:80]}"
        assert ("000001" in answer2 or "华夏成长" in answer2), \
            f"第二轮回答应体现对'它'=000001 的理解，实际：{answer2[:80]}"

        # 数据库只读校验
        print("=" * 64)
        holdings_after = client.get(f"{BASE}/api/portfolio/holdings").json()
        before = [(h["id"], h["fund_code"], h["shares"], h["cost_price"]) for h in holdings_before]
        after = [(h["id"], h["fund_code"], h["shares"], h["cost_price"]) for h in holdings_after]
        assert before == after, "持仓数据发生变化！"
        print("✓ 多轮流式 E2E 全过：指代理解生效、流式分块到达、会话延续、数据库只读")

    server.should_exit = True


if __name__ == "__main__":
    asyncio.run(main())

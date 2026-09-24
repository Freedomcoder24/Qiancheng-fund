"""
手动测试脚本：验证基金数据源真实连通性（会发起真实网络请求）

这个脚本不在 pytest 中自动运行，仅供开发者手动验证数据源是否可用。

用法（项目根目录下执行）：
    .venv\\Scripts\\python.exe tests/manual_test_fund.py
"""
import asyncio
import json
import re
import time

import httpx

TIMEOUT = 10.0

# 通用请求头：东方财富系接口对无 User-Agent / Referer 的请求可能拒绝
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}


async def test_history(client: httpx.AsyncClient) -> None:
    """测试 1：基金历史净值接口"""
    print("=" * 60)
    print("测试 1：历史净值 api.fund.eastmoney.com/f10/lsjz")
    url = "https://api.fund.eastmoney.com/f10/lsjz"
    params = {"fundCode": "000001", "pageIndex": 1, "pageSize": 5}
    # 该接口要求 Referer 指向天天基金官网，否则返回 403
    headers = {**HEADERS, "Referer": "https://fundf10.eastmoney.com/"}
    start = time.perf_counter()
    try:
        resp = await client.get(url, params=params, headers=headers)
        elapsed = time.perf_counter() - start
        print(f"HTTP 状态码: {resp.status_code}  耗时: {elapsed:.2f}s")
        resp.raise_for_status()
        data = resp.json()
        print(f"顶层键: {list(data.keys())}")
        print(f"TotalCount: {data.get('TotalCount')}")
        # 2026-09 实测：净值列表在 Data.LSJZList 里（嵌套结构）
        data_obj = data.get("Data") or {}
        datas = data_obj.get("LSJZList") or []
        print(f"本页条数: {len(datas)}")
        if datas:
            print(f"字段: {list(datas[0].keys())}")
            for item in datas[:3]:
                print(
                    f"  {item.get('FSRQ')}  单位净值={item.get('DWJZ')}  "
                    f"累计净值={item.get('LJJZ')}  日增长={item.get('JZZZL')}"
                )
            print("结论: 可用 ✓")
        else:
            print("结论: 返回为空，不可用 ✗")
    except Exception as e:
        print(f"失败: {type(e).__name__}: {e}")


async def test_valuation(client: httpx.AsyncClient) -> None:
    """测试 2：基金实时估值接口（JSONP）"""
    print("=" * 60)
    print("测试 2：实时估值 fundgz.1234567.com.cn/js/000001.js")
    url = "https://fundgz.1234567.com.cn/js/000001.js"
    start = time.perf_counter()
    try:
        resp = await client.get(url, headers=HEADERS)
        elapsed = time.perf_counter() - start
        print(f"HTTP 状态码: {resp.status_code}  耗时: {elapsed:.2f}s")
        print(f"Content-Type: {resp.headers.get('content-type')}")
        text = resp.text.strip()
        print(f"原始响应前 200 字符: {text[:200]}")
        if "jsonpgz" in text and "(" in text:
            inner = re.search(r"\((.*)\)", text, re.S).group(1).strip().rstrip(";")
            data = json.loads(inner)
            if data:
                print(
                    f"解析成功: fundcode={data.get('fundcode')} name={data.get('name')} "
                    f"dwjz={data.get('dwjz')} gsz={data.get('gsz')} "
                    f"gszzl={data.get('gszzl')} gztime={data.get('gztime')}"
                )
                print("结论: 可用 ✓")
            else:
                print("结论: 返回空对象（基金不存在或接口降级）✗")
        else:
            print("结论: 不是 JSONP 格式，不可用 ✗")
    except Exception as e:
        print(f"失败: {type(e).__name__}: {e}")


async def test_search(client: httpx.AsyncClient) -> None:
    """测试 3：基金搜索接口"""
    print("=" * 60)
    print("测试 3：基金搜索 fundsuggest.eastmoney.com FundSearchAPI")
    url = "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
    params = {"m": 1, "key": "半导体"}
    start = time.perf_counter()
    try:
        resp = await client.get(url, params=params, headers=HEADERS)
        elapsed = time.perf_counter() - start
        print(f"HTTP 状态码: {resp.status_code}  耗时: {elapsed:.2f}s")
        print(f"Content-Type: {resp.headers.get('content-type')}")
        resp.raise_for_status()
        data = resp.json()
        print(f"顶层键: {list(data.keys())}")
        datas = data.get("Datas") or []
        print(f"结果条数: {len(datas)}")
        if datas:
            first = datas[0]
            print(f"字段: {list(first.keys())}")
            print(
                f"首条: CODE={first.get('CODE')} NAME={first.get('NAME')} "
                f"category={first.get('category')}"
            )
            print(f"FundBaseInfo 示例: {str(first.get('FundBaseInfo'))[:150]}")
            print("结论: 可用 ✓")
        else:
            print("结论: 返回为空，不可用 ✗")
    except Exception as e:
        print(f"失败: {type(e).__name__}: {e}")


async def test_invalid_code(client: httpx.AsyncClient) -> None:
    """测试 4：无效基金代码的行为（用于错误处理设计）"""
    print("=" * 60)
    print("测试 4：无效基金代码 999999 的历史净值返回")
    url = "https://api.fund.eastmoney.com/f10/lsjz"
    params = {"fundCode": "999999", "pageIndex": 1, "pageSize": 5}
    headers = {**HEADERS, "Referer": "https://fundf10.eastmoney.com/"}
    try:
        resp = await client.get(url, params=params, headers=headers)
        data = resp.json()
        print(f"HTTP 状态码: {resp.status_code}  TotalCount: {data.get('TotalCount')}  Data: {data.get('Data')}")
    except Exception as e:
        print(f"失败: {type(e).__name__}: {e}")


async def main() -> None:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await test_history(client)
        await test_invalid_code(client)
        await test_search(client)
    print("=" * 60)
    print("全部测试完成")


if __name__ == "__main__":
    asyncio.run(main())

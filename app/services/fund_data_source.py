"""
基金数据源层：封装对第三方接口的 HTTP 请求和原始数据解析

架构设计（方便未来更换数据源）：
- FundDataSource：抽象基类，定义数据源的统一接口
- EastMoneyFundSource：东方财富 / 天天基金公开接口实现
- 业务层（fund_service）只依赖抽象基类，更换数据源时新增子类即可

⚠️ 风险说明：
这些是公开网页接口，不是官方开放 API，没有 SLA 保证，结构可能随时变化。
2026-09-22 实测：
- 历史净值 lsjz 接口可用（数据在 Data.LSJZList，必须带 Referer 头）
- fundgz 实时估值接口已失效（返回 notfound 页面），因此不实现估值
- 基金搜索接口可用
"""
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import date, timedelta

import httpx

from app.models.fund import (
    FundBasicInfo,
    FundHistoryPage,
    FundNavItem,
    FundRankingItem,
    FundRankingPage,
    FundValuation,
)
from app.utils.fund_parser import to_float

logger = logging.getLogger(__name__)


# ---------------- 自定义异常：用于区分不同类型的错误 ----------------

class FundNotFoundError(Exception):
    """基金不存在（代码写错或该基金没有净值数据）"""


class DataSourceUnavailableError(Exception):
    """数据源暂时不可用（网络异常 / 接口失效 / 返回格式异常）"""


# ---------------- 数据源抽象基类 ----------------

class FundDataSource(ABC):
    """数据源抽象基类

    未来如果接入新的数据源（例如官方开放 API），
    实现同样的三个方法即可，业务层代码不需要改动。
    """

    @abstractmethod
    async def get_fund_history(
        self, fund_code: str, page: int = 1, page_size: int = 20
    ) -> FundHistoryPage:
        """查询基金历史净值（分页）"""

    @abstractmethod
    async def search_funds(self, keyword: str) -> list[FundBasicInfo]:
        """按关键词搜索基金"""

    @abstractmethod
    async def get_navs_by_date_range(
        self, fund_code: str, start_date: str, end_date: str
    ) -> list[FundNavItem]:
        """按日期区间查询历史净值（Phase 4：区间收益 / 最大回撤 / 模拟市值）

        start_date / end_date 格式均为 YYYY-MM-DD。
        返回的列表按日期倒序（最新在前，与接口原始顺序一致）。
        """

    async def get_fund_valuation(self, fund_code: str) -> FundValuation | None:
        """查询实时估值。默认返回 None 表示该数据源不支持估值"""
        return None

    async def get_fund_ranking(
        self, fund_type: str = "gp", page: int = 1, page_size: int = 15
    ) -> FundRankingPage:
        """查询基金业绩排行（Phase 12 候选池数据来源）。默认不实现"""
        raise DataSourceUnavailableError("当前数据源不支持基金排行查询")


# ---------------- 东方财富数据源实现 ----------------

class EastMoneyFundSource(FundDataSource):
    """东方财富 / 天天基金公开接口数据源"""

    NAME = "eastmoney"  # 数据源名称（用于日志）

    def __init__(self, timeout: float = 10.0, transport: httpx.BaseTransport | None = None):
        """
        timeout: 请求超时时间（秒）
        transport: httpx 传输层，测试时传入 MockTransport，默认 None 表示真实请求
        """
        self._timeout = timeout
        self._transport = transport
        # 该系接口对没有 User-Agent 的请求可能拒绝，带上浏览器请求头
        self._headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
        }

    def _check_response(self, resp: httpx.Response, api_name: str) -> dict:
        """统一的响应检查：状态码 + JSON 格式，失败抛 DataSourceUnavailableError"""
        if resp.status_code != 200:
            raise DataSourceUnavailableError(
                f"{api_name}返回 HTTP {resp.status_code}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise DataSourceUnavailableError(
                f"{api_name}返回格式异常（不是 JSON）"
            ) from e

    async def get_fund_history(
        self, fund_code: str, page: int = 1, page_size: int = 20
    ) -> FundHistoryPage:
        """查询历史净值

        接口: https://api.fund.eastmoney.com/f10/lsjz
        2026-09 实测返回结构: {"TotalCount": n, "Data": {"LSJZList": [...]}, ...}
        每条记录: FSRQ 净值日期 / DWJZ 单位净值 / LJJZ 累计净值 / JZZZL 日增长率
        """
        url = "https://api.fund.eastmoney.com/f10/lsjz"
        params = {
            "fundCode": fund_code,
            "pageIndex": page,
            "pageSize": page_size,
        }
        # 该接口要求 Referer 指向天天基金官网，否则返回 403
        headers = {**self._headers, "Referer": "https://fundf10.eastmoney.com/"}

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("[%s] 历史净值网络异常 code=%s: %s", self.NAME, fund_code, e)
            raise DataSourceUnavailableError(f"历史净值接口网络异常: {e}") from e

        data = self._check_response(resp, "历史净值接口")
        elapsed = time.perf_counter() - start

        total_count = int(data.get("TotalCount") or 0)
        rows = ((data.get("Data") or {}).get("LSJZList")) or []

        # 无效基金代码：TotalCount 为 0 且列表为空
        if total_count == 0:
            raise FundNotFoundError(f"基金 {fund_code} 不存在或没有净值数据")

        items = [
            FundNavItem(
                date=row.get("FSRQ") or "",
                unit_nav=to_float(row.get("DWJZ")),
                accumulated_nav=to_float(row.get("LJJZ")),
                daily_change=to_float(row.get("JZZZL")),
            )
            for row in rows
        ]

        result = FundHistoryPage(
            fund_code=fund_code,
            page=page,
            page_size=page_size,
            total_count=total_count,
            items=items,
        )
        logger.info(
            "[%s] 历史净值 code=%s page=%s 耗时=%.2fs 结果=%d/%d 条",
            self.NAME, fund_code, page, elapsed, len(items), total_count,
        )
        return result

    async def get_navs_by_date_range(
        self, fund_code: str, start_date: str, end_date: str
    ) -> list[FundNavItem]:
        """按日期区间查询历史净值（Phase 4，自动翻页）

        接口: https://api.fund.eastmoney.com/f10/lsjz
        与 get_fund_history 相同，但带上 startDate / endDate 参数，
        只返回区间内的净值记录。列表按日期倒序（最新在前）。

        翻页说明（2026-09-22 实测）：带 startDate / endDate 参数时，
        接口每页固定只返回 20 条（pageSize 传多大都没用），
        180 天区间约 120 条记录需要翻 6 页；写成通用循环，
        取满 TotalCount 或拿到空页即停止。
        """
        url = "https://api.fund.eastmoney.com/f10/lsjz"
        # 该接口要求 Referer 指向天天基金官网，否则返回 403
        headers = {**self._headers, "Referer": "https://fundf10.eastmoney.com/"}
        # 实测带日期区间参数时每页固定返回 20 条，pageSize 传更大值无效
        page_size = 20
        all_items: list[FundNavItem] = []
        page = 1

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                while True:
                    params = {
                        "fundCode": fund_code,
                        "pageIndex": page,
                        "pageSize": page_size,
                        "startDate": start_date,
                        "endDate": end_date,
                    }
                    resp = await client.get(url, params=params, headers=headers)
                    data = self._check_response(resp, "历史净值区间接口")

                    total_count = int(data.get("TotalCount") or 0)
                    if total_count == 0:
                        # 区间内没有任何净值记录：可能是基金代码错误或区间太早
                        raise FundNotFoundError(
                            f"基金 {fund_code} 在 {start_date} ~ {end_date} 没有净值数据"
                        )

                    rows = ((data.get("Data") or {}).get("LSJZList")) or []
                    if not rows:
                        # TotalCount > 0 但本页没有数据，停止翻页避免死循环
                        break

                    all_items.extend(
                        FundNavItem(
                            date=row.get("FSRQ") or "",
                            unit_nav=to_float(row.get("DWJZ")),
                            accumulated_nav=to_float(row.get("LJJZ")),
                            daily_change=to_float(row.get("JZZZL")),
                        )
                        for row in rows
                    )

                    # 已取满总数，或本页不足一页（说明是最后一页）
                    if len(all_items) >= total_count or len(rows) < page_size:
                        break
                    page += 1
        except httpx.HTTPError as e:
            logger.warning("[%s] 区间净值网络异常 code=%s: %s", self.NAME, fund_code, e)
            raise DataSourceUnavailableError(f"历史净值接口网络异常: {e}") from e

        elapsed = time.perf_counter() - start
        logger.info(
            "[%s] 区间净值 code=%s %s~%s 耗时=%.2fs 结果=%d 条",
            self.NAME, fund_code, start_date, end_date, elapsed, len(all_items),
        )
        return all_items

    async def search_funds(self, keyword: str) -> list[FundBasicInfo]:
        """按关键词搜索基金

        接口: https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx
        返回的 Datas 里混有股票等结果，CATEGORY=700 才是基金。
        FundBaseInfo 是一个 dict，包含基金类型等信息。
        """
        url = "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
        params = {"m": 1, "key": keyword}

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(url, params=params, headers=self._headers)
        except httpx.HTTPError as e:
            logger.warning("[%s] 基金搜索网络异常 keyword=%s: %s", self.NAME, keyword, e)
            raise DataSourceUnavailableError(f"基金搜索接口网络异常: {e}") from e

        data = self._check_response(resp, "基金搜索接口")
        elapsed = time.perf_counter() - start

        results: list[FundBasicInfo] = []
        for row in data.get("Datas") or []:
            # 只要基金类结果，过滤掉股票、债券等
            if str(row.get("CATEGORY") or "") != "700":
                continue
            code = str(row.get("CODE") or "").strip()
            name = str(row.get("NAME") or "").strip()
            if not code or not name:
                continue
            base = row.get("FundBaseInfo")
            fund_type = base.get("FundType") if isinstance(base, dict) else None
            results.append(FundBasicInfo(code=code, name=name, fund_type=fund_type))

        logger.info(
            "[%s] 搜索 keyword=%s 耗时=%.2fs 基金结果=%d 条",
            self.NAME, keyword, elapsed, len(results),
        )
        return results

    async def get_fund_valuation(self, fund_code: str) -> FundValuation | None:
        """实时估值：fundgz 接口 2026-09 实测已失效，返回 None 表示不可用。

        未来如果找到可用的备用估值数据源，在这里实现并返回 FundValuation。
        """
        return None

    async def get_fund_ranking(
        self, fund_type: str = "gp", page: int = 1, page_size: int = 15
    ) -> FundRankingPage:
        """查询基金业绩排行（Phase 12 候选池数据来源，2026-09-23 真实接口回归修正）

        接口: https://fund.eastmoney.com/data/rankhandler.aspx（JSONP 格式）
        返回文本形如: var rankData = {datas:[...],allRecords:N,...};
        ⚠️ 键名无引号（JS 对象字面量，非严格 JSON），解析需容错处理。
        - fund_type: gp=股票型 / hh=混合型
        - sc=1nzf&st=desc：按近 1 年收益率降序（2026-09-23 实测：
          st=fd 为升序会把最差的排前面，sc=1nzf 为近 1 年涨幅字段）
        - datas 每行是逗号分隔的字符串，按固定索引取字段：
          0=代码 / 1=名称 / 3=净值日期 / 4=单位净值 / 6=日涨幅
          / 9=近3月 / 10=近6月 / 11=近1年；"-" 表示无数据（to_float 转 None）。
        """
        if fund_type not in ("gp", "hh"):
            raise DataSourceUnavailableError(f"不支持的排行类别: {fund_type}")

        end = date.today()
        params = {
            "op": "ph",
            "dt": "kf",
            "ft": fund_type,
            "rs": "",
            "gs": 0,
            "sc": "1nzf",
            "st": "desc",
            "sd": (end - timedelta(days=365)).isoformat(),
            "ed": end.isoformat(),
            "qdii": "",
            "tabSubtype": ",,,,",
            "pi": page,
            "pn": page_size,
            "dx": 1,
        }
        # 排行接口同样要求 Referer，否则可能被拒绝
        headers = {**self._headers, "Referer": "https://fund.eastmoney.com/"}

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(
                    "https://fund.eastmoney.com/data/rankhandler.aspx",
                    params=params,
                    headers=headers,
                )
        except httpx.HTTPError as e:
            logger.warning("[%s] 基金排行网络异常 ft=%s: %s", self.NAME, fund_type, e)
            raise DataSourceUnavailableError(f"基金排行接口网络异常: {e}") from e

        if resp.status_code != 200:
            raise DataSourceUnavailableError(f"基金排行接口返回 HTTP {resp.status_code}")

        # JSONP 剥壳：提取文本中第一个 { 到最后一个 } 之间的对象字面量
        match = re.search(r"\{.*\}", resp.text, re.DOTALL)
        if not match:
            raise DataSourceUnavailableError("基金排行接口返回格式异常（不是 JSONP 数据）")
        try:
            # 键名无引号（{datas:[...],allRecords:N}），先补引号转成严格 JSON 再解析
            json_like = match.group(0)
            json_like = re.sub(r'([{,]\s*)([A-Za-z_]\w*)\s*:', r'\1"\2":', json_like)
            data = json.loads(json_like)
        except ValueError as e:
            raise DataSourceUnavailableError(
                "基金排行接口返回格式异常（JSON 解析失败）"
            ) from e

        total_count = int(data.get("allRecords") or 0)
        rows = data.get("datas") or []

        items: list[FundRankingItem] = []
        for row in rows:
            cols = str(row).split(",")
            if len(cols) < 12 or not cols[0].strip():
                continue  # 字段不足 / 无代码的脏行直接跳过
            items.append(FundRankingItem(
                code=cols[0].strip(),
                name=cols[1].strip(),
                # 类别由请求参数决定，不依赖行内难以保证稳定的类型字段
                fund_type="股票型" if fund_type == "gp" else "混合型",
                nav_date=cols[3].strip() or None,
                unit_nav=to_float(cols[4]),
                daily_change=to_float(cols[6]),
                return_3m=to_float(cols[9]),
                return_6m=to_float(cols[10]),
                return_1y=to_float(cols[11]),
            ))

        elapsed = time.perf_counter() - start
        logger.info(
            "[%s] 基金排行 ft=%s page=%s 耗时=%.2fs 结果=%d/%d 条",
            self.NAME, fund_type, page, elapsed, len(items), total_count,
        )
        return FundRankingPage(
            fund_type=fund_type,
            page=page,
            page_size=page_size,
            total_count=total_count,
            items=items,
        )

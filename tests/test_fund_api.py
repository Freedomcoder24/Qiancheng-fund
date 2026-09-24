"""
基金数据模块测试（全部使用 mock，不发起真实网络请求）

测试思路：
1. 解析函数测试：直接测试 fund_parser 的各种输入
2. 数据源测试：用 httpx.MockTransport 伪造接口响应，测试解析和错误处理
3. API 测试：用 TestClient + 替换 fund_service.data_source，测试接口行为

真实数据源连通性测试请手动运行：tests/manual_test_fund.py
"""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import fund_service
from app.services.fund_data_source import (
    DataSourceUnavailableError,
    EastMoneyFundSource,
    FundNotFoundError,
)
from app.utils.fund_parser import FundParseError, parse_jsonp_response, to_float

client = TestClient(app)


def run(coro):
    """在同步测试中运行协程（避免依赖 pytest-asyncio 等插件）"""
    return asyncio.run(coro)


# ============================================================
# 第一部分：解析工具测试
# ============================================================

class TestJsonpParser:
    """JSONP 解析测试"""

    def test_normal_jsonp(self):
        """测试 3：正常 JSONP 可以解析"""
        text = 'jsonpgz({"fundcode":"000001","name":"华夏成长混合","dwjz":"1.3320"});'
        data = parse_jsonp_response(text)
        assert data["fundcode"] == "000001"
        assert data["name"] == "华夏成长混合"

    def test_pure_json(self):
        """纯 JSON 输入也能兼容"""
        data = parse_jsonp_response('{"code": "000001"}')
        assert data["code"] == "000001"

    def test_empty_response(self):
        """空响应抛出明确异常"""
        with pytest.raises(FundParseError):
            parse_jsonp_response("")
        with pytest.raises(FundParseError):
            parse_jsonp_response("   ")

    def test_html_response(self):
        """接口失效返回 HTML 页面时抛出明确异常（fundgz 实测就是这种情况）"""
        html = "<!doctype html><html><head><title>页面未找到</title></head></html>"
        with pytest.raises(FundParseError):
            parse_jsonp_response(html)

    def test_invalid_content(self):
        """括号内容不是合法 JSON 时抛出异常"""
        with pytest.raises(FundParseError):
            parse_jsonp_response("jsonpgz({invalid json});")


class TestToFloat:
    """数字转换测试：接口返回空字符串时不能崩溃"""

    def test_normal(self):
        assert to_float("1.3320") == 1.332
        assert to_float("-0.08") == -0.08
        assert to_float("2.70%") == 2.70  # 带百分号也能转

    def test_empty_and_invalid(self):
        assert to_float("") is None
        assert to_float(None) is None
        assert to_float("abc") is None


# ============================================================
# 第二部分：数据源测试（MockTransport 伪造 HTTP 响应）
# ============================================================

# 伪造的历史净值接口响应（结构与 2026-09 实测一致）
FAKE_HISTORY_RESPONSE = {
    "TotalCount": 6011,
    "ErrCode": 0,
    "Data": {
        "LSJZList": [
            {"FSRQ": "2026-09-21", "DWJZ": "1.3320", "LJJZ": "3.9050", "JZZZL": "-0.08"},
            {"FSRQ": "2026-09-18", "DWJZ": "1.3330", "LJJZ": "3.9060", "JZZZL": ""},
        ]
    },
}

# 伪造的搜索接口响应（CATEGORY=700 才是基金）
FAKE_SEARCH_RESPONSE = {
    "ErrCode": 0,
    "Datas": [
        {"CODE": "159325", "NAME": "半导体ETF南方", "CATEGORY": "700",
         "FundBaseInfo": {"FCODE": "159325", "FundType": "指数型-股票"}},
        {"CODE": "000001", "NAME": "平安银行", "CATEGORY": "200", "FundBaseInfo": None},
    ],
}


def make_mock_source(handler) -> EastMoneyFundSource:
    """创建使用 MockTransport 的数据源实例（不发起真实请求）"""
    return EastMoneyFundSource(transport=httpx.MockTransport(handler))


def mock_handler_factory(history_response=None, search_response=None, raise_error=None):
    """生成一个 MockTransport 处理函数"""
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_error is not None:
            raise raise_error
        if "lsjz" in str(request.url):
            if isinstance(history_response, Exception):
                raise history_response
            return httpx.Response(200, json=history_response or FAKE_HISTORY_RESPONSE)
        if "FundSearchAPI" in str(request.url):
            return httpx.Response(200, json=search_response or FAKE_SEARCH_RESPONSE)
        return httpx.Response(404, text="not found")
    return handler


class TestEastMoneySource:
    """数据源解析测试"""

    def test_history_parse(self):
        """测试 2：历史净值数据可以正常解析"""
        source = make_mock_source(mock_handler_factory())
        result = run(source.get_fund_history("000001", page=1, page_size=20))
        assert result.fund_code == "000001"
        assert result.total_count == 6011
        assert len(result.items) == 2
        assert result.items[0].date == "2026-09-21"
        assert result.items[0].unit_nav == 1.332
        assert result.items[0].daily_change == -0.08
        # 空字符串的日增长率应转成 None 而不是报错
        assert result.items[1].daily_change is None

    def test_history_not_found(self):
        """无效基金代码应抛出 FundNotFoundError"""
        empty = {"TotalCount": 0, "Data": {"LSJZList": []}}
        source = make_mock_source(mock_handler_factory(history_response=empty))
        with pytest.raises(FundNotFoundError):
            run(source.get_fund_history("999999"))

    def test_history_bad_status(self):
        """接口返回非 200 时应抛出数据源不可用异常"""
        def handler(request):
            return httpx.Response(403, text="forbidden")
        source = make_mock_source(handler)
        with pytest.raises(DataSourceUnavailableError):
            run(source.get_fund_history("000001"))

    def test_history_network_error(self):
        """测试 5：网络异常应转换为数据源不可用异常"""
        source = make_mock_source(
            mock_handler_factory(raise_error=httpx.ConnectError("网络不通"))
        )
        with pytest.raises(DataSourceUnavailableError):
            run(source.get_fund_history("000001"))

    def test_search_filter_funds_only(self):
        """测试 7（数据源层）：搜索结果应过滤掉非基金条目"""
        source = make_mock_source(mock_handler_factory())
        results = run(source.search_funds("半导体"))
        # 平安银行（股票 CATEGORY=200）应被过滤掉
        assert len(results) == 1
        assert results[0].code == "159325"
        assert results[0].name == "半导体ETF南方"
        assert results[0].fund_type == "指数型-股票"


# ============================================================
# 第三部分：FastAPI 接口测试（替换 fund_service.data_source）
# ============================================================

@pytest.fixture()
def mock_api(monkeypatch):
    """把 fund_service 里的数据源替换成 mock 数据源，并清空缓存"""
    source = make_mock_source(mock_handler_factory())
    monkeypatch.setattr(fund_service, "data_source", source)
    fund_service.clear_cache()
    return source


class TestFundApi:
    """API 接口行为测试"""

    def test_history_api(self, mock_api):
        """测试 6：GET /api/funds/{code}/history 能正常返回"""
        resp = client.get("/api/funds/000001/history?page=1&page_size=20")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fund_code"] == "000001"
        assert data["total_count"] == 6011
        assert data["items"][0]["unit_nav"] == 1.332

    def test_history_api_invalid_code_format(self, mock_api):
        """基金代码格式错误返回 400（参数错误），而不是 500"""
        resp = client.get("/api/funds/abc123/history")
        assert resp.status_code == 400

    def test_history_api_not_found(self, monkeypatch):
        """测试 4：错误基金代码不会导致服务器 500，而是返回 404"""
        empty = {"TotalCount": 0, "Data": {"LSJZList": []}}
        source = make_mock_source(mock_handler_factory(history_response=empty))
        monkeypatch.setattr(fund_service, "data_source", source)
        fund_service.clear_cache()
        resp = client.get("/api/funds/999999/history")
        assert resp.status_code == 404

    def test_history_api_source_down(self, monkeypatch):
        """数据源不可用时返回 503，而不是 500"""
        source = make_mock_source(
            mock_handler_factory(raise_error=httpx.ConnectError("网络不通"))
        )
        monkeypatch.setattr(fund_service, "data_source", source)
        fund_service.clear_cache()
        resp = client.get("/api/funds/000001/history")
        assert resp.status_code == 503

    def test_search_api(self, mock_api):
        """测试 7：基金搜索接口能正常返回"""
        resp = client.get("/api/funds/search", params={"keyword": "半导体"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["code"] == "159325"

    def test_valuation_api_unavailable(self, mock_api):
        """估值接口：当前数据源不可用时应返回 valuation_available=false，不伪造数据"""
        resp = client.get("/api/funds/000001/valuation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["valuation_available"] is False
        assert data["valuation"] is None

    def test_detail_api(self, mock_api):
        """综合信息接口：应包含最新净值和名称"""
        resp = client.get("/api/funds/000001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code"] == "000001"
        assert data["latest_nav"]["unit_nav"] == 1.332
        assert data["valuation_available"] is False

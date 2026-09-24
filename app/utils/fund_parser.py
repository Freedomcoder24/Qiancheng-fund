"""
基金数据解析工具

把数据源返回的原始文本（JSONP / 数字字符串等）转换成 Python 数据。
解析失败时抛出明确异常，绝不静默返回假数据。
"""
import json
import re


class FundParseError(Exception):
    """数据格式解析失败时抛出"""


def parse_jsonp_response(text: str) -> dict:
    """解析 JSONP 响应。

    天天基金估值类接口返回格式类似：jsonpgz({...});
    这个函数提取括号里的 JSON 并解析成 dict。

    参数 text 是原始响应文本，返回解析后的 dict。
    格式异常 / 空响应 / HTML 页面 都会抛出 FundParseError。
    """
    if text is None or not text.strip():
        raise FundParseError("响应内容为空")

    text = text.strip()

    # 有些接口直接返回纯 JSON，这种情况直接解析
    if text.startswith("{") or text.startswith("["):
        try:
            result = json.loads(text)
        except json.JSONDecodeError as e:
            raise FundParseError(f"JSON 解析失败: {e}") from e
        return result

    # JSONP：提取第一个 ( 和最后一个 ) 之间的内容
    match = re.search(r"\((.*)\)", text, re.DOTALL)
    if not match:
        raise FundParseError(f"不是合法的 JSONP 格式: {text[:100]}")

    inner = match.group(1).strip().rstrip(";").strip()
    if not inner:
        raise FundParseError("JSONP 括号内容为空")

    try:
        result = json.loads(inner)
    except json.JSONDecodeError as e:
        raise FundParseError(f"JSONP 内容解析失败: {e}") from e

    if not isinstance(result, dict):
        raise FundParseError(f"JSONP 内容不是对象: {text[:100]}")
    return result


def to_float(value) -> float | None:
    """把接口返回的数字字符串转换成 float。

    接口经常用 "" 或 None 表示无数据（例如 QDII 基金没有日增长率），
    这里统一转成 None，避免一个空字符串导致整个程序崩溃。
    """
    if value is None:
        return None
    text = str(value).strip().replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None

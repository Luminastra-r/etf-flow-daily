"""ETF 证券代码标准化和跨来源统一主键。"""
from __future__ import annotations

import re


def normalize_code(value: object) -> str:
    """返回六位证券代码；不能可靠识别时抛出 ValueError。"""
    text = str(value or "").strip().upper()
    digits = re.sub(r"\D", "", text)
    if len(digits) != 6:
        raise ValueError(f"无效 ETF 代码: {value!r}")
    return digits


def infer_exchange(code: str, source_exchange: str | None = None) -> str:
    hint = str(source_exchange or "").strip().upper()
    if hint in {"SH", "SSE", "上海", "SHANGHAI"}:
        return "SH"
    if hint in {"SZ", "SZSE", "深圳", "SHENZHEN"}:
        return "SZ"
    code = normalize_code(code)
    if code.startswith(("5", "6")):
        return "SH"
    if code.startswith(("1", "3")):
        return "SZ"
    raise ValueError(f"无法推断交易所: {code}")


def instrument_id(value: object, source_exchange: str | None = None) -> str:
    code = normalize_code(value)
    return f"{infer_exchange(code, source_exchange)}.{code}"


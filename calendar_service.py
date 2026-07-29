"""Baostock 交易日历与沪深300基准，AKShare 仅作交叉验证。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import akshare as ak
import baostock as bs
import pandas as pd


def today_cn() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def trading_dates(start: date, end: date) -> list[str]:
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"Baostock 登录失败: {login.error_msg}")
    try:
        rs = bs.query_trade_dates(start_date=start.isoformat(), end_date=end.isoformat())
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        if rs.error_code != "0":
            raise RuntimeError(f"Baostock 交易日历失败: {rs.error_msg}")
        frame = pd.DataFrame(rows, columns=rs.fields)
        return frame.loc[frame["is_trading_day"] == "1", "calendar_date"].tolist()
    finally:
        bs.logout()


def is_trading_day(value: str) -> bool:
    d = date.fromisoformat(value)
    return value in trading_dates(d, d)


def latest_completed_trade_date(as_of: date | None = None) -> str:
    as_of = as_of or today_cn()
    dates = trading_dates(as_of - timedelta(days=20), as_of - timedelta(days=1))
    if not dates:
        raise RuntimeError("近20天未找到已完成交易日")
    return dates[-1]


def benchmark_closes(start_date: str, end_date: str) -> tuple[pd.DataFrame, list[str]]:
    """Baostock 为主，AKShare 交叉验证；返回警告而非伪造缺失值。"""
    warnings: list[str] = []
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"Baostock 登录失败: {login.error_msg}")
    try:
        rs = bs.query_history_k_data_plus(
            "sh.000300", "date,close", start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="3",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        primary = pd.DataFrame(rows, columns=rs.fields)
    finally:
        bs.logout()
    if primary.empty:
        return pd.DataFrame(columns=["date", "close"]), ["沪深300 Baostock 数据为空"]
    primary["close"] = pd.to_numeric(primary["close"], errors="coerce")
    primary["date"] = primary["date"].astype(str)

    try:
        check = ak.stock_zh_index_daily(symbol="sh000300")
        check["date"] = pd.to_datetime(check["date"]).dt.strftime("%Y-%m-%d")
        check["close"] = pd.to_numeric(check["close"], errors="coerce")
        merged = primary.merge(check[["date", "close"]], on="date", suffixes=("_bs", "_ak"))
        if not merged.empty:
            deviation = ((merged["close_bs"] - merged["close_ak"]).abs()
                         / merged["close_bs"].replace(0, pd.NA)).max()
            if pd.notna(deviation) and deviation > 0.02:
                warnings.append(f"沪深300跨源最大偏差 {deviation:.2%}")
    except Exception as exc:  # 交叉验证失败不替代主数据
        warnings.append(f"沪深300 AKShare 交叉验证不可用: {exc}")
    return primary[["date", "close"]].dropna(), warnings


"""Baostock 交易日历与沪深300基准，AKShare 仅作交叉验证。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import time
from zoneinfo import ZoneInfo

import akshare as ak
import baostock as bs
import pandas as pd

from config import SETTINGS


def today_cn() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _ak_trading_dates(start: date, end: date) -> list[str]:
    """AKShare 备用交易日历（新浪财经）。"""
    frame = ak.tool_trade_date_hist_sina()
    all_dates = frame["trade_date"].astype(str).tolist()
    return [d for d in all_dates if start.isoformat() <= d <= end.isoformat()]


def _retry(fn, label: str):
    last = None
    for attempt in range(int(SETTINGS["retry_count"])):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt + 1 < int(SETTINGS["retry_count"]):
                time.sleep(float(SETTINGS["retry_base_seconds"]) * (attempt + 1))
    raise RuntimeError(f"{label} 连续失败: {last}")


def _baostock_trading_dates(start: date, end: date) -> list[str]:
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


def trading_dates(start: date, end: date) -> list[str]:
    """Baostock 为主，AKShare 备用；baostock 失败时自动切换。"""
    try:
        return _retry(lambda: _baostock_trading_dates(start, end), "Baostock 交易日历")
    except Exception as exc:
        print(f"[calendar] Baostock 不可用，切换 AKShare: {exc}")
        return _retry(lambda: _ak_trading_dates(start, end), "AKShare 交易日历")


def is_trading_day(value: str) -> bool:
    d = date.fromisoformat(value)
    return value in trading_dates(d, d)


def latest_completed_trade_date(as_of: date | None = None) -> str:
    as_of = as_of or today_cn()
    dates = trading_dates(as_of - timedelta(days=20), as_of - timedelta(days=1))
    if not dates:
        raise RuntimeError("近20天未找到已完成交易日")
    return dates[-1]


def _baostock_benchmark_closes(start_date: str, end_date: str) -> pd.DataFrame:
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
        if rs.error_code != "0":
            raise RuntimeError(f"Baostock 沪深300失败: {rs.error_msg}")
        return pd.DataFrame(rows, columns=rs.fields)
    finally:
        bs.logout()


def _ak_benchmark_closes(start_date: str, end_date: str) -> pd.DataFrame:
    frame = ak.stock_zh_index_daily(symbol="sh000300")
    frame["date"] = pd.to_datetime(frame["date"]).dt.strftime("%Y-%m-%d")
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame[(frame["date"] >= start_date) & (frame["date"] <= end_date)]


def benchmark_closes(start_date: str, end_date: str) -> tuple[pd.DataFrame, list[str]]:
    """Baostock 为主，AKShare 备用；返回警告而非伪造缺失值。"""
    warnings: list[str] = []
    primary = pd.DataFrame(columns=["date", "close"])
    try:
        primary = _retry(
            lambda: _baostock_benchmark_closes(start_date, end_date),
            "Baostock 沪深300",
        )
    except Exception as exc:
        warnings.append(f"Baostock 不可用，使用 AKShare: {exc}")
    if primary.empty:
        try:
            primary = _retry(
                lambda: _ak_benchmark_closes(start_date, end_date),
                "AKShare 沪深300",
            )
        except Exception as exc:
            warnings.append(f"沪深300 AKShare 也不可用: {exc}")
            return pd.DataFrame(columns=["date", "close"]), warnings
    else:
        primary["close"] = pd.to_numeric(primary["close"], errors="coerce")
        primary["date"] = primary["date"].astype(str)
    if primary.empty:
        return pd.DataFrame(columns=["date", "close"]), ["沪深300 数据为空"]
    return primary[["date", "close"]].dropna(), warnings


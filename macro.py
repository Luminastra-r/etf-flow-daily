# -*- coding: utf-8 -*-
"""宏观辅助数据：基准指数、宽基估值分位、人民币+美元指数、中美利差、融资融券。

数据源均已实测可用（akshare 1.18.x）。全部带容错，某一路失败返回空表，不影响整体报告。
统一返回以 date 为列的 DataFrame（升序）。
"""
import warnings

import akshare as ak
import pandas as pd

warnings.filterwarnings("ignore")


def _norm_date(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    for c in df.columns:
        if "日期" in str(c) or str(c).lower() == "date":
            df = df.rename(columns={c: "date"})
            break
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    return df


def get_index_hist(symbol: str = "sh000300") -> pd.DataFrame:
    """基准指数日线收盘。返回 date, close。"""
    try:
        df = ak.stock_zh_index_daily(symbol=symbol)
        df = _norm_date(df)
        if not df.empty and "close" in df.columns:
            return df[["date", "close"]]
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


def get_valuation(symbol: str = "沪深300") -> pd.DataFrame:
    """宽基估值：滚动市盈率(PE-TTM)。返回 date, pe_ttm。"""
    try:
        df = ak.stock_index_pe_lg(symbol=symbol)
        df = _norm_date(df)
        col = next((c for c in df.columns if "滚动市盈率" in str(c)
                    and "中位数" not in str(c) and "等权" not in str(c)), None)
        if col and not df.empty:
            out = df[["date", col]].rename(columns={col: "pe_ttm"})
            out["pe_ttm"] = pd.to_numeric(out["pe_ttm"], errors="coerce")
            return out.dropna(subset=["pe_ttm"])
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


def get_usdcny() -> pd.DataFrame:
    """美元兑离岸人民币。返回 date, usdcny。"""
    try:
        df = ak.forex_hist_em(symbol="USDCNH")
        df = _norm_date(df)
        col = next((c for c in df.columns if "最新价" in str(c) or "收盘" in str(c)), None)
        if col and not df.empty:
            out = df[["date", col]].rename(columns={col: "usdcny"})
            out["usdcny"] = pd.to_numeric(out["usdcny"], errors="coerce")
            return out.dropna(subset=["usdcny"])
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


def get_dxy() -> pd.DataFrame:
    """当前允许数据源未提供稳定 DXY 日线接口，显式降级。"""
    return pd.DataFrame()


def get_bond() -> pd.DataFrame:
    """中美十年期国债收益率 + 利差。返回 date, cn10y, us10y, spread。"""
    try:
        df = ak.bond_zh_us_rate(start_date="20150101")
        df = _norm_date(df)
        cn = next((c for c in df.columns if "中国国债收益率10年" in str(c)), None)
        us = next((c for c in df.columns if "美国国债收益率10年" in str(c)), None)
        if cn and us and not df.empty:
            out = df[["date", cn, us]].rename(columns={cn: "cn10y", us: "us10y"})
            out["cn10y"] = pd.to_numeric(out["cn10y"], errors="coerce")
            out["us10y"] = pd.to_numeric(out["us10y"], errors="coerce")
            out["spread"] = out["cn10y"] - out["us10y"]
            return out.dropna(subset=["cn10y"])
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


def get_margin() -> pd.DataFrame:
    """融资余额（上海市场，亿元）。返回 date, margin。"""
    try:
        df = ak.macro_china_market_margin_sh()
        df = _norm_date(df)
        col = next((c for c in df.columns if "融资余额" in str(c)), None)
        if col and not df.empty:
            out = df[["date", col]].rename(columns={col: "margin"})
            out["margin"] = pd.to_numeric(out["margin"], errors="coerce")
            return out.dropna(subset=["margin"])
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


def load_all(index_symbol="sh000300") -> dict:
    """一次性取齐宏观数据，返回 dict；失败项为空表。"""
    return {
        "index": get_index_hist(index_symbol),
        "valuation": get_valuation(),
        "usdcny": get_usdcny(),
        "dxy": get_dxy(),
        "bond": get_bond(),
        "margin": get_margin(),
    }


if __name__ == "__main__":
    m = load_all()
    for k, v in m.items():
        tip = f" 最新={v['date'].max().date()}" if not v.empty else "（空）"
        print(f"[macro] {k}: {len(v)} 行{tip}")

# -*- coding: utf-8 -*-
"""宏观辅助数据：基准指数、估值、美元指数、中美利差与融资融券。

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


def get_dxy() -> pd.DataFrame:
    """美元指数(DXY)日线。返回 date, dxy。

    免 API Key：优先 Yahoo Finance 公开 chart 接口（DX-Y.NYB），
    GitHub Actions 美国节点可直连；失败回退 AKShare 新浪美股指数。
    """
    # 源 1：Yahoo Finance chart 接口（无需 API Key，返回纯 JSON）
    try:
        import json as _json
        import urllib.request as _urllib
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB"
               "?range=1y&interval=1d")
        req = _urllib.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _urllib.urlopen(req, timeout=20) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
        result = payload.get("chart", {}).get("result") or []
        if result:
            ts = result[0].get("timestamp", []) or []
            quote = (result[0].get("indicators", {}).get("quote") or [{}])[0]
            closes = quote.get("close", []) or []
            rows = []
            for i in range(len(ts)):
                if i < len(closes) and closes[i] is not None:
                    d = pd.Timestamp(ts[i], unit="s", tz="UTC").tz_convert(
                        "America/New_York").normalize()
                    rows.append((d, closes[i]))
            if rows:
                df = pd.DataFrame(rows, columns=["date", "dxy"])
                df["date"] = pd.to_datetime(df["date"])
                df["dxy"] = pd.to_numeric(df["dxy"], errors="coerce")
                df = df.dropna(subset=["dxy"]).sort_values("date").reset_index(drop=True)
                if not df.empty:
                    return df[["date", "dxy"]]
    except Exception:  # noqa: BLE001
        pass
    # 源 2：AKShare 新浪美股指数（备用）
    try:
        df = ak.index_us_stock_sina(symbol=".DXY")
        df = _norm_date(df)
        col = next((c for c in df.columns if "close" in str(c).lower()
                    or "收盘" in str(c) or "price" in str(c).lower()), None)
        if col and not df.empty:
            out = df[["date", col]].rename(columns={col: "dxy"})
            out["dxy"] = pd.to_numeric(out["dxy"], errors="coerce")
            return out.dropna(subset=["dxy"])
    except Exception:  # noqa: BLE001
        pass
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
        "dxy": get_dxy(),
        "bond": get_bond(),
        "margin": get_margin(),
    }


if __name__ == "__main__":
    m = load_all()
    for k, v in m.items():
        tip = f" 最新={v['date'].max().date()}" if not v.empty else "（空）"
        print(f"[macro] {k}: {len(v)} 行{tip}")

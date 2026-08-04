# -*- coding: utf-8 -*-
"""宏观辅助数据：基准指数、估值、美元指数、中美利差与融资融券。

数据源均已实测可用（akshare 1.18.x）。全部带容错，某一路失败返回空表，不影响整体报告。
统一返回以 date 为列的 DataFrame（升序）。
"""
import json
import urllib.parse
import urllib.request
import warnings

import akshare as ak
import pandas as pd

warnings.filterwarnings("ignore")


def _yahoo_daily(symbol: str, field: str, range_: str = "5y") -> pd.DataFrame:
    """读取 Yahoo Finance 日线收盘；用于不提供 API Key 的非核心市场代理。"""
    encoded = urllib.parse.quote(symbol, safe="")
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
           f"?range={range_}&interval=1d")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    result = payload.get("chart", {}).get("result") or []
    if not result:
        return pd.DataFrame()
    ts = result[0].get("timestamp", []) or []
    closes = ((result[0].get("indicators", {}).get("quote") or [{}])[0]
              .get("close", []) or [])
    rows = []
    for stamp, close in zip(ts, closes):
        if close is not None:
            date = pd.Timestamp(stamp, unit="s", tz="UTC").tz_convert(
                "America/New_York").tz_localize(None).normalize()
            rows.append((date, close))
    frame = pd.DataFrame(rows, columns=["date", field])
    if frame.empty:
        return frame
    frame[field] = pd.to_numeric(frame[field], errors="coerce")
    return frame.dropna(subset=[field]).drop_duplicates("date", keep="last").sort_values("date")


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
        df = _yahoo_daily("DX-Y.NYB", "dxy", "1y")
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


def get_cpi_yoy() -> pd.DataFrame:
    """美国 CPI 同比；按实际发布日期生效，避免未来数据穿越。"""
    try:
        df = ak.macro_usa_cpi_yoy()
        release = next((c for c in df.columns if "发布时间" in str(c)), None)
        period = next((c for c in df.columns if str(c) == "时间"), None)
        value = next((c for c in df.columns if "现值" in str(c)), None)
        # 部分 Windows/Actions 环境的 AKShare 返回列名会发生编码损坏，列序不变。
        if len(df.columns) >= 3:
            period = period or df.columns[0]
            release = release or df.columns[1]
            value = value or df.columns[2]
        if release and value:
            out = df[[release, value] + ([period] if period else [])].copy()
            names = {release: "date", value: "cpi_yoy"}
            if period:
                names[period] = "cpi_period"
            out = out.rename(columns=names)
            out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.normalize()
            out["cpi_yoy"] = pd.to_numeric(out["cpi_yoy"], errors="coerce")
            return out.dropna(subset=["date", "cpi_yoy"]).sort_values("date")
    except Exception:  # noqa: BLE001
        pass
    return pd.DataFrame()


def get_gold() -> pd.DataFrame:
    """COMEX 黄金期货主连（GC=F），作为黄金现货方向代理。"""
    try:
        return _yahoo_daily("GC=F", "gold", "5y")
    except Exception:  # noqa: BLE001
        pass
    try:
        import yfinance as yf
        df = yf.download("GC=F", period="5y", interval="1d", auto_adjust=False,
                         progress=False, threads=False)
        if df.empty:
            return pd.DataFrame()
        close = df["Close"]
        if isinstance(close, pd.DataFrame):
            close = close.iloc[:, 0]
        out = close.rename("gold").reset_index().rename(columns={"Date": "date"})
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.tz_localize(None)
        out["gold"] = pd.to_numeric(out["gold"], errors="coerce")
        return out.dropna(subset=["date", "gold"]).sort_values("date")
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def get_real_rate_gold() -> pd.DataFrame:
    """美国10年期收益率减 CPI 同比，并与黄金代理按日对齐。"""
    bond, cpi, gold = get_bond(), get_cpi_yoy(), get_gold()
    if bond.empty or cpi.empty:
        return pd.DataFrame()
    rates = bond[["date", "us10y"]].dropna().sort_values("date").copy()
    cpi_columns = [c for c in ["date", "cpi_yoy", "cpi_period"] if c in cpi]
    inflation = cpi[cpi_columns].dropna(subset=["date", "cpi_yoy"]).sort_values("date")
    combined = pd.merge_asof(rates, inflation, on="date", direction="backward")
    combined = combined.dropna(subset=["cpi_yoy"])
    combined["real_rate"] = combined["us10y"] - combined["cpi_yoy"]
    combined = combined.rename(columns={"date_y": "cpi_release_date"})
    # merge_asof 不保留右键，显式反推每行采用的最近 CPI 发布日。
    releases = inflation[["date"]].rename(columns={"date": "cpi_release_date"})
    combined = pd.merge_asof(
        combined.sort_values("date"), releases.sort_values("cpi_release_date"),
        left_on="date", right_on="cpi_release_date", direction="backward",
    )
    if not gold.empty:
        combined = combined.merge(gold[["date", "gold"]], on="date", how="outer")
    elif "gold" not in combined:
        combined["gold"] = pd.NA
    return combined.sort_values("date").reset_index(drop=True)


def load_all(index_symbol="sh000300") -> dict:
    """一次性取齐宏观数据，返回 dict；失败项为空表。"""
    return {
        "index": get_index_hist(index_symbol),
        "valuation": get_valuation(),
        "dxy": get_dxy(),
        "bond": get_bond(),
        "margin": get_margin(),
        "real_gold": get_real_rate_gold(),
    }


if __name__ == "__main__":
    m = load_all()
    for k, v in m.items():
        tip = f" 最新={v['date'].max().date()}" if not v.empty else "（空）"
        print(f"[macro] {k}: {len(v)} 行{tip}")

"""ETF staging：交易所份额优先、日期对齐、估算资金流与事务入库。"""
from __future__ import annotations

import io
import re
import time
from datetime import date

import akshare as ak
import pandas as pd
import requests

import calendar_service
import db
import metrics
import quality
from classify import classify_frame
from config import CLASSIFICATION_VERSION, RESULT_UNIT, SETTINGS, SHARE_TO_YUAN
from instrument import infer_exchange, instrument_id, normalize_code


def _to_num(value):
    return pd.to_numeric(value, errors="coerce")


def _retry(fn, label=""):
    last = None
    for attempt in range(int(SETTINGS["retry_count"])):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt + 1 < int(SETTINGS["retry_count"]):
                time.sleep(float(SETTINGS["retry_base_seconds"]) * (attempt + 1))
    raise RuntimeError(f"{label} 连续失败: {last}")


def _optional(fn, label: str, warnings: list[str]) -> pd.DataFrame:
    try:
        value = _retry(fn, label)
        return value.copy() if value is not None else pd.DataFrame()
    except Exception as exc:
        warnings.append(f"{label} 不可用: {exc}")
        return pd.DataFrame()


def _nav_cols(frame: pd.DataFrame):
    found = []
    for col in frame.columns:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})-单位净值$", str(col))
        if match:
            found.append((match.group(1), col))
    return sorted(found, reverse=True)


def _safe_code(value):
    try:
        return normalize_code(value)
    except ValueError:
        return None


def _date_text(value):
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.strftime("%Y-%m-%d")


def _prepare_daily(raw: pd.DataFrame, expected_date: str | None = None) -> tuple[pd.DataFrame, str]:
    nav_cols = _nav_cols(raw)
    if not nav_cols:
        raise RuntimeError("ETF 净值表未找到日期化单位净值列")
    all_dates = [d for d, _ in nav_cols]
    # 优先使用请求交易日对应的净值列，避免数据已更新到更新日期时日期不匹配
    if expected_date and expected_date in all_dates:
        trade_date = expected_date
        nav_cols = [(d, c) for d, c in nav_cols if d == expected_date] + \
                   [(d, c) for d, c in nav_cols if d != expected_date]
    else:
        trade_date = all_dates[0]
    daily = raw.rename(columns={
        "基金代码": "code", "基金简称": "daily_name", "市价": "nav_market",
        "类型": "fund_type",
    }).copy()
    if not {"code", "daily_name"}.issubset(daily.columns):
        raise RuntimeError("ETF 净值表缺少基金代码或基金简称")
    daily["code"] = daily["code"].map(_safe_code)
    daily = daily.dropna(subset=["code"])
    nav = pd.Series(pd.NA, index=daily.index, dtype="Float64")
    valuation_date = pd.Series(pd.NA, index=daily.index, dtype="string")
    for date_text, col in nav_cols:
        values = _to_num(daily[col])
        use = nav.isna() & values.notna()
        nav.loc[use] = values.loc[use]
        valuation_date.loc[use] = date_text
    daily["unit_nav"] = nav
    daily["valuation_date"] = valuation_date
    daily["nav_market"] = _to_num(daily.get("nav_market"))
    keep = ["code", "daily_name", "fund_type", "unit_nav", "valuation_date", "nav_market"]
    return daily[keep].drop_duplicates("code", keep="first"), trade_date


def _prepare_spot(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame(columns=[
            "code", "spot_name", "close", "spot_previous_close", "pct_change", "volume", "amount",
            "spot_shares_raw", "spot_shares_date", "spot_updated_at",
        ])
    spot = raw.rename(columns={
        "代码": "code", "名称": "spot_name", "最新份额": "spot_shares_raw",
        "涨跌幅": "pct_change", "最新价": "close", "昨收": "spot_previous_close", "成交量": "volume",
        "成交额": "amount", "数据日期": "spot_shares_date",
        "更新时间": "spot_updated_at",
    }).copy()
    required = {"code", "spot_name"}
    if not required.issubset(spot.columns):
        raise RuntimeError(f"ETF 快照缺列: {sorted(required - set(spot.columns))}")
    spot["code"] = spot["code"].map(_safe_code)
    spot = spot.dropna(subset=["code"])
    for col in ["spot_shares_raw", "pct_change", "close", "spot_previous_close", "volume", "amount"]:
        spot[col] = _to_num(spot[col]) if col in spot else pd.NA
    if "spot_shares_date" not in spot:
        spot["spot_shares_date"] = None
    spot["spot_shares_date"] = spot["spot_shares_date"].map(_date_text)
    if "spot_updated_at" not in spot:
        spot["spot_updated_at"] = None
    spot["spot_updated_at"] = spot["spot_updated_at"].map(
        lambda x: None if pd.isna(x) else str(x)
    )
    keep = [
        "code", "spot_name", "close", "spot_previous_close", "pct_change", "volume", "amount",
        "spot_shares_raw", "spot_shares_date", "spot_updated_at",
    ]
    return spot[keep].drop_duplicates("code", keep="first")


def _prepare_sse(raw: pd.DataFrame, expected_date: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    shares = raw.rename(columns={
        "基金代码": "code", "基金简称": "exchange_name", "基金份额": "exchange_shares_raw",
        "统计日期": "exchange_shares_date",
    }).copy()
    required = {"code", "exchange_shares_raw", "exchange_shares_date"}
    if not required.issubset(shares.columns):
        raise RuntimeError(f"上交所份额缺列: {sorted(required - set(shares.columns))}")
    shares["code"] = shares["code"].map(_safe_code)
    shares = shares.dropna(subset=["code"])
    shares["exchange_shares_raw"] = _to_num(shares["exchange_shares_raw"])
    shares["exchange_shares_date"] = shares["exchange_shares_date"].map(_date_text)
    shares["exchange_shares_source"] = "AKShare:SSE"
    shares["exchange_shares_unit"] = "份"
    shares["exchange_shares_factor"] = 1.0
    shares["exchange_updated_at"] = db.now_cn()
    # 接口允许按历史日期请求；返回日期仍必须与请求日完全一致。
    shares.loc[shares["exchange_shares_date"] != expected_date, "exchange_shares_raw"] = pd.NA
    return shares[[
        "code", "exchange_name", "exchange_shares_raw", "exchange_shares_date",
        "exchange_shares_source", "exchange_shares_unit", "exchange_shares_factor",
        "exchange_updated_at",
    ]].drop_duplicates("code", keep="first")


def _prepare_szse(raw: pd.DataFrame, expected_date: str,
                  latest_completed: str) -> pd.DataFrame:
    if raw.empty or expected_date != latest_completed:
        return pd.DataFrame()
    shares = raw.rename(columns={
        "基金代码": "code", "基金简称": "exchange_name", "基金份额": "exchange_shares_raw",
        "基金类别": "exchange_fund_category",
    }).copy()
    required = {"code", "exchange_shares_raw"}
    if not required.issubset(shares.columns):
        raise RuntimeError(f"深交所份额缺列: {sorted(required - set(shares.columns))}")
    if "exchange_fund_category" in shares:
        shares = shares[
            shares["exchange_fund_category"].astype(str).str.contains("ETF", case=False, na=False)
        ]
    shares["code"] = shares["code"].map(_safe_code)
    shares = shares.dropna(subset=["code"])
    shares["exchange_shares_raw"] = _to_num(shares["exchange_shares_raw"])
    shares["exchange_shares_date"] = expected_date
    shares["exchange_shares_source"] = "AKShare:SZSE"
    shares["exchange_shares_unit"] = "份"
    shares["exchange_shares_factor"] = 1.0
    shares["exchange_updated_at"] = db.now_cn()
    if "exchange_name" not in shares:
        shares["exchange_name"] = None
    return shares[[
        "code", "exchange_name", "exchange_shares_raw", "exchange_shares_date",
        "exchange_shares_source", "exchange_shares_unit", "exchange_shares_factor",
        "exchange_updated_at",
    ]].drop_duplicates("code", keep="first")


def _fund_etf_scale_szse_fixed() -> pd.DataFrame:
    """修复 akshare fund_etf_scale_szse 的 bytes 返回值 bug。

    akshare 1.18.81 直接将 requests.get().content（bytes）传给 pd.read_excel，
    在 pandas 2.x + openpyxl 环境下报错。这里用 io.BytesIO 包装。
    """
    url = "https://fund.szse.cn/api/report/ShowReport"
    params = {
        "SHOWTYPE": "xlsx",
        "CATALOGID": "1000_lf",
        "TABKEY": "tab1",
        "random": "0.07610353191740105",
    }
    headers = {
        "Referer": "https://fund.szse.cn/marketdata/fundslist/index.html",
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/88.0.4324.150 Safari/537.36"),
    }
    r = requests.get(url, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    temp_df = pd.read_excel(io.BytesIO(r.content), engine="openpyxl", dtype={"基金代码": str})
    if "当前规模(份)" in temp_df.columns:
        temp_df.rename(columns={"当前规模(份)": "基金份额"}, inplace=True)
    # 与 akshare 原版一致：清理千位分隔符并转数值
    if "基金份额" in temp_df.columns:
        temp_df["基金份额"] = (
            temp_df["基金份额"].astype(str).str.replace(",", "", regex=False)
        )
        temp_df["基金份额"] = pd.to_numeric(temp_df["基金份额"], errors="coerce")
    keep = ["基金代码", "基金简称", "基金类别", "基金份额"]
    keep = [c for c in keep if c in temp_df.columns]
    return temp_df[keep]


def _combine_exchange(sse: pd.DataFrame, szse: pd.DataFrame) -> pd.DataFrame:
    available = [frame for frame in [sse, szse] if frame is not None and not frame.empty]
    if not available:
        return pd.DataFrame()
    return pd.concat(available, ignore_index=True).drop_duplicates("code", keep="first")


def fetch_staging(expected_date: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame, str, list[str]]:
    db.migrate(create_backup=False)
    warnings: list[str] = []
    daily_raw = _retry(ak.fund_etf_fund_daily_em, "fund_etf_fund_daily_em")
    daily, trade_date = _prepare_daily(daily_raw, expected_date)
    expected_date = expected_date or trade_date
    if trade_date != expected_date:
        raise RuntimeError(f"净值日期 {trade_date} 与请求交易日 {expected_date} 不一致")

    spot = _prepare_spot(_optional(ak.fund_etf_spot_em, "fund_etf_spot_em", warnings))
    sse_fn = getattr(ak, "fund_etf_scale_sse", None)
    if sse_fn is None:
        raise RuntimeError("AKShare 版本不包含上交所 ETF 份额接口，请安装 1.18.81")
    sse = _prepare_sse(
        _optional(lambda: sse_fn(date=expected_date.replace("-", "")),
                  "fund_etf_scale_sse", warnings),
        expected_date,
    )
    latest_completed = calendar_service.latest_completed_trade_date()
    # 先尝试 akshare 原始接口，失败（bytes bug）时用修复版
    szse_fn = getattr(ak, "fund_etf_scale_szse", None)

    def _szse_with_fallback():
        if szse_fn:
            try:
                return szse_fn()
            except Exception:
                pass
        return _fund_etf_scale_szse_fixed()

    szse = _prepare_szse(
        _optional(_szse_with_fallback, "fund_etf_scale_szse", warnings),
        expected_date,
        latest_completed,
    )
    exchange = _combine_exchange(sse, szse)

    stage = daily.merge(spot, on="code", how="outer")
    if not exchange.empty:
        stage = stage.merge(exchange, on="code", how="outer")
    else:
        for col in [
            "exchange_name", "exchange_shares_raw", "exchange_shares_date",
            "exchange_shares_source", "exchange_shares_unit",
            "exchange_shares_factor", "exchange_updated_at",
        ]:
            stage[col] = pd.NA
    stage["code"] = stage["code"].map(_safe_code)
    stage = stage.dropna(subset=["code"]).drop_duplicates("code", keep="first")
    stage["exchange"] = stage["code"].map(infer_exchange)
    stage["name"] = (
        stage.get("daily_name").fillna(stage.get("spot_name"))
        .fillna(stage.get("exchange_name")).fillna(stage["code"])
    )
    stage["fund_type"] = stage.get("fund_type").fillna("未知")
    # 次日盘前运行时，实时快照的“最新价/涨跌幅”属于新交易日，不能写回目标日。
    # 对最近已结束交易日使用快照里的“昨收”，其他历史日期仅使用净值接口市价。
    completed_quote = expected_date == latest_completed
    previous_close_quote = _to_num(stage.get("spot_previous_close"))
    if not completed_quote:
        previous_close_quote = pd.Series(pd.NA, index=stage.index, dtype="Float64")
    stage["close"] = (
        previous_close_quote.fillna(_to_num(stage.get("nav_market")))
    )
    stage["pct_change"] = pd.NA
    stage["volume"] = pd.NA
    stage["amount"] = pd.NA
    stage["unit_nav"] = _to_num(stage.get("unit_nav"))

    exchange_valid = (
        stage["exchange_shares_raw"].notna()
        & (stage["exchange_shares_date"] == expected_date)
    )
    spot_valid = (
        stage["spot_shares_raw"].notna()
        & (stage["spot_shares_date"] == expected_date)
    )
    stage["shares_raw"] = stage["exchange_shares_raw"].where(
        exchange_valid, stage["spot_shares_raw"]
    )
    stage["shares_unit"] = stage["exchange_shares_unit"].where(exchange_valid, "份")
    stage["shares_unit_factor"] = stage["exchange_shares_factor"].where(exchange_valid, 1.0)
    stage["shares_date"] = stage["exchange_shares_date"].where(
        exchange_valid, stage["spot_shares_date"]
    )
    stage["shares_source"] = stage["exchange_shares_source"].where(
        exchange_valid, "AKShare:EM"
    )
    stage["shares_updated_at"] = stage["exchange_updated_at"].where(
        exchange_valid, stage["spot_updated_at"]
    )
    stage["shares"] = (
        stage["shares_raw"] * stage["shares_unit_factor"]
    ).where(exchange_valid | spot_valid)

    comparable = exchange_valid & spot_valid & stage["spot_shares_raw"].notna()
    denominator = stage["exchange_shares_raw"].where(stage["exchange_shares_raw"] > 0)
    stage["share_source_deviation"] = (
        (stage["exchange_shares_raw"] - stage["spot_shares_raw"]).abs() / denominator
    ).where(comparable)

    stage["instrument_id"] = stage.apply(
        lambda row: instrument_id(row["code"], row["exchange"]), axis=1
    )
    stage = classify_frame(stage, name_col="name", type_col="fund_type")
    stage = stage.rename(columns={
        "category": "primary_category", "sub_industry": "secondary_category",
    })

    stamp = db.now_cn()
    instruments = pd.DataFrame({
        "instrument_id": stage["instrument_id"],
        "code": stage["code"],
        "exchange": stage["exchange"],
        "name": stage["name"],
        "fund_type": stage["fund_type"],
        "list_date": None,
        "delist_date": None,
        "primary_category": stage["primary_category"],
        "secondary_category": stage["secondary_category"],
        "tags_json": "[]",
        "track_index_code": None,
        "track_index_name": None,
        "source": "AKShare",
        "first_seen_date": trade_date,
        "last_seen_date": trade_date,
        "active": 1,
        "classification_version": CLASSIFICATION_VERSION,
        "updated_at": stamp,
    })

    fact_columns = [
        "instrument_id", "close", "pct_change", "volume", "amount", "unit_nav",
        "valuation_date", "shares", "shares_raw", "shares_unit",
        "shares_unit_factor", "shares_date", "shares_source", "shares_updated_at",
        "share_source_deviation",
    ]
    facts = stage[fact_columns].copy()
    facts.insert(0, "trade_date", trade_date)
    previous = db.query(
        """SELECT f.instrument_id,f.shares,f.unit_nav,f.trade_date
           FROM etf_daily_fact f
           JOIN (
               SELECT instrument_id,MAX(trade_date) d
               FROM etf_daily_fact
               WHERE trade_date<? AND shares IS NOT NULL AND shares_date=trade_date
               GROUP BY instrument_id
           ) p ON p.instrument_id=f.instrument_id AND p.d=f.trade_date""",
        (trade_date,),
    ).rename(columns={
        "shares": "prev_shares", "unit_nav": "prev_nav",
        "trade_date": "prev_trade_date",
    })
    facts = facts.merge(previous, on="instrument_id", how="left")
    previous_close = db.query(
        """SELECT f.instrument_id,f.close prev_close
           FROM etf_daily_fact f
           JOIN (
               SELECT instrument_id,MAX(trade_date) d
               FROM etf_daily_fact
               WHERE trade_date<? AND close IS NOT NULL
               GROUP BY instrument_id
           ) p ON p.instrument_id=f.instrument_id AND p.d=f.trade_date""",
        (trade_date,),
    )
    facts = facts.merge(previous_close, on="instrument_id", how="left")
    for col in ["prev_close", "prev_shares", "prev_nav"]:
        facts[col] = _to_num(facts[col])
    computed_change = (facts["close"] / facts["prev_close"] - 1) * 100
    facts["pct_change"] = computed_change.where(
        facts["close"].notna() & facts["prev_close"].notna() & (facts["prev_close"] > 0)
    )

    current_share = facts["shares"].notna() & (facts["shares_date"] == trade_date)
    previous_share = facts["prev_shares"].notna() & facts["prev_trade_date"].notna()
    current_nav = facts["unit_nav"].notna() & (facts["valuation_date"] == trade_date)
    facts["previous_aum"] = (
        facts["prev_shares"] * facts["prev_nav"] * SHARE_TO_YUAN
    ).where(previous_share & facts["prev_nav"].notna() & (facts["prev_nav"] > 0))
    share_ratio = facts["shares"] / facts["prev_shares"]
    nav_ratio = facts["unit_nav"] / facts["prev_nav"]
    split_like = (
        current_share & previous_share & current_nav
        & ((share_ratio >= 1.25) | (share_ratio <= 0.80))
        & ((share_ratio * nav_ratio - 1).abs() <= 0.15)
    )
    ready = (
        current_share & previous_share & current_nav
        & (facts["previous_aum"] > 0) & ~split_like
    )
    facts["estimated_net_flow"] = (
        (facts["shares"] - facts["prev_shares"]) * facts["unit_nav"]
        * SHARE_TO_YUAN / RESULT_UNIT
    ).where(ready)
    facts["flow_rate"] = (
        facts["estimated_net_flow"] * RESULT_UNIT / facts["previous_aum"]
    ).where(ready)
    facts["flow_status"] = "SOURCE_MISSING"
    facts.loc[facts["shares"].notna() & ~current_share, "flow_status"] = "DATE_MISMATCH"
    facts.loc[current_share & ~previous_share, "flow_status"] = "BASELINE"
    facts.loc[current_share & previous_share & ~current_nav, "flow_status"] = "DATE_MISMATCH"
    facts.loc[ready, "flow_status"] = "VALID"
    facts.loc[split_like, "flow_status"] = "ANOMALOUS"
    facts.loc[split_like, ["previous_aum", "estimated_net_flow", "flow_rate", "pct_change"]] = pd.NA
    if split_like.any():
        warnings.append(f"检测到 {int(split_like.sum())} 只 ETF 疑似拆分/份额折算，已从资金流和收益统计剔除")
    facts["data_status"] = (
        ready & facts["close"].notna()
    ).map({True: "VALID", False: "PARTIAL"})
    facts["source"] = "AKShare"
    facts["collected_at"] = stamp
    facts = facts.drop(columns=["prev_shares", "prev_nav", "prev_close", "prev_trade_date"])
    return instruments, facts, trade_date, warnings


def compute_and_store(expected_date: str, run_id: str, benchmark=None,
                      force_refresh: bool = False) -> dict:
    instruments, facts, data_date, source_warnings = fetch_staging(expected_date)
    if data_date != expected_date:
        raise RuntimeError(f"数据日期 {data_date} 与请求交易日 {expected_date} 不一致")
    # 诊断输出：帮助定位覆盖率不足的具体原因
    n = len(facts)
    if n:
        shares_date_dist = facts["shares_date"].value_counts(dropna=False).to_dict()
        shares_source_dist = facts["shares_source"].value_counts(dropna=False).to_dict()
        close_valid = int(facts["close"].notna().sum())
        shares_valid = int(facts["shares"].notna().sum())
        unit_nav_valid = int(facts["unit_nav"].notna().sum())
        print(f"[fetch] pool={n} close={close_valid} shares={shares_valid} unit_nav={unit_nav_valid}")
        print(f"[fetch] shares_date={shares_date_dist}")
        print(f"[fetch] shares_source={shares_source_dist}")
    if source_warnings:
        print(f"[fetch] source_warnings={source_warnings}")
    issues, stats = quality.validate_snapshot(instruments, facts, expected_date)
    category_metrics = metrics.compute_metrics(
        facts, instruments, expected_date, benchmark=benchmark
    )
    quality.validate_metrics(facts, instruments, category_metrics, expected_date)
    db.upsert_snapshot(instruments, facts, category_metrics)
    db.record_quality_issues(run_id, issues)
    return {
        "instruments": instruments, "facts": facts, "metrics": category_metrics,
        "issues": issues, "source_warnings": source_warnings, **stats,
    }

"""AKShare 全量 ETF staging、标准化、估算资金流与事务入库。"""
from __future__ import annotations

import json
import re
import time

import akshare as ak
import pandas as pd

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


def _nav_cols(frame: pd.DataFrame):
    found = []
    for col in frame.columns:
        match = re.match(r"^(\d{4}-\d{2}-\d{2})-单位净值$", str(col))
        if match:
            found.append((match.group(1), col))
    return sorted(found, reverse=True)


def fetch_staging() -> tuple[pd.DataFrame, pd.DataFrame, str]:
    spot = _retry(ak.fund_etf_spot_em, "fund_etf_spot_em").copy()
    spot = spot.rename(columns={
        "代码": "code", "名称": "spot_name", "最新份额": "shares",
        "涨跌幅": "pct_change", "最新价": "close", "成交量": "volume",
        "成交额": "amount",
    })
    required = {"code", "spot_name"}
    if not required.issubset(spot.columns):
        raise RuntimeError(f"ETF 快照缺列: {sorted(required - set(spot.columns))}")
    for col in ["shares", "pct_change", "close", "volume", "amount"]:
        spot[col] = _to_num(spot[col]) if col in spot else pd.NA
    spot["code"] = spot["code"].map(normalize_code)
    spot = spot.drop_duplicates("code", keep="first")

    daily = _retry(ak.fund_etf_fund_daily_em, "fund_etf_fund_daily_em").copy()
    nav_cols = _nav_cols(daily)
    if not nav_cols:
        raise RuntimeError("ETF 净值表未找到日期化单位净值列")
    trade_date = nav_cols[0][0]
    daily = daily.rename(columns={
        "基金代码": "code", "基金简称": "daily_name", "市价": "nav_market",
        "类型": "fund_type",
    })
    daily["code"] = daily["code"].map(normalize_code)
    nav = pd.Series(pd.NA, index=daily.index, dtype="Float64")
    valuation_date = pd.Series(pd.NA, index=daily.index, dtype="string")
    for date_text, col in nav_cols:
        values = _to_num(daily[col])
        use = nav.isna() & values.notna()
        nav.loc[use] = values.loc[use]
        valuation_date.loc[use] = date_text
    daily["unit_nav"] = nav
    daily["valuation_date"] = valuation_date
    keep = ["code", "daily_name", "fund_type", "unit_nav", "valuation_date"]
    daily = daily[keep].drop_duplicates("code", keep="first")

    stage = spot.merge(daily, on="code", how="left")
    stage["name"] = stage["daily_name"].fillna(stage["spot_name"])
    stage["fund_type"] = stage["fund_type"].fillna("未知")
    stage["exchange"] = stage["code"].map(infer_exchange)
    stage["instrument_id"] = stage.apply(
        lambda r: instrument_id(r["code"], r["exchange"]), axis=1
    )
    stage = classify_frame(stage, name_col="name", type_col="fund_type")
    stage = stage.rename(columns={"category": "primary_category",
                                  "sub_industry": "secondary_category"})

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

    facts = stage[[
        "instrument_id", "close", "pct_change", "volume", "amount", "unit_nav",
        "valuation_date", "shares",
    ]].copy()
    facts.insert(0, "trade_date", trade_date)
    previous = db.query(
        """SELECT f.instrument_id,f.shares,f.unit_nav FROM etf_daily_fact f
           JOIN (SELECT instrument_id,MAX(trade_date) d FROM etf_daily_fact
                 WHERE trade_date<? GROUP BY instrument_id) p
             ON p.instrument_id=f.instrument_id AND p.d=f.trade_date""",
        (trade_date,),
    )
    previous = previous.rename(columns={"shares": "prev_shares", "unit_nav": "prev_nav"})
    facts = facts.merge(previous, on="instrument_id", how="left")
    facts["previous_aum"] = facts["prev_shares"] * facts["prev_nav"] * SHARE_TO_YUAN
    facts["estimated_net_flow"] = (
        (facts["shares"] - facts["prev_shares"]) * facts["unit_nav"]
        * SHARE_TO_YUAN / RESULT_UNIT
    )
    facts["flow_rate"] = (
        facts["estimated_net_flow"] * RESULT_UNIT
        / facts["previous_aum"].where(facts["previous_aum"] > 0)
    )
    full = facts[["close", "shares", "unit_nav"]].notna().all(axis=1)
    current_nav = facts["valuation_date"] == trade_date
    flow_ready = facts["estimated_net_flow"].notna()
    facts["data_status"] = (full & current_nav & flow_ready).map(
        {True: "VALID", False: "PARTIAL"}
    )
    facts["source"] = "AKShare"
    facts["collected_at"] = stamp
    facts = facts.drop(columns=["prev_shares", "prev_nav"])
    return instruments, facts, trade_date


def compute_and_store(expected_date: str, run_id: str, benchmark=None,
                      force_refresh: bool = False) -> dict:
    instruments, facts, data_date = fetch_staging()
    if data_date != expected_date:
        raise RuntimeError(f"数据日期 {data_date} 与请求交易日 {expected_date} 不一致")
    issues, stats = quality.validate_snapshot(instruments, facts, expected_date)
    category_metrics = metrics.compute_metrics(
        facts, instruments, expected_date, benchmark=benchmark
    )
    db.upsert_snapshot(instruments, facts, category_metrics)
    db.record_quality_issues(run_id, issues)
    return {
        "instruments": instruments, "facts": facts, "metrics": category_metrics,
        "issues": issues, **stats,
    }

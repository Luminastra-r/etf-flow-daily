"""按完整交易日窗口计算分类资金、价格、广度和异常指标。"""
from __future__ import annotations

import math

import pandas as pd

import db
from config import PERIOD_WINDOWS, SETTINGS


METRIC_COLUMNS = [
    "trade_date", "category", "window", "available_days", "estimated_net_flow",
    "flow_rate", "price_return", "equal_weight_return", "benchmark_return",
    "relative_return", "breadth",
    "inflow_count", "outflow_count", "valid_count", "missing_count",
    "top1_concentration", "top3_concentration", "inflow_streak", "flow_zscore",
    "observation_status",
]


def _safe_sum(series: pd.Series):
    return series.sum(min_count=1)


def _observation(flow_rate, price_return):
    if pd.isna(flow_rate) or pd.isna(price_return):
        return "历史不足"
    f0 = float(SETTINGS["neutral_flow_rate"])
    p0 = float(SETTINGS["neutral_price_return"])
    if abs(flow_rate) <= f0 or abs(price_return) <= p0:
        return "中性"
    if price_return > 0 and flow_rate > 0:
        return "资金价格共振"
    if price_return < 0 and flow_rate > 0:
        return "逆势承接"
    if price_return > 0 and flow_rate < 0:
        return "上涨背离"
    return "共同走弱"


def _weighted_period_return(group: pd.DataFrame):
    rows = []
    for _, item in group.groupby("instrument_id"):
        item = item.sort_values("trade_date")
        closes = item.dropna(subset=["close"])
        if len(closes) < 2:
            if len(group["trade_date"].unique()) == 1 and item["pct_change"].notna().any():
                ret = item["pct_change"].dropna().iloc[-1] / 100
            else:
                continue
        else:
            first, last = closes.iloc[0], closes.iloc[-1]
            if not first["close"]:
                continue
            ret = last["close"] / first["close"] - 1
        weight = item["previous_aum"].dropna()
        rows.append((ret, float(weight.iloc[0]) if not weight.empty else 1.0))
    if not rows:
        return None
    denom = sum(max(w, 0) for _, w in rows)
    return sum(r * max(w, 0) for r, w in rows) / denom if denom else None


def _equal_weight_period_return(group: pd.DataFrame):
    returns = []
    for _, item in group.groupby("instrument_id"):
        item = item.sort_values("trade_date")
        closes = item.dropna(subset=["close"])
        if len(closes) < 2:
            values = item["pct_change"].dropna()
            if len(group["trade_date"].unique()) == 1 and not values.empty:
                returns.append(float(values.iloc[-1]) / 100)
        elif closes.iloc[0]["close"]:
            returns.append(closes.iloc[-1]["close"] / closes.iloc[0]["close"] - 1)
    return sum(returns) / len(returns) if returns else None


def _benchmark_return(benchmark: pd.DataFrame, dates: list[str], window: int):
    if benchmark is None or benchmark.empty:
        return None
    b = benchmark[benchmark["date"].isin(dates)].sort_values("date")
    if window == 1:
        prior = benchmark[benchmark["date"] <= dates[-1]].sort_values("date").tail(2)
        if len(prior) < 2:
            return None
        return prior.iloc[-1]["close"] / prior.iloc[0]["close"] - 1
    if len(b) < window or not b.iloc[0]["close"]:
        return None
    return b.iloc[-1]["close"] / b.iloc[0]["close"] - 1


def compute_metrics(current_facts: pd.DataFrame, instruments: pd.DataFrame,
                    trade_date: str, benchmark: pd.DataFrame | None = None,
                    path=None) -> pd.DataFrame:
    old = db.query(
        """SELECT f.*,i.primary_category FROM etf_daily_fact f
           JOIN etf_instrument i USING(instrument_id) WHERE f.trade_date < ?""",
        (trade_date,), path=path,
    )
    current = current_facts.merge(
        instruments[["instrument_id", "primary_category"]], on="instrument_id", how="left"
    )
    history = current.copy() if old.empty else pd.concat([old, current], ignore_index=True)
    history = history.drop_duplicates(["trade_date", "instrument_id"], keep="last")
    dates = sorted(history["trade_date"].dropna().unique())
    active_counts = instruments.groupby("primary_category")["instrument_id"].nunique().to_dict()
    rows = []

    for category in sorted(instruments["primary_category"].unique()):
        cat = history[history["primary_category"] == category]
        daily_totals = (cat.groupby("trade_date")["estimated_net_flow"]
                        .agg(_safe_sum).sort_index())
        streak = 0
        for value in reversed(daily_totals.tolist()):
            if pd.notna(value) and value > 0:
                streak += 1
            else:
                break
        for window in PERIOD_WINDOWS:
            chosen_dates = dates[-window:]
            available = len(chosen_dates)
            subset = cat[cat["trade_date"].isin(chosen_dates)]
            complete = available == window
            flows = subset.dropna(subset=["estimated_net_flow"])
            flow_total = _safe_sum(flows["estimated_net_flow"]) if complete else None
            denom = subset["previous_aum"].sum(min_count=1) if complete else None
            flow_rate = (flow_total * 10000 / denom
                         if complete and pd.notna(flow_total) and pd.notna(denom) and denom > 0
                         else None)
            valid_count = int(flows["instrument_id"].nunique()) if complete else 0
            category_count = int(active_counts.get(category, 0))
            category_coverage = valid_count / category_count if category_count else 0
            if complete and category_coverage < float(SETTINGS["coverage_failure"]):
                flow_total = None
                flow_rate = None
            inflow_count = int((flows["estimated_net_flow"] > 0).sum()) if complete else 0
            outflow_count = int((flows["estimated_net_flow"] < 0).sum()) if complete else 0
            breadth = inflow_count / len(flows) if complete and len(flows) else None
            price_return = _weighted_period_return(subset) if complete else None
            equal_weight_return = _equal_weight_period_return(subset) if complete else None
            bench_return = _benchmark_return(benchmark, chosen_dates, window) if complete else None
            relative = price_return - bench_return if price_return is not None and bench_return is not None else None

            per_etf = flows.groupby("instrument_id")["estimated_net_flow"].sum().sort_values(ascending=False)
            threshold = float(SETTINGS["concentration_min_flow_wan"])
            if flow_total is not None and flow_total > threshold and not per_etf.empty:
                top1 = per_etf.head(1).clip(lower=0).sum() / flow_total
                top3 = per_etf.head(3).clip(lower=0).sum() / flow_total
            else:
                top1 = top3 = None

            zscore = None
            if window == 1 and complete:
                past = db.query(
                    """SELECT flow_rate FROM category_daily_metric
                       WHERE category=? AND window=1 AND trade_date<?
                         AND flow_rate IS NOT NULL ORDER BY trade_date DESC LIMIT ?""",
                    (category, trade_date, int(SETTINGS["zscore_window"])), path=path,
                )
                if len(past) == int(SETTINGS["zscore_window"]):
                    std = past["flow_rate"].std(ddof=0)
                    if std and flow_rate is not None:
                        zscore = (flow_rate - past["flow_rate"].mean()) / std

            rows.append({
                "trade_date": trade_date, "category": category, "window": window,
                "available_days": available, "estimated_net_flow": flow_total,
                "flow_rate": flow_rate, "price_return": price_return,
                "equal_weight_return": equal_weight_return,
                "benchmark_return": bench_return, "relative_return": relative,
                "breadth": breadth, "inflow_count": inflow_count if complete else None,
                "outflow_count": outflow_count if complete else None,
                "valid_count": valid_count if complete else None,
                "missing_count": max(active_counts.get(category, 0) - valid_count, 0) if complete else None,
                "top1_concentration": top1, "top3_concentration": top3,
                "inflow_streak": streak if complete else None, "flow_zscore": zscore,
                "observation_status": _observation(flow_rate, price_return),
            })
    return pd.DataFrame(rows, columns=METRIC_COLUMNS)

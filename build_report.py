"""从 SQLite 构建轻量静态站点；HTML 不内嵌历史数据。"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import db
import macro
import metrics as metric_engine
from config import (
    BUILD_DIR, CLASSIFICATION_VERSION, OUTPUT_DIR, PERIOD_WINDOWS, SETTINGS,
)
from presentation import ranking_layout

WEB_DIR = Path(__file__).resolve().parent / "web"


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if value is pd.NA or (isinstance(value, float) and pd.isna(value)):
        return None
    if hasattr(value, "item"):
        return _clean(value.item())
    return value


def _write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_clean(payload), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _latest_status(trade_date: str, pool_count: int, valid_count: int):
    run = db.query(
        """SELECT * FROM pipeline_run WHERE trade_date=? OR status='REBUILT'
           ORDER BY started_at DESC LIMIT 1""", (trade_date,)
    )
    if run.empty:
        return {
            "status": "PARTIAL", "scheduled_at": None, "generated_at": db.now_cn(),
            "warnings": ["历史数据来自 v1 迁移，尚无 v2 正式运行记录"],
        }
    row = run.iloc[0]
    try:
        warnings = json.loads(row["warnings_json"] or "[]")
    except json.JSONDecodeError:
        warnings = []
    return {
        "status": row["status"], "scheduled_at": row["scheduled_at"],
        "generated_at": row["finished_at"] or row["started_at"], "warnings": warnings,
    }


def _category_metrics(trade_date: str, facts: pd.DataFrame,
                      instruments: pd.DataFrame) -> pd.DataFrame:
    stored = db.query(
        "SELECT * FROM category_daily_metric WHERE trade_date=?", (trade_date,)
    )
    if not stored.empty:
        return stored
    current = facts.drop(columns=["primary_category", "secondary_category"], errors="ignore")
    return metric_engine.compute_metrics(current, instruments, trade_date)


def _period_facts(all_facts: pd.DataFrame, window: int):
    dates = sorted(all_facts["trade_date"].unique())
    selected = dates[-window:]
    return all_facts[all_facts["trade_date"].isin(selected)], len(selected)


def _rankings(all_facts: pd.DataFrame, window: int) -> list[dict]:
    period, available = _period_facts(all_facts, window)
    if available < window:
        return []
    grouped = (period[period["secondary_category"] != "未分类"]
               .groupby(["primary_category", "secondary_category"])
               .agg(
                   etf_count=("instrument_id", "nunique"),
                   estimated_net_flow=("estimated_net_flow",
                                       lambda s: s.sum(min_count=1)),
                   flow_rate=("flow_rate", "mean"),
                   price_return=("pct_change", "mean"),
               ).reset_index())
    output = []
    for category, part in grouped.groupby("primary_category"):
        layout = ranking_layout(part, int(SETTINGS["top_n"]))
        output.append({"category": category, **layout})
    return output


def _industry_matrix(all_facts: pd.DataFrame) -> list[dict]:
    output = []
    for window in PERIOD_WINDOWS:
        period, available = _period_facts(all_facts, window)
        if available < window:
            continue
        industry = period[
            (period["primary_category"] == "行业")
            & (period["secondary_category"] != "未分类")
        ]
        grouped = industry.groupby("secondary_category").agg(
            estimated_net_flow=("estimated_net_flow", lambda s: s.sum(min_count=1)),
            flow_rate=("flow_rate", "mean"),
            price_return=("pct_change", "mean"),
            inflow_count=("estimated_net_flow", lambda s: int((s > 0).sum())),
            valid_count=("estimated_net_flow", lambda s: int(s.notna().sum())),
            etf_count=("instrument_id", "nunique"),
        ).reset_index()
        grouped["breadth"] = grouped.apply(
            lambda r: r["inflow_count"] / r["valid_count"] if r["valid_count"] else None,
            axis=1,
        )
        newest = industry[industry["trade_date"] == industry["trade_date"].max()].copy()
        newest["representative_score"] = newest["previous_aum"].fillna(
            newest["amount"].fillna(0)
        )
        representative_map = {}
        for name, group in newest.groupby("secondary_category"):
            representative_map[name] = (
                group.sort_values("representative_score", ascending=False)
                .drop_duplicates("instrument_id")
                .head(int(SETTINGS["representative_etf_count"]))["name"].tolist()
            )
        for record in grouped.to_dict("records"):
            output.append({
                "window": window, **record,
                "representatives": representative_map.get(record["secondary_category"], []),
            })
    return output


def _market_payload(load_market: bool):
    if not load_market:
        return {"status": "测试模式", "series": {}}
    loaded = macro.load_all()
    series = {}
    for name, frame in loaded.items():
        if frame is None or frame.empty:
            series[name] = []
        else:
            series[name] = frame.tail(260).to_dict("records")
    return {"status": "AKShare 可用字段；DXY 取自 Yahoo Finance (DX-Y.NYB)", "series": series}


def instrument_status_counts(instruments: pd.DataFrame, all_facts: pd.DataFrame,
                             trade_date: str) -> dict:
    current = all_facts[all_facts["trade_date"] == trade_date]
    current_map = dict(zip(current["instrument_id"], current["data_status"]))
    ever = set(all_facts["instrument_id"])
    counts = {"VALID": 0, "PARTIAL": 0, "STALE": 0, "MISSING": 0}
    for iid in instruments["instrument_id"]:
        if iid in current_map:
            counts[current_map[iid]] += 1
        elif iid in ever:
            counts["STALE"] += 1
        else:
            counts["MISSING"] += 1
    return counts


def _validate_build(root: Path):
    required = [
        "index.html", "market.html", "methodology.html", "assets/style.css",
        "assets/app.js", "assets/charts.js", "data/latest.json",
        "data/overview.json", "data/category_latest.json",
        "data/industry_latest.json", "data/market_context.json",
    ]
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise RuntimeError(f"静态构建缺少文件: {missing}")
    for path in (root / "data").rglob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))
    html = (root / "index.html").read_text(encoding="utf-8")
    if "data/overview.json" not in (root / "assets/app.js").read_text(encoding="utf-8"):
        raise RuntimeError("首页脚本缺少 overview.json 引用")
    if "Plotly.newPlot" in html:
        raise RuntimeError("首页不应内嵌 Plotly 数据")


def _publish(root: Path):
    previous = OUTPUT_DIR.parent / ".output-previous"
    if previous.exists():
        shutil.rmtree(previous)
    if OUTPUT_DIR.exists():
        os.replace(OUTPUT_DIR, previous)
    try:
        os.replace(root, OUTPUT_DIR)
    except Exception:
        if previous.exists() and not OUTPUT_DIR.exists():
            os.replace(previous, OUTPUT_DIR)
        raise
    if previous.exists():
        shutil.rmtree(previous)


def build(trade_date: str | None = None, load_market: bool = True,
          publish: bool = True, status_override: dict | None = None) -> dict:
    db.migrate()
    trade_date = trade_date or db.get_latest_date()
    if not trade_date:
        raise RuntimeError("数据库无可构建交易日")
    instruments = db.query("SELECT * FROM etf_instrument WHERE active=1")
    all_facts = db.query(
        """SELECT f.*,i.primary_category,i.secondary_category,i.name,i.code
           FROM etf_daily_fact f JOIN etf_instrument i USING(instrument_id)
           WHERE f.trade_date<=? ORDER BY f.trade_date""", (trade_date,)
    )
    facts = all_facts[all_facts["trade_date"] == trade_date].copy()
    cat_metrics = _category_metrics(trade_date, facts, instruments)
    pool_count = len(instruments)
    valid_count = int(facts[["close", "shares", "unit_nav"]].notna().all(axis=1).sum())
    coverage = valid_count / pool_count if pool_count else 0
    status = status_override or _latest_status(trade_date, pool_count, valid_count)

    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    (BUILD_DIR / "assets").mkdir(parents=True)
    for name in ["index.html", "market.html", "methodology.html", "favicon.svg"]:
        shutil.copy2(WEB_DIR / name, BUILD_DIR / name)
    for name in ["style.css", "app.js", "charts.js"]:
        shutil.copy2(WEB_DIR / name, BUILD_DIR / "assets" / name)

    latest = {
        "trade_date": trade_date, "scheduled_at": status["scheduled_at"],
        "generated_at": status["generated_at"], "status": status["status"],
        "pool_label": "全量ETF池", "pool_count": pool_count,
        "valid_count": valid_count, "missing_count": pool_count - valid_count,
        "coverage": coverage, "classification_version": CLASSIFICATION_VERSION,
        "warnings": status["warnings"],
        "status_counts": instrument_status_counts(instruments, all_facts, trade_date),
    }
    category_records = cat_metrics.to_dict("records")
    overview = {
        "periods": list(PERIOD_WINDOWS), "default_period": SETTINGS["default_period"],
        "by_period": {
            str(window): [r for r in category_records if int(r["window"]) == window]
            for window in PERIOD_WINDOWS
        },
    }
    rankings = {str(w): _rankings(all_facts, w) for w in PERIOD_WINDOWS}
    industry = _industry_matrix(all_facts)
    history = (cat_metrics[cat_metrics["window"] == 1]
               .sort_values("trade_date").to_dict("records"))

    _write_json(BUILD_DIR / "data/latest.json", latest)
    _write_json(BUILD_DIR / "data/overview.json", overview)
    _write_json(BUILD_DIR / "data/category_latest.json", rankings)
    _write_json(BUILD_DIR / "data/industry_latest.json", industry)
    _write_json(BUILD_DIR / "data/market_context.json", _market_payload(load_market))
    year = trade_date[:4]
    _write_json(BUILD_DIR / f"data/history/category_{year}.json", history)
    _validate_build(BUILD_DIR)

    files = [p for p in BUILD_DIR.rglob("*") if p.is_file()]
    result = {
        "path": str(OUTPUT_DIR / "index.html"), "file_count": len(files),
        "total_bytes": sum(p.stat().st_size for p in files),
        "max_file": max(((p.stat().st_size, str(p.relative_to(BUILD_DIR))) for p in files),
                        default=(0, "")),
        "coverage": coverage,
    }
    if publish:
        _publish(BUILD_DIR)
    return result


if __name__ == "__main__":
    print(build())

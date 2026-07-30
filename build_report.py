"""从 SQLite 构建轻量静态站点；HTML 不内嵌历史数据。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
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
DISPLAY_CATEGORIES = ["宽基", "行业", "港股", "Smart Beta", "海外", "固收", "商品", "货币"]
MARKET_DEFINITIONS = {
    "index": {"field": "close", "label": "沪深300", "source": "AKShare"},
    "valuation": {"field": "pe_ttm", "label": "沪深300 PE-TTM", "source": "AKShare"},
    "bond": {"field": "spread", "label": "中美十年期利差", "source": "AKShare"},
    "margin": {"field": "margin", "label": "融资余额", "source": "AKShare"},
    "dxy": {"field": "dxy", "label": "美元指数", "source": "Yahoo Finance / AKShare"},
}


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
                   price_return=("pct_change", lambda s: s.mean() / 100),
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
            price_return=("pct_change", lambda s: s.mean() / 100),
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


def _previous_market_payload() -> dict:
    payloads = []
    path = OUTPUT_DIR / "data" / "market_context.json"
    if path.is_file():
        try:
            payloads.append(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    # v2 -> v3 首次发布时，从当前远端基线继承已经验证过的市场序列。
    try:
        result = subprocess.run(
            ["git", "show", "origin/main:output/data/market_context.json"],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        payloads.append(json.loads(result.stdout))
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        pass
    converted = {}
    for payload in payloads:
        raw = payload.get("series", [])
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict) and item.get("key"):
                    converted.setdefault(item["key"], item)
            continue
        if isinstance(raw, dict):
            for key, rows in raw.items():
                if key not in MARKET_DEFINITIONS or not rows or key in converted:
                    continue
                definition = MARKET_DEFINITIONS[key]
                last_date = rows[-1].get("date")
                converted[key] = {
                    "key": key, **definition, "state": "VALID",
                    "as_of": str(last_date)[:10] if last_date else None,
                    "data": rows,
                }
    return converted


def _market_payload(load_market: bool):
    if not load_market:
        return {"status": "测试模式", "series": []}
    loaded = macro.load_all()
    previous = _previous_market_payload()
    health_rows = db.source_health()
    health_map = (
        health_rows.set_index("field_key").to_dict("index")
        if not health_rows.empty else {}
    )
    series = []
    for name, definition in MARKET_DEFINITIONS.items():
        frame = loaded.get(name, pd.DataFrame())
        if frame is not None and not frame.empty:
            value_date = pd.to_datetime(frame["date"], errors="coerce").max()
            value_date_text = None if pd.isna(value_date) else value_date.date().isoformat()
            health = db.record_source_health(
                name, definition["source"], True, value_date=value_date_text,
                details={"rows": len(frame)},
            )
            series.append({
                "key": name, **definition, "state": "VALID",
                "as_of": value_date_text, "data": frame.tail(260).to_dict("records"),
                "health": health,
            })
            continue
        health = db.record_source_health(
            name, definition["source"], False, error="本次构建未取得有效记录",
        )
        cached = previous.get(name)
        old_health = health_map.get(name, {})
        if cached and not old_health.get("last_success_at"):
            db.record_source_health(
                name, definition["source"], True,
                value_date=cached.get("as_of"), details={"bootstrap": "v2 static output"},
            )
            health = db.record_source_health(
                name, definition["source"], False, error="本次构建未取得有效记录",
            )
        if health["active"] and cached and cached.get("data"):
            series.append({
                **cached, **definition, "state": "STALE", "health": health,
            })
    return {
        "status": "仅展示健康字段；临时失败标记 STALE，连续失败字段自动下线",
        "series": series,
    }


def _daily_table(trade_date: str, instruments: pd.DataFrame,
                 facts: pd.DataFrame, generated_at: str) -> dict:
    joined = instruments[[
        "instrument_id", "primary_category", "secondary_category",
    ]].merge(
        facts[[
            "instrument_id", "estimated_net_flow", "pct_change", "flow_status",
        ]],
        on="instrument_id", how="left",
    )
    classified = joined[joined["primary_category"].isin(DISPLAY_CATEGORIES)].copy()
    valid = classified[
        classified["flow_status"].eq("VALID")
        & classified["estimated_net_flow"].notna()
    ]
    total_count = len(classified)
    total_valid = len(valid)
    total_coverage = total_valid / total_count if total_count else 0
    total_flow = (
        valid["estimated_net_flow"].sum(min_count=1)
        if total_coverage >= float(SETTINGS["coverage_failure"]) else None
    )
    equal_return = (
        classified["pct_change"].dropna().mean() / 100
        if classified["pct_change"].notna().any() else None
    )

    categories = []
    for category in DISPLAY_CATEGORIES:
        group = classified[classified["primary_category"] == category]
        count = len(group)
        if not count:
            continue
        group_valid = group[
            group["flow_status"].eq("VALID")
            & group["estimated_net_flow"].notna()
        ]
        valid_count = len(group_valid)
        coverage = valid_count / count
        category_flow = (
            group_valid["estimated_net_flow"].sum(min_count=1)
            if coverage >= float(SETTINGS["coverage_failure"]) else None
        )
        category_return = (
            group["pct_change"].dropna().mean() / 100
            if group["pct_change"].notna().any() else None
        )
        themes = []
        themed = group[group["secondary_category"] != "未分类"]
        for theme, part in themed.groupby("secondary_category"):
            theme_valid = part[
                part["flow_status"].eq("VALID")
                & part["estimated_net_flow"].notna()
            ]
            theme_coverage = len(theme_valid) / len(part)
            themes.append({
                "theme": theme,
                "etf_count": len(part),
                "flow_valid_count": len(theme_valid),
                "flow_coverage": theme_coverage,
                "estimated_net_flow_wan": (
                    theme_valid["estimated_net_flow"].sum(min_count=1)
                    if theme_coverage >= float(SETTINGS["coverage_failure"]) else None
                ),
                "equal_weight_return": (
                    part["pct_change"].dropna().mean() / 100
                    if part["pct_change"].notna().any() else None
                ),
            })
        ranked = [item for item in themes if item["estimated_net_flow_wan"] is not None]
        ranked.sort(key=lambda item: (-item["estimated_net_flow_wan"], item["theme"]))
        if len(themes) < 6:
            mode = "full"
            full_ranking = ranked
            top_inflows, top_outflows = [], []
        else:
            mode = "split"
            full_ranking = []
            top_inflows = [item for item in ranked if item["estimated_net_flow_wan"] > 0][:3]
            selected = {item["theme"] for item in top_inflows}
            top_outflows = sorted(
                [
                    item for item in ranked
                    if item["estimated_net_flow_wan"] < 0 and item["theme"] not in selected
                ],
                key=lambda item: (item["estimated_net_flow_wan"], item["theme"]),
            )[:3]
        if valid_count == 0 and group["flow_status"].eq("BASELINE").any():
            status = "BASELINE"
        elif coverage >= float(SETTINGS["coverage_warning"]):
            status = "VALID"
        else:
            status = "PARTIAL"
        categories.append({
            "category": category,
            "etf_count": count,
            "flow_valid_count": valid_count,
            "flow_coverage": coverage,
            "estimated_net_flow_wan": category_flow,
            "equal_weight_return": category_return,
            "status": status,
            "ranking_mode": mode,
            "full_ranking": full_ranking,
            "top_inflows": top_inflows,
            "top_outflows": top_outflows,
        })
    flow_status = (
        "VALID" if total_coverage >= float(SETTINGS["coverage_failure"])
        else "BASELINE" if classified["flow_status"].eq("BASELINE").any()
        else "PARTIAL"
    )
    return {
        "trade_date": trade_date,
        "generated_at": generated_at,
        "flow_status": flow_status,
        "total_etf_count": len(instruments),
        "classified_count": total_count,
        "unclassified_count": int(
            (instruments["primary_category"] == "未分类").sum()
        ),
        "flow_valid_count": total_valid,
        "flow_coverage": total_coverage,
        "estimated_net_flow_wan": total_flow,
        "equal_weight_return": equal_return,
        "categories": categories,
    }


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
        "data/daily_table.json",
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
          publish: bool = True, status_override: dict | None = None,
          build_id: str | None = None) -> dict:
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
    market_valid_count = int(
        facts[["close", "shares", "unit_nav"]].notna().all(axis=1).sum()
    )
    valid_count = int(
        (
            facts["flow_status"].eq("VALID")
            & facts["estimated_net_flow"].notna()
        ).sum()
    )
    coverage = valid_count / pool_count if pool_count else 0
    market_coverage = market_valid_count / pool_count if pool_count else 0
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
        "coverage": coverage, "market_valid_count": market_valid_count,
        "market_coverage": market_coverage,
        "classification_version": CLASSIFICATION_VERSION,
        "build_id": build_id,
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
    daily_table = _daily_table(
        trade_date, instruments, facts, status["generated_at"],
    )
    history = (cat_metrics[cat_metrics["window"] == 1]
               .sort_values("trade_date").to_dict("records"))

    _write_json(BUILD_DIR / "data/latest.json", latest)
    _write_json(BUILD_DIR / "data/overview.json", overview)
    _write_json(BUILD_DIR / "data/category_latest.json", rankings)
    _write_json(BUILD_DIR / "data/industry_latest.json", industry)
    _write_json(BUILD_DIR / "data/daily_table.json", daily_table)
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

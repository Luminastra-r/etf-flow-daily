import json

import pandas as pd
import pytest

import build_report
import db
import fetch
import quality


def _daily(day, shares=100.0):
    return pd.DataFrame([{
        "基金代码": "510300", "基金简称": "沪深300ETF", "类型": "指数型-股票",
        "市价": 4.1, f"{day}-单位净值": 4.0, f"{day}-累计净值": 4.0,
    }])


def _spot(day, shares=100.0):
    return pd.DataFrame([{
        "代码": "510300", "名称": "沪深300ETF", "最新价": 4.1,
        "涨跌幅": 1.0, "成交量": 10.0, "成交额": 100.0,
        "最新份额": shares, "数据日期": day,
        "更新时间": f"{day} 16:00:00+08:00",
    }])


def _sse(day, shares=100.0):
    return pd.DataFrame([{
        "基金代码": "510300", "基金简称": "沪深300ETF",
        "基金份额": shares, "统计日期": day,
    }])


def _patch_sources(monkeypatch, day, shares):
    monkeypatch.setattr(fetch.ak, "fund_etf_fund_daily_em", lambda: _daily(day, shares))
    monkeypatch.setattr(fetch.ak, "fund_etf_spot_em", lambda: _spot(day, shares))
    monkeypatch.setattr(fetch.ak, "fund_etf_scale_sse", lambda date: _sse(day, shares),
                        raising=False)
    monkeypatch.setattr(fetch.ak, "fund_etf_scale_szse", lambda: pd.DataFrame(),
                        raising=False)
    monkeypatch.setattr(fetch.calendar_service, "latest_completed_trade_date", lambda: day)


def test_first_reliable_snapshot_is_baseline_then_next_day_is_valid(tmp_path, monkeypatch):
    path = tmp_path / "flow.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    _patch_sources(monkeypatch, "2026-07-28", 100.0)
    instruments, first, _, _ = fetch.fetch_staging("2026-07-28")
    assert first.iloc[0]["flow_status"] == "BASELINE"
    assert pd.isna(first.iloc[0]["estimated_net_flow"])
    db.upsert_snapshot(instruments, first, pd.DataFrame(), path)

    _patch_sources(monkeypatch, "2026-07-29", 110.0)
    _, second, _, _ = fetch.fetch_staging("2026-07-29")
    row = second.iloc[0]
    assert row["shares_date"] == "2026-07-29"
    assert row["shares_source"] == "AKShare:SSE"
    assert row["flow_status"] == "VALID"
    assert row["estimated_net_flow"] == pytest.approx((110 - 100) * 4 / 10000)


def test_mismatched_spot_date_does_not_override_target_price(tmp_path, monkeypatch):
    path = tmp_path / "price-date.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(fetch.ak, "fund_etf_fund_daily_em",
                        lambda: _daily("2026-07-29"))
    monkeypatch.setattr(fetch.ak, "fund_etf_spot_em",
                        lambda: _spot("2026-07-30", 999))
    monkeypatch.setattr(fetch.ak, "fund_etf_scale_sse",
                        lambda date: _sse("2026-07-29", 100), raising=False)
    monkeypatch.setattr(fetch.ak, "fund_etf_scale_szse",
                        lambda: pd.DataFrame(), raising=False)
    monkeypatch.setattr(fetch.calendar_service, "latest_completed_trade_date",
                        lambda: "2026-07-29")
    _, facts, _, _ = fetch.fetch_staging("2026-07-29")
    row = facts.iloc[0]
    assert row["close"] == pytest.approx(4.1)
    assert pd.isna(row["pct_change"])
    assert row["shares"] == pytest.approx(100)


def test_all_market_zero_flow_is_blocked(tmp_path):
    path = tmp_path / "quality.sqlite"
    db.migrate(path, create_backup=False)
    ids = [f"SH.{510000+i:06d}" for i in range(20)]
    instruments = pd.DataFrame({
        "instrument_id": ids, "primary_category": "宽基",
    })
    facts = pd.DataFrame({
        "trade_date": "2026-07-29", "instrument_id": ids, "close": 4.0,
        "pct_change": 1.0, "volume": 1.0, "amount": 1.0, "unit_nav": 4.0,
        "valuation_date": "2026-07-29", "shares": 100.0,
        "shares_unit_factor": 1.0, "shares_date": "2026-07-29",
        "estimated_net_flow": 0.0, "flow_status": "VALID",
    })
    with pytest.raises(quality.QualityGateError, match="全市场份额静止异常"):
        quality.validate_snapshot(instruments, facts, "2026-07-29", path)


def test_source_health_counts_once_per_day_retires_and_recovers(tmp_path):
    path = tmp_path / "health.sqlite"
    db.migrate(path, create_backup=False)
    db.record_source_health("dxy", "Yahoo", True, "2026-07-29", path=path)
    assert db.record_source_health("dxy", "Yahoo", False, path=path)["active"]
    same_day = db.record_source_health("dxy", "Yahoo", False, path=path)
    assert same_day["active"] and same_day["consecutive_failures"] == 1
    with db.connect(path) as conn:
        conn.execute(
            "UPDATE source_field_health SET updated_at=? WHERE field_key=?",
            ("2000-01-01T00:00:00+08:00", "dxy"),
        )
    assert db.record_source_health("dxy", "Yahoo", False, path=path)["active"]
    with db.connect(path) as conn:
        conn.execute(
            "UPDATE source_field_health SET updated_at=? WHERE field_key=?",
            ("2000-01-02T00:00:00+08:00", "dxy"),
        )
    assert not db.record_source_health("dxy", "Yahoo", False, path=path)["active"]
    recovered = db.record_source_health("dxy", "Yahoo", True, "2026-07-30", path=path)
    assert recovered["active"] and recovered["consecutive_failures"] == 0


def test_daily_table_excludes_unclassified_and_uses_decimal_returns():
    instruments = pd.DataFrame([
        {"instrument_id": "SH.510300", "primary_category": "宽基",
         "secondary_category": "沪深300"},
        {"instrument_id": "SH.510500", "primary_category": "宽基",
         "secondary_category": "中证500"},
        {"instrument_id": "SH.510999", "primary_category": "未分类",
         "secondary_category": "未分类"},
    ])
    facts = pd.DataFrame([
        {"instrument_id": "SH.510300", "estimated_net_flow": 100.0,
         "pct_change": 1.0, "flow_status": "VALID"},
        {"instrument_id": "SH.510500", "estimated_net_flow": -20.0,
         "pct_change": -1.0, "flow_status": "VALID"},
        {"instrument_id": "SH.510999", "estimated_net_flow": 999.0,
         "pct_change": 9.0, "flow_status": "VALID"},
    ])
    payload = build_report._daily_table(
        "2026-07-29", instruments, facts, "2026-07-30T07:46:00+08:00",
    )
    assert payload["classified_count"] == 2
    assert payload["unclassified_count"] == 1
    assert payload["estimated_net_flow_wan"] == 80.0
    assert payload["equal_weight_return"] == pytest.approx(0.0)


def test_market_payload_has_no_usdcny_definition():
    assert "usdcny" not in build_report.MARKET_DEFINITIONS

from datetime import date

import pandas as pd

import calendar_service
import db
import fetch
import quality
import pytest


def _instrument(iid, day="2026-08-06"):
    return {
        "instrument_id": iid, "code": iid[-6:], "exchange": "SH", "name": iid,
        "fund_type": "股票", "list_date": None, "delist_date": None,
        "primary_category": "宽基", "secondary_category": "沪深300",
        "tags_json": "[]", "track_index_code": None, "track_index_name": None,
        "source": "AKShare", "first_seen_date": day, "last_seen_date": day,
        "active": 1, "classification_version": "test", "updated_at": "now",
    }


def _fact(iid, valid, day="2026-08-06"):
    return {
        "trade_date": day, "instrument_id": iid,
        "close": 4.0 if valid else None, "pct_change": 1.0 if valid else None,
        "volume": 1 if valid else None, "amount": 1 if valid else None,
        "unit_nav": 4.0 if valid else None, "valuation_date": day if valid else None,
        "shares": 100 if valid else None, "shares_raw": 100 if valid else None,
        "shares_unit": "份" if valid else None,
        "shares_unit_factor": 1.0 if valid else None,
        "shares_date": day if valid else None, "shares_source": "AKShare:SSE",
        "shares_updated_at": "now", "previous_aum": 400 if valid else None,
        "estimated_net_flow": 1.0 if valid else None,
        "flow_rate": 0.01 if valid else None,
        "flow_status": "VALID" if valid else "SOURCE_MISSING",
        "source": "AKShare", "data_status": "VALID" if valid else "PARTIAL",
        "collected_at": "now",
    }


def test_low_coverage_is_warning_not_failure(tmp_path):
    path = tmp_path / "quality.sqlite"
    db.migrate(path, create_backup=False)
    instruments = pd.DataFrame([_instrument("SH.510300"), _instrument("SH.510301")])
    facts = pd.DataFrame([_fact("SH.510300", True), _fact("SH.510301", False)])

    issues, stats = quality.validate_snapshot(instruments, facts, "2026-08-06", path=path)

    assert stats["coverage"] == 0.5
    assert all(issue["severity"] == "WARNING" for issue in issues)
    assert {issue["check_name"] for issue in issues} >= {
        "行情有效覆盖率", "份额有效覆盖率", "资金流有效覆盖率",
    }


def test_truthfulness_errors_still_fail(tmp_path):
    path = tmp_path / "quality.sqlite"
    db.migrate(path, create_backup=False)
    instruments = pd.DataFrame([_instrument("SH.510300")])
    facts = pd.DataFrame([_fact("SH.510300", True)])
    facts.loc[0, "trade_date"] = "2026-08-05"
    facts.loc[0, "shares"] = -1

    with pytest.raises(quality.QualityGateError) as exc:
        quality.validate_snapshot(instruments, facts, "2026-08-06", path=path)

    assert "交易日期一致性" in str(exc.value)
    assert "非负字段合法性" in str(exc.value)


def test_worse_retry_is_retained_without_upsert(monkeypatch):
    instruments = pd.DataFrame([_instrument("SH.510300"), _instrument("SH.510301")])
    facts = pd.DataFrame([_fact("SH.510300", True), _fact("SH.510301", False)])
    monkeypatch.setattr(
        fetch, "fetch_staging", lambda expected: (instruments, facts, expected, [])
    )
    monkeypatch.setattr(
        fetch.quality, "validate_snapshot",
        lambda *args, **kwargs: ([], {
            "pool_count": 2, "valid_count": 1, "coverage": 0.5,
            "market_valid_count": 1, "market_coverage": 0.5,
            "share_valid_count": 1, "share_coverage": 0.5,
            "baseline": False, "preserve_universe": False,
        }),
    )
    monkeypatch.setattr(fetch.metrics, "compute_metrics", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(fetch.quality, "validate_metrics", lambda *args, **kwargs: None)
    monkeypatch.setattr(fetch.db, "record_quality_issues", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        fetch.db, "upsert_snapshot",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应覆盖较好快照")),
    )

    result = fetch.compute_and_store(
        "2026-08-06", "run", benchmark=pd.DataFrame(), minimum_coverage=0.8,
    )

    assert result["retained"] is True
    assert result["retained_coverage"] == 0.8


def test_force_refresh_can_replace_lower_coverage(monkeypatch):
    instruments = pd.DataFrame([_instrument("SH.510300"), _instrument("SH.510301")])
    facts = pd.DataFrame([_fact("SH.510300", True), _fact("SH.510301", False)])
    written = []
    monkeypatch.setattr(
        fetch, "fetch_staging", lambda expected: (instruments, facts, expected, [])
    )
    monkeypatch.setattr(
        fetch.quality, "validate_snapshot",
        lambda *args, **kwargs: ([], {
            "pool_count": 2, "valid_count": 1, "coverage": 0.5,
            "market_valid_count": 1, "market_coverage": 0.5,
            "share_valid_count": 1, "share_coverage": 0.5,
            "baseline": False, "preserve_universe": False,
        }),
    )
    monkeypatch.setattr(fetch.metrics, "compute_metrics", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(fetch.quality, "validate_metrics", lambda *args, **kwargs: None)
    monkeypatch.setattr(fetch.db, "record_quality_issues", lambda *args, **kwargs: None)
    monkeypatch.setattr(fetch.db, "upsert_snapshot", lambda *args, **kwargs: written.append(kwargs))

    result = fetch.compute_and_store(
        "2026-08-06", "run", benchmark=pd.DataFrame(), force_refresh=True,
        minimum_coverage=0.8,
    )

    assert result["retained"] is False
    assert written == [{"deactivate_missing": True}]


def test_partial_universe_does_not_deactivate_existing_instruments(tmp_path):
    path = tmp_path / "pool.sqlite"
    db.migrate(path, create_backup=False)
    initial = pd.DataFrame([_instrument("SH.510300"), _instrument("SH.510301")])
    initial_facts = pd.DataFrame([_fact("SH.510300", True), _fact("SH.510301", True)])
    db.upsert_snapshot(initial, initial_facts, pd.DataFrame(), path=path)

    db.upsert_snapshot(
        initial.iloc[[0]], initial_facts.iloc[[0]], pd.DataFrame(),
        path=path, deactivate_missing=False,
    )

    active = db.query("SELECT COUNT(*) n FROM etf_instrument WHERE active=1", path=path)
    assert int(active.iloc[0]["n"]) == 2


def test_calendar_retries_primary_then_falls_back(monkeypatch):
    calls = {"primary": 0, "fallback": 0}

    def primary(start, end):
        calls["primary"] += 1
        raise RuntimeError("primary unavailable")

    def fallback(start, end):
        calls["fallback"] += 1
        return ["2026-08-06"]

    monkeypatch.setitem(calendar_service.SETTINGS, "retry_count", 2)
    monkeypatch.setitem(calendar_service.SETTINGS, "retry_base_seconds", 0)
    monkeypatch.setattr(calendar_service, "_baostock_trading_dates", primary)
    monkeypatch.setattr(calendar_service, "_ak_trading_dates", fallback)

    result = calendar_service.trading_dates(date(2026, 8, 6), date(2026, 8, 6))

    assert result == ["2026-08-06"]
    assert calls == {"primary": 2, "fallback": 1}


def test_benchmark_retries_primary_then_falls_back(monkeypatch):
    calls = {"primary": 0, "fallback": 0}

    def primary(start, end):
        calls["primary"] += 1
        raise RuntimeError("primary unavailable")

    def fallback(start, end):
        calls["fallback"] += 1
        return pd.DataFrame([{"date": "2026-08-06", "close": 4000}])

    monkeypatch.setitem(calendar_service.SETTINGS, "retry_count", 2)
    monkeypatch.setitem(calendar_service.SETTINGS, "retry_base_seconds", 0)
    monkeypatch.setattr(calendar_service, "_baostock_benchmark_closes", primary)
    monkeypatch.setattr(calendar_service, "_ak_benchmark_closes", fallback)

    frame, warnings = calendar_service.benchmark_closes("2026-08-06", "2026-08-06")

    assert frame.to_dict("records") == [{"date": "2026-08-06", "close": 4000}]
    assert calls == {"primary": 2, "fallback": 1}
    assert any("Baostock 不可用" in warning for warning in warnings)

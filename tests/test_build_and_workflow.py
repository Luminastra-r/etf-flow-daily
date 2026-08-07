from argparse import Namespace
from datetime import date
from pathlib import Path

import pandas as pd

import build_report
import db
import run_daily


def _seed(path):
    db.migrate(path, create_backup=False)
    stamp = db.now_cn()
    instrument = pd.DataFrame([{
        "instrument_id": "SH.510300", "code": "510300", "exchange": "SH",
        "name": "沪深300ETF", "fund_type": "股票", "list_date": None,
        "delist_date": None, "primary_category": "宽基",
        "secondary_category": "沪深300", "tags_json": "[]",
        "track_index_code": None, "track_index_name": None, "source": "AKShare",
        "first_seen_date": "2026-07-28", "last_seen_date": "2026-07-28",
        "active": 1, "classification_version": "test", "updated_at": stamp,
    }])
    fact = pd.DataFrame([{
        "trade_date": "2026-07-28", "instrument_id": "SH.510300", "close": 4.0,
        "pct_change": 1.0, "volume": 1, "amount": 1, "unit_nav": 4.0,
        "valuation_date": "2026-07-28", "shares": 100, "previous_aum": None,
        "estimated_net_flow": None, "flow_rate": None, "source": "AKShare",
        "shares_raw": 100, "shares_unit": "份", "shares_unit_factor": 1.0,
        "shares_date": "2026-07-28", "shares_source": "AKShare:SSE",
        "shares_updated_at": stamp, "flow_status": "BASELINE",
        "data_status": "PARTIAL", "collected_at": stamp,
    }])
    db.upsert_snapshot(instrument, fact, pd.DataFrame(), path)


def test_static_build_has_external_json_and_valid_paths(tmp_path, monkeypatch):
    path = tmp_path / "build.sqlite"
    _seed(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(build_report, "BUILD_DIR", tmp_path / "build")
    monkeypatch.setattr(build_report, "OUTPUT_DIR", tmp_path / "output")
    result = build_report.build("2026-07-28", load_market=False, publish=False)
    root = tmp_path / "build"
    assert result["total_bytes"] < 1_000_000
    assert (root / "data/overview.json").is_file()
    assert (root / "data/daily_table.json").is_file()
    html = (root / "index.html").read_text(encoding="utf-8")
    assert "Plotly.newPlot" not in html
    assert "viewport" in html


def test_non_trading_day_is_skipped(tmp_path, monkeypatch):
    path = tmp_path / "run.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(run_daily.calendar_service, "is_trading_day", lambda _: False)
    args = Namespace(trade_date="2026-07-26", force_refresh=False, rebuild_page=False,
                     scheduled_at=None, no_market=True)
    result = run_daily.execute(args)
    assert result["status"] == "SKIPPED"
    assert db.query("SELECT COUNT(*) n FROM etf_daily_fact", path=path).iloc[0]["n"] == 0


def test_workflow_has_manual_inputs_and_timeout():
    text = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")
    assert text.count("cron:") == 3
    for token in ["trade_date:", "force_refresh:", "rebuild_page:", "timeout-minutes: 20",
                  'cron: "47 16 * * *"', 'cron: "17 19 * * *"',
                  'cron: "47 21 * * *"', "cache: \"pip\"", "--build-id",
                  "--scheduled-run",
                  "etf-flow-daily.pages.dev/data/latest.json"]:
        assert token in text


def test_existing_complete_snapshot_is_up_to_date_without_fetch(tmp_path, monkeypatch):
    path = tmp_path / "run.sqlite"
    _seed(path)
    with db.connect(path) as conn:
        conn.execute(
            """UPDATE etf_daily_fact
               SET estimated_net_flow=1,flow_rate=0.01,flow_status='VALID',data_status='VALID'"""
        )
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(run_daily.calendar_service, "is_trading_day", lambda _: True)
    monkeypatch.setattr(
        run_daily.fetch, "compute_and_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应重新抓数")),
    )
    args = Namespace(
        trade_date="2026-07-28", force_refresh=False, rebuild_page=False,
        scheduled_at="2026-07-29T00:47:00+08:00", scheduled_run=True,
        no_market=True, build_id="test",
    )
    result = run_daily.execute(args)
    assert result["status"] == "UP_TO_DATE"


def test_old_partial_snapshot_does_not_retry_outside_next_morning(tmp_path, monkeypatch):
    path = tmp_path / "run.sqlite"
    _seed(path)
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(run_daily.calendar_service, "is_trading_day", lambda _: True)
    monkeypatch.setattr(run_daily.calendar_service, "today_cn", lambda: date(2026, 7, 30))
    monkeypatch.setattr(
        run_daily.fetch, "compute_and_store",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("补跑窗口已结束")),
    )
    args = Namespace(
        trade_date="2026-07-28", force_refresh=False, rebuild_page=False,
        scheduled_at="2026-07-30T00:47:00+08:00", scheduled_run=True,
        no_market=True, build_id="test",
    )
    result = run_daily.execute(args)
    assert result["status"] == "RETAINED"


def test_instrument_status_derivation():
    instruments = pd.DataFrame({"instrument_id": ["A", "B", "C", "D"]})
    facts = pd.DataFrame([
        {"trade_date": "2026-07-28", "instrument_id": "A", "data_status": "VALID"},
        {"trade_date": "2026-07-28", "instrument_id": "B", "data_status": "PARTIAL"},
        {"trade_date": "2026-07-27", "instrument_id": "C", "data_status": "VALID"},
    ])
    assert build_report.instrument_status_counts(instruments, facts, "2026-07-28") == {
        "VALID": 1, "PARTIAL": 1, "STALE": 1, "MISSING": 1,
    }


def test_page_headers_are_concise_and_quality_notice_is_available():
    index = Path("web/index.html").read_text(encoding="utf-8")
    market = Path("web/market.html").read_text(encoding="utf-8")
    methodology = Path("web/methodology.html").read_text(encoding="utf-8")
    app = Path("web/app.js").read_text(encoding="utf-8")

    assert "PRE-MARKET CAPITAL TAPE" not in index
    assert "缺失就是缺失" not in index
    assert "把资金流，放回市场环境里" not in market
    assert "每一个数字，都应当知道自己从哪里来" not in methodology
    assert "<h1>市场环境</h1>" in market
    assert "<h1>方法与数据质量</h1>" in methodology
    assert "source-note" in market
    assert "quality-alert" in app
    assert "未分类 ETF 占比" in app

import sqlite3

import pandas as pd

import db


def _legacy(path):
    conn = sqlite3.connect(path)
    conn.executescript(db.DDL_V2.split("CREATE TABLE IF NOT EXISTS schema_migrations")[0] or "")
    conn.execute("""CREATE TABLE etf_daily(
      date TEXT NOT NULL,code TEXT NOT NULL,name TEXT,category TEXT,sub_industry TEXT,
      shares REAL,nav REAL,close REAL,change_pct REAL,net_subscribe REAL,
      PRIMARY KEY(date,code))""")
    conn.execute("""CREATE TABLE category_daily(
      date TEXT NOT NULL,category TEXT NOT NULL,count INTEGER,total_net_subscribe REAL,
      avg_change_pct REAL,PRIMARY KEY(date,category))""")
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
    conn.execute("""INSERT INTO etf_daily VALUES(
      '2026-07-28','510300','沪深300ETF','宽基','沪深300',100,4,4,1,NULL)""")
    conn.commit()
    conn.close()


def test_migration_is_idempotent_and_preserves_null(tmp_path):
    path = tmp_path / "legacy.sqlite"
    _legacy(path)
    first = db.migrate(path, create_backup=False)
    second = db.migrate(path, create_backup=False)
    assert first["migrated_facts"] == 1
    assert second["migrated_facts"] == 0
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM etf_daily").fetchone()[0] == 1
    row = conn.execute("SELECT instrument_id,estimated_net_flow FROM etf_daily_fact").fetchone()
    assert row == ("SH.510300", None)
    conn.close()


def test_upsert_does_not_replace_valid_value_with_null(tmp_path):
    path = tmp_path / "new.sqlite"
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
    base = {
        "trade_date": "2026-07-28", "instrument_id": "SH.510300", "close": 4.0,
        "pct_change": 1.0, "volume": 1.0, "amount": 1.0, "unit_nav": 4.0,
        "valuation_date": "2026-07-28", "shares": 110.0, "previous_aum": 400.0,
        "estimated_net_flow": 0.004, "flow_rate": 0.1, "source": "AKShare",
        "data_status": "VALID", "collected_at": stamp,
    }
    db.upsert_snapshot(instrument, pd.DataFrame([base]), pd.DataFrame(), path)
    partial = dict(base, estimated_net_flow=None, flow_rate=None, data_status="PARTIAL")
    db.upsert_snapshot(instrument, pd.DataFrame([partial]), pd.DataFrame(), path)
    row = db.query("SELECT estimated_net_flow,data_status FROM etf_daily_fact", path=path).iloc[0]
    assert row["estimated_net_flow"] == 0.004
    assert row["data_status"] == "VALID"


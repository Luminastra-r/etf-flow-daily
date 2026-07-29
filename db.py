"""SQLite v2：版本化迁移、事务 UPSERT 与查询辅助。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from config import BACKUP_DIR, CLASSIFICATION_VERSION, DB_PATH
from instrument import infer_exchange, instrument_id, normalize_code

SCHEMA_VERSION = "2"
SCHEMA_CHECKSUM = "etf-flow-v2-20260729"

DDL_V2 = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS etf_instrument (
    instrument_id TEXT PRIMARY KEY,
    code TEXT NOT NULL,
    exchange TEXT NOT NULL CHECK(exchange IN ('SH','SZ')),
    name TEXT,
    fund_type TEXT,
    list_date TEXT,
    delist_date TEXT,
    primary_category TEXT NOT NULL DEFAULT '未分类',
    secondary_category TEXT NOT NULL DEFAULT '未分类',
    tags_json TEXT NOT NULL DEFAULT '[]',
    track_index_code TEXT,
    track_index_name TEXT,
    source TEXT NOT NULL,
    first_seen_date TEXT NOT NULL,
    last_seen_date TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    classification_version TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(exchange, code)
);
CREATE TABLE IF NOT EXISTS etf_daily_fact (
    trade_date TEXT NOT NULL,
    instrument_id TEXT NOT NULL REFERENCES etf_instrument(instrument_id),
    close REAL,
    pct_change REAL,
    volume REAL,
    amount REAL,
    unit_nav REAL,
    valuation_date TEXT,
    shares REAL,
    previous_aum REAL,
    estimated_net_flow REAL,
    flow_rate REAL,
    source TEXT NOT NULL,
    data_status TEXT NOT NULL CHECK(data_status IN ('VALID','PARTIAL')),
    collected_at TEXT NOT NULL,
    PRIMARY KEY(trade_date, instrument_id)
);
CREATE TABLE IF NOT EXISTS category_daily_metric (
    trade_date TEXT NOT NULL,
    category TEXT NOT NULL,
    window INTEGER NOT NULL,
    available_days INTEGER NOT NULL,
    estimated_net_flow REAL,
    flow_rate REAL,
    price_return REAL,
    benchmark_return REAL,
    relative_return REAL,
    breadth REAL,
    inflow_count INTEGER,
    outflow_count INTEGER,
    valid_count INTEGER,
    missing_count INTEGER,
    top1_concentration REAL,
    top3_concentration REAL,
    inflow_streak INTEGER,
    flow_zscore REAL,
    observation_status TEXT,
    PRIMARY KEY(trade_date, category, window)
);
CREATE TABLE IF NOT EXISTS pipeline_run (
    run_id TEXT PRIMARY KEY,
    requested_date TEXT,
    trade_date TEXT,
    scheduled_at TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('RUNNING','VALID','PARTIAL','FAILED','SKIPPED','REBUILT')),
    pool_count INTEGER,
    valid_count INTEGER,
    coverage REAL,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS quality_issue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES pipeline_run(run_id),
    check_name TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('WARNING','ERROR')),
    affected_count INTEGER NOT NULL DEFAULT 0,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_fact_instrument_date
    ON etf_daily_fact(instrument_id, trade_date);
CREATE INDEX IF NOT EXISTS idx_fact_date_status
    ON etf_daily_fact(trade_date, data_status);
CREATE INDEX IF NOT EXISTS idx_instrument_active_category
    ON etf_instrument(active, primary_category);
CREATE INDEX IF NOT EXISTS idx_metric_date_window
    ON category_daily_metric(trade_date, window);
"""


def now_cn() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


@contextmanager
def connect(path: str | Path | None = None):
    conn = sqlite3.connect(str(path or DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _backup_once(path: Path) -> Path | None:
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"{path.stem}.pre_v2.sqlite"
    if target.exists():
        return target
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return target


def migrate(path: str | Path | None = None, create_backup: bool = True) -> dict:
    """幂等迁移旧库；旧表保留不再写入。"""
    db_path = Path(path or DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as probe:
        already = _has_table(probe, "schema_migrations") and probe.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,)
        ).fetchone()
        has_legacy = _has_table(probe, "etf_daily")
    backup = _backup_once(db_path) if create_backup and has_legacy and not already else None

    migrated_facts = 0
    with connect(db_path) as conn:
        conn.executescript(DDL_V2)
        if conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,)
        ).fetchone():
            _backfill_legacy_metrics(conn)
            return {"version": SCHEMA_VERSION, "migrated_facts": 0, "backup": str(backup) if backup else None}

        if _has_table(conn, "etf_daily"):
            legacy = conn.execute(
                """SELECT date,code,name,category,sub_industry,shares,nav,close,
                          change_pct,net_subscribe FROM etf_daily ORDER BY date,code"""
            ).fetchall()
            for row in legacy:
                code = normalize_code(row["code"])
                exchange = infer_exchange(code)
                iid = instrument_id(code, exchange)
                stamp = now_cn()
                category = row["category"] or "未分类"
                secondary = row["sub_industry"]
                if not secondary or secondary == "其他":
                    secondary = "未分类"
                conn.execute(
                    """INSERT INTO etf_instrument(
                        instrument_id,code,exchange,name,primary_category,secondary_category,
                        source,first_seen_date,last_seen_date,classification_version,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(instrument_id) DO UPDATE SET
                      name=COALESCE(excluded.name,etf_instrument.name),
                      last_seen_date=MAX(etf_instrument.last_seen_date,excluded.last_seen_date),
                      updated_at=excluded.updated_at""",
                    (iid, code, exchange, row["name"], category, secondary, "AKShare",
                     row["date"], row["date"], CLASSIFICATION_VERSION, stamp),
                )
                status = "VALID" if row["net_subscribe"] is not None else "PARTIAL"
                previous_aum = None
                flow_rate = None
                conn.execute(
                    """INSERT OR IGNORE INTO etf_daily_fact(
                        trade_date,instrument_id,close,pct_change,unit_nav,valuation_date,
                        shares,previous_aum,estimated_net_flow,flow_rate,source,data_status,collected_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row["date"], iid, row["close"], row["change_pct"], row["nav"],
                     row["date"], row["shares"], previous_aum, row["net_subscribe"],
                     flow_rate, "AKShare", status, stamp),
                )
                migrated_facts += 1

        conn.execute(
            "INSERT INTO schema_migrations(version,applied_at,checksum) VALUES(?,?,?)",
            (SCHEMA_VERSION, now_cn(), SCHEMA_CHECKSUM),
        )
        _backfill_legacy_metrics(conn)
    return {"version": SCHEMA_VERSION, "migrated_facts": migrated_facts, "backup": str(backup) if backup else None}


def _backfill_legacy_metrics(conn: sqlite3.Connection):
    """只迁移可证明的旧指标；旧空资金被聚合成的 0 不予沿用。"""
    if not _has_table(conn, "category_daily") or not _has_table(conn, "etf_daily"):
        return
    rows = conn.execute(
        "SELECT date,category,count,avg_change_pct FROM category_daily"
    ).fetchall()
    for row in rows:
        flow_rows = conn.execute(
            """SELECT net_subscribe FROM etf_daily
               WHERE date=? AND category=? AND net_subscribe IS NOT NULL""",
            (row["date"], row["category"]),
        ).fetchall()
        flow_total = sum(x["net_subscribe"] for x in flow_rows) if flow_rows else None
        valid_count = len(flow_rows)
        count = int(row["count"] or 0)
        conn.execute(
            """INSERT OR IGNORE INTO category_daily_metric(
                trade_date,category,window,available_days,estimated_net_flow,
                flow_rate,price_return,benchmark_return,relative_return,breadth,
                inflow_count,outflow_count,valid_count,missing_count,
                top1_concentration,top3_concentration,inflow_streak,flow_zscore,
                observation_status
            ) VALUES(?,?,1,1,?,NULL,?,NULL,NULL,NULL,?,?,?,?,NULL,NULL,NULL,NULL,'历史不足')""",
            (row["date"], row["category"], flow_total,
             row["avg_change_pct"] / 100 if row["avg_change_pct"] is not None else None,
             sum(x["net_subscribe"] > 0 for x in flow_rows) if flow_rows else None,
             sum(x["net_subscribe"] < 0 for x in flow_rows) if flow_rows else None,
             valid_count, max(count - valid_count, 0)),
        )


def init_db(path: str | Path | None = None):
    return migrate(path)


def query(sql: str, params=None, path: str | Path | None = None) -> pd.DataFrame:
    with connect(path) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def get_latest_date(path: str | Path | None = None):
    migrate(path, create_backup=False)
    df = query("SELECT MAX(trade_date) AS d FROM etf_daily_fact", path=path)
    return None if df.empty else df.iloc[0]["d"]


def start_run(run_id: str, requested_date: str | None, scheduled_at: str | None = None,
              path: str | Path | None = None):
    migrate(path)
    with connect(path) as conn:
        conn.execute(
            """INSERT INTO pipeline_run(run_id,requested_date,scheduled_at,started_at,status)
               VALUES(?,?,?,?, 'RUNNING')""",
            (run_id, requested_date, scheduled_at, now_cn()),
        )


def finish_run(run_id: str, status: str, trade_date: str | None = None,
               pool_count: int | None = None, valid_count: int | None = None,
               coverage: float | None = None, warnings=None, error: str | None = None,
               path: str | Path | None = None):
    with connect(path) as conn:
        conn.execute(
            """UPDATE pipeline_run SET trade_date=?,finished_at=?,status=?,pool_count=?,
               valid_count=?,coverage=?,warnings_json=?,error_message=? WHERE run_id=?""",
            (trade_date, now_cn(), status, pool_count, valid_count, coverage,
             json.dumps(warnings or [], ensure_ascii=False), error, run_id),
        )


def record_quality_issues(run_id: str, issues: list[dict], path=None):
    if not issues:
        return
    with connect(path) as conn:
        conn.executemany(
            """INSERT INTO quality_issue(run_id,check_name,severity,affected_count,details_json)
               VALUES(?,?,?,?,?)""",
            [(run_id, x["check_name"], x["severity"], int(x.get("affected_count", 0)),
              json.dumps(x.get("details", {}), ensure_ascii=False)) for x in issues],
        )


def upsert_snapshot(instruments: pd.DataFrame, facts: pd.DataFrame, metrics: pd.DataFrame,
                    path: str | Path | None = None):
    """一个事务写入全部正式表；新空值不会覆盖已有有效字段。"""
    migrate(path)
    with connect(path) as conn:
        for row in instruments.to_dict("records"):
            conn.execute(
                """INSERT INTO etf_instrument(
                    instrument_id,code,exchange,name,fund_type,list_date,delist_date,
                    primary_category,secondary_category,tags_json,track_index_code,
                    track_index_name,source,first_seen_date,last_seen_date,active,
                    classification_version,updated_at
                ) VALUES(:instrument_id,:code,:exchange,:name,:fund_type,:list_date,:delist_date,
                    :primary_category,:secondary_category,:tags_json,:track_index_code,
                    :track_index_name,:source,:first_seen_date,:last_seen_date,:active,
                    :classification_version,:updated_at)
                ON CONFLICT(instrument_id) DO UPDATE SET
                    name=COALESCE(excluded.name,etf_instrument.name),
                    fund_type=COALESCE(excluded.fund_type,etf_instrument.fund_type),
                    list_date=COALESCE(excluded.list_date,etf_instrument.list_date),
                    delist_date=COALESCE(excluded.delist_date,etf_instrument.delist_date),
                    primary_category=excluded.primary_category,
                    secondary_category=excluded.secondary_category,
                    tags_json=excluded.tags_json,
                    track_index_code=COALESCE(excluded.track_index_code,etf_instrument.track_index_code),
                    track_index_name=COALESCE(excluded.track_index_name,etf_instrument.track_index_name),
                    last_seen_date=excluded.last_seen_date,active=excluded.active,
                    classification_version=excluded.classification_version,updated_at=excluded.updated_at""",
                row,
            )
        seen = instruments["instrument_id"].tolist()
        if seen:
            marks = ",".join("?" for _ in seen)
            conn.execute(f"UPDATE etf_instrument SET active=0 WHERE instrument_id NOT IN ({marks})", seen)

        for row in facts.to_dict("records"):
            clean = {k: (None if pd.isna(v) else v) for k, v in row.items()}
            conn.execute(
                """INSERT INTO etf_daily_fact(
                    trade_date,instrument_id,close,pct_change,volume,amount,unit_nav,
                    valuation_date,shares,previous_aum,estimated_net_flow,flow_rate,
                    source,data_status,collected_at
                ) VALUES(:trade_date,:instrument_id,:close,:pct_change,:volume,:amount,:unit_nav,
                    :valuation_date,:shares,:previous_aum,:estimated_net_flow,:flow_rate,
                    :source,:data_status,:collected_at)
                ON CONFLICT(trade_date,instrument_id) DO UPDATE SET
                    close=COALESCE(excluded.close,etf_daily_fact.close),
                    pct_change=COALESCE(excluded.pct_change,etf_daily_fact.pct_change),
                    volume=COALESCE(excluded.volume,etf_daily_fact.volume),
                    amount=COALESCE(excluded.amount,etf_daily_fact.amount),
                    unit_nav=COALESCE(excluded.unit_nav,etf_daily_fact.unit_nav),
                    valuation_date=COALESCE(excluded.valuation_date,etf_daily_fact.valuation_date),
                    shares=COALESCE(excluded.shares,etf_daily_fact.shares),
                    previous_aum=COALESCE(excluded.previous_aum,etf_daily_fact.previous_aum),
                    estimated_net_flow=COALESCE(excluded.estimated_net_flow,etf_daily_fact.estimated_net_flow),
                    flow_rate=COALESCE(excluded.flow_rate,etf_daily_fact.flow_rate),
                    source=excluded.source,
                    data_status=CASE WHEN etf_daily_fact.data_status='VALID'
                                      AND excluded.data_status='PARTIAL'
                                     THEN 'VALID' ELSE excluded.data_status END,
                    collected_at=excluded.collected_at""",
                clean,
            )
        if not metrics.empty:
            cols = list(metrics.columns)
            sql = f"""INSERT INTO category_daily_metric({','.join(cols)})
                      VALUES({','.join(':'+c for c in cols)})
                      ON CONFLICT(trade_date,category,window) DO UPDATE SET
                      {','.join(f'{c}=excluded.{c}' for c in cols if c not in {'trade_date','category','window'})}"""
            for row in metrics.to_dict("records"):
                conn.execute(sql, {k: (None if pd.isna(v) else v) for k, v in row.items()})


def checkpoint(path: str | Path | None = None):
    with connect(path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def database_stats(path: str | Path | None = None) -> dict:
    db_path = Path(path or DB_PATH)
    with connect(path) as conn:
        facts = conn.execute("SELECT COUNT(*) FROM etf_daily_fact").fetchone()[0]
        instruments = conn.execute("SELECT COUNT(*) FROM etf_instrument").fetchone()[0]
    return {"bytes": db_path.stat().st_size if db_path.exists() else 0,
            "facts": facts, "instruments": instruments}


# 旧演示脚本的最小兼容接口。
def get_meta(key: str):
    if key == "last_run":
        return get_latest_date()
    return None

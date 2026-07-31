"""每日入口：交易日判断 → staging/质量门禁 → 事务入库 → 原子静态发布。"""
from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from datetime import date, timedelta

import build_report
import calendar_service
import db
import fetch
from config import OUTPUT_DIR, SETTINGS


def _parser():
    parser = argparse.ArgumentParser(description="大类 ETF 资金流向日报")
    parser.add_argument("--trade-date", help="指定交易日 YYYY-MM-DD")
    parser.add_argument("--force-refresh", action="store_true", help="强制重抓已有交易日")
    parser.add_argument("--rebuild-page", action="store_true", help="仅使用现有 SQLite 重建页面")
    parser.add_argument("--scheduled-at", help="计划运行时间（北京时间 ISO8601）")
    parser.add_argument("--build-id", help="GitHub Actions run_id / 构建标识")
    parser.add_argument("--no-market", action="store_true", help="构建时不抓宏观数据（测试用）")
    return parser


def _static_stats():
    files = [p for p in OUTPUT_DIR.rglob("*") if p.is_file()] if OUTPUT_DIR.exists() else []
    return {
        "bytes": sum(p.stat().st_size for p in files),
        "max": max(((p.stat().st_size, str(p.relative_to(OUTPUT_DIR))) for p in files),
                   default=(0, "")),
    }


def execute(args) -> dict:
    started = time.perf_counter()
    migration = db.migrate()
    run_id = uuid.uuid4().hex
    expected = args.trade_date
    # 容错：用户可能在 workflow_dispatch 输入了 "trade_date: 2026-07-30" 等带前缀格式
    if expected:
        match = re.search(r"\d{4}-\d{2}-\d{2}", expected)
        if match:
            expected = match.group()
        else:
            raise RuntimeError(f"无法从输入中解析交易日: {expected}")

    if args.rebuild_page:
        expected = expected or db.get_latest_date()
        if not expected:
            raise RuntimeError("无历史数据可重建")
        db.start_run(run_id, expected, args.scheduled_at)
        try:
            flow_state = db.query(
                """SELECT
                     SUM(CASE WHEN flow_status='VALID' THEN 1 ELSE 0 END) valid_n,
                     SUM(CASE WHEN flow_status='BASELINE' THEN 1 ELSE 0 END) baseline_n
                   FROM etf_daily_fact WHERE trade_date=?""", (expected,)
            ).iloc[0]
            page_status = (
                "BASELINE"
                if int(flow_state["valid_n"] or 0) == 0
                and int(flow_state["baseline_n"] or 0) > 0
                else "REBUILT"
            )
            build = build_report.build(
                expected, load_market=not args.no_market,
                status_override={"status": page_status, "scheduled_at": args.scheduled_at,
                                 "generated_at": db.now_cn(), "warnings": []},
                build_id=getattr(args, "build_id", None),
            )
            db.finish_run(run_id, "REBUILT", expected, coverage=build["coverage"])
            db.checkpoint()
            return _summary(run_id, "REBUILT", expected, migration, build, started)
        except Exception as exc:
            db.finish_run(run_id, "FAILED", expected, error=str(exc))
            raise

    expected = expected or calendar_service.latest_completed_trade_date()
    # 手工传入非交易日应可审计地跳过，而不是抓取最近数据冒充。
    if not calendar_service.is_trading_day(expected):
        db.start_run(run_id, expected, args.scheduled_at)
        db.finish_run(run_id, "SKIPPED", expected, warnings=["非交易日，未写入市场事实"])
        return _summary(run_id, "SKIPPED", expected, migration, None, started)

    db.start_run(run_id, expected, args.scheduled_at)
    try:
        if not args.force_refresh:
            existing = db.query(
                """SELECT COUNT(*) n FROM etf_daily_fact
                   WHERE trade_date=? AND flow_status='VALID'""", (expected,)
            )
            if int(existing.iloc[0]["n"]):
                build = build_report.build(
                    expected, load_market=not args.no_market,
                    status_override={"status": "REBUILT", "scheduled_at": args.scheduled_at,
                                     "generated_at": db.now_cn(),
                                     "warnings": ["当日有效数据已存在，仅重建页面"]},
                    build_id=getattr(args, "build_id", None),
                )
                db.finish_run(run_id, "REBUILT", expected, warnings=["当日有效数据已存在，仅重建页面"])
                db.checkpoint()
                return _summary(run_id, "REBUILT", expected, migration, build, started)

        start = (date.fromisoformat(expected) - timedelta(days=120)).isoformat()
        benchmark, benchmark_warnings = calendar_service.benchmark_closes(start, expected)
        result = fetch.compute_and_store(
            expected, run_id, benchmark=benchmark, force_refresh=args.force_refresh
        )
        warnings = benchmark_warnings + result.get("source_warnings", []) + [
            f"{x['check_name']}: {x.get('details', {})}"
            for x in result["issues"] if x["severity"] == "WARNING"
        ]
        status = "BASELINE" if result.get("baseline") else (
            "VALID" if result["coverage"] >= float(SETTINGS["coverage_warning"])
            else "PARTIAL"
        )
        build = build_report.build(
            expected, load_market=not args.no_market,
            status_override={"status": status, "scheduled_at": args.scheduled_at,
                             "generated_at": db.now_cn(), "warnings": warnings},
            build_id=getattr(args, "build_id", None),
        )
        database_status = "PARTIAL" if status == "BASELINE" else status
        db.finish_run(
            run_id, database_status, expected, result["pool_count"], result["valid_count"],
            result["coverage"], warnings=warnings,
        )
        db.checkpoint()
        return _summary(run_id, status, expected, migration, build, started)
    except Exception as exc:
        try:
            db.finish_run(run_id, "FAILED", expected, error=str(exc))
        finally:
            raise


def _summary(run_id, status, trade_date, migration, build, started):
    elapsed = time.perf_counter() - started
    database = db.database_stats()
    static = _static_stats()
    warning_seconds = float(SETTINGS["runtime_warning_minutes"]) * 60
    mb = 1024 * 1024
    capacity_warnings = []
    if database["bytes"] > float(SETTINGS["sqlite_migration_mb"]) * mb:
        capacity_warnings.append("SQLite 超过 1GB，建议评估存储迁移")
    elif database["bytes"] > float(SETTINGS["sqlite_warning_mb"]) * mb:
        capacity_warnings.append("SQLite 超过 500MB")
    if static["max"][0] > float(SETTINGS["static_file_risk_mb"]) * mb:
        capacity_warnings.append("最大静态文件超过 20MB")
    elif static["max"][0] > float(SETTINGS["static_file_warning_mb"]) * mb:
        capacity_warnings.append("最大静态文件超过 10MB")
    if elapsed > warning_seconds:
        capacity_warnings.append("单次运行超过 15 分钟")
    summary = {
        "run_id": run_id, "status": status, "trade_date": trade_date,
        "elapsed_seconds": round(elapsed, 2), "runtime_warning": elapsed > warning_seconds,
        "migration": migration, "database": database, "build": build,
        "static": static, "capacity_warnings": capacity_warnings,
    }
    print("[run_daily] " + json.dumps(summary, ensure_ascii=False, default=str))
    return summary


def main(argv=None):
    args = _parser().parse_args(argv)
    return execute(args)


if __name__ == "__main__":
    main()

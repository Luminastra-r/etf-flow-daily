"""数据质量门禁：错误阻止发布，警告进入运行日志。"""
from __future__ import annotations

import pandas as pd

import db
from config import SETTINGS


class QualityGateError(RuntimeError):
    def __init__(self, issues: list[dict]):
        self.issues = issues
        super().__init__("；".join(x["check_name"] for x in issues if x["severity"] == "ERROR"))


def _issue(name, severity, affected=0, **details):
    return {"check_name": name, "severity": severity,
            "affected_count": int(affected), "details": details}


def validate_snapshot(instruments: pd.DataFrame, facts: pd.DataFrame,
                      expected_date: str, path=None) -> tuple[list[dict], dict]:
    issues: list[dict] = []
    if instruments.empty or facts.empty:
        issues.append(_issue("staging 非空", "ERROR", 0))
        raise QualityGateError(issues)
    dup_i = int(instruments["instrument_id"].duplicated().sum())
    dup_f = int(facts.duplicated(["trade_date", "instrument_id"]).sum())
    if dup_i or dup_f:
        issues.append(_issue("ETF代码唯一性", "ERROR", dup_i + dup_f))
    wrong_date = int((facts["trade_date"] != expected_date).sum())
    if wrong_date:
        issues.append(_issue("交易日期一致性", "ERROR", wrong_date, expected=expected_date))

    invalid_negative = pd.Series(False, index=facts.index)
    for col in ["shares", "unit_nav", "close", "volume", "amount"]:
        if col in facts:
            invalid_negative |= facts[col].notna() & (facts[col] < 0)
    if invalid_negative.any():
        issues.append(_issue("非负字段合法性", "ERROR", invalid_negative.sum()))

    pct_bad = facts["pct_change"].abs() > float(SETTINGS["pct_change_limit"])
    if pct_bad.any():
        issues.append(_issue("涨跌幅异常", "WARNING", pct_bad.sum()))

    valid_mask = facts[["close", "shares", "unit_nav"]].notna().all(axis=1)
    pool_count = len(instruments)
    valid_count = int(valid_mask.sum())
    coverage = valid_count / pool_count if pool_count else 0.0
    if coverage < float(SETTINGS["coverage_failure"]):
        issues.append(_issue("有效数据覆盖率", "ERROR", pool_count - valid_count,
                             coverage=coverage))
    elif coverage < float(SETTINGS["coverage_warning"]):
        issues.append(_issue("有效数据覆盖率", "WARNING", pool_count - valid_count,
                             coverage=coverage))

    unclassified = int((instruments["primary_category"] == "未分类").sum())
    if unclassified:
        issues.append(_issue("未分类ETF", "WARNING", unclassified,
                             ratio=unclassified / pool_count))

    previous = db.query("SELECT COUNT(*) n FROM etf_instrument WHERE active=1", path=path)
    previous_count = int(previous.iloc[0]["n"]) if not previous.empty else 0
    if previous_count:
        change = abs(pool_count - previous_count) / previous_count
        if change > float(SETTINGS["pool_change_warning"]):
            issues.append(_issue("ETF池数量突变", "WARNING",
                                 abs(pool_count - previous_count), change=change))

    prior_shares = db.query(
        """SELECT f.instrument_id,f.shares FROM etf_daily_fact f
           JOIN (SELECT instrument_id,MAX(trade_date) d FROM etf_daily_fact
                 WHERE trade_date<? GROUP BY instrument_id) p
             ON p.instrument_id=f.instrument_id AND p.d=f.trade_date
           WHERE f.shares IS NOT NULL""", (expected_date,), path=path,
    )
    if not prior_shares.empty:
        comparison = facts[["instrument_id", "shares"]].merge(
            prior_shares, on="instrument_id", suffixes=("_new", "_old")
        )
        jumps = (
            (comparison["shares_new"] - comparison["shares_old"]).abs()
            / comparison["shares_old"].where(comparison["shares_old"] > 0)
        ) > float(SETTINGS["share_jump_warning"])
        if jumps.any():
            issues.append(_issue("基金份额异常跳变", "WARNING", jumps.sum()))

    errors = [x for x in issues if x["severity"] == "ERROR"]
    stats = {"pool_count": pool_count, "valid_count": valid_count, "coverage": coverage}
    if errors:
        raise QualityGateError(issues)
    return issues, stats

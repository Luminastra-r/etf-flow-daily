"""数据质量门禁：日期与资金可信度错误阻止发布，警告进入运行日志。"""
from __future__ import annotations

import math

import pandas as pd

import db
from config import SETTINGS


class QualityGateError(RuntimeError):
    def __init__(self, issues: list[dict]):
        self.issues = issues
        names = [x["check_name"] for x in issues if x["severity"] == "ERROR"]
        super().__init__("；".join(names))


def _issue(name, severity, affected=0, **details):
    return {
        "check_name": name, "severity": severity,
        "affected_count": int(affected), "details": details,
    }


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
    for col in ["shares", "unit_nav", "close", "volume", "amount", "shares_unit_factor"]:
        if col in facts:
            invalid_negative |= facts[col].notna() & (facts[col] < 0)
    if invalid_negative.any():
        issues.append(_issue("非负字段合法性", "ERROR", invalid_negative.sum()))

    if "shares_date" in facts:
        mismatch = facts["shares"].notna() & facts["shares_date"].notna() & (
            facts["shares_date"] != expected_date
        )
        if mismatch.any():
            issues.append(_issue("份额日期一致性", "ERROR", mismatch.sum(),
                                 expected=expected_date))

    pct_bad = facts["pct_change"].abs() > float(SETTINGS["pct_change_limit"])
    if pct_bad.any():
        issues.append(_issue("涨跌幅异常", "WARNING", pct_bad.sum()))

    market_valid = facts[["close", "shares", "unit_nav"]].notna().all(axis=1)
    if "shares_date" in facts:
        share_valid = facts["shares"].notna() & (facts["shares_date"] == expected_date)
    else:
        share_valid = facts["shares"].notna()
    if "flow_status" in facts:
        flow_valid = facts["flow_status"].eq("VALID") & facts["estimated_net_flow"].notna()
        baseline = facts["flow_status"].eq("BASELINE")
    else:
        flow_valid = facts["estimated_net_flow"].notna()
        baseline = pd.Series(False, index=facts.index)

    pool_count = len(instruments)
    market_valid_count = int(market_valid.sum())
    share_valid_count = int(share_valid.sum())
    flow_valid_count = int(flow_valid.sum())
    market_coverage = market_valid_count / pool_count if pool_count else 0.0
    share_coverage = share_valid_count / pool_count if pool_count else 0.0
    flow_coverage = flow_valid_count / pool_count if pool_count else 0.0
    baseline_run = (
        flow_valid_count == 0
        and share_coverage >= float(SETTINGS["coverage_failure"])
        and int(baseline.sum()) >= int(share_valid.sum() * 0.8)
    )

    if market_coverage < float(SETTINGS["coverage_failure"]):
        issues.append(_issue("行情有效覆盖率", "ERROR", pool_count - market_valid_count,
                             coverage=market_coverage))
    elif market_coverage < float(SETTINGS["coverage_warning"]):
        issues.append(_issue("行情有效覆盖率", "WARNING", pool_count - market_valid_count,
                             coverage=market_coverage))

    if share_coverage < float(SETTINGS["coverage_failure"]):
        issues.append(_issue("份额有效覆盖率", "ERROR", pool_count - share_valid_count,
                             coverage=share_coverage))
    elif share_coverage < float(SETTINGS["coverage_warning"]):
        issues.append(_issue("份额有效覆盖率", "WARNING", pool_count - share_valid_count,
                             coverage=share_coverage))

    if baseline_run:
        issues.append(_issue("可靠份额基线", "WARNING", share_valid_count,
                             message="首次可靠快照仅建立基线，资金流保持 N/A"))
    elif flow_coverage < float(SETTINGS["coverage_failure"]):
        issues.append(_issue("资金流有效覆盖率", "ERROR", pool_count - flow_valid_count,
                             coverage=flow_coverage))
    elif flow_coverage < float(SETTINGS["coverage_warning"]):
        issues.append(_issue("资金流有效覆盖率", "WARNING", pool_count - flow_valid_count,
                             coverage=flow_coverage))

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
                 WHERE trade_date<? AND shares_date=trade_date GROUP BY instrument_id) p
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

    if "share_source_deviation" in facts:
        deviations = facts["share_source_deviation"].dropna()
        bad = deviations > float(SETTINGS["source_deviation_warning"])
        if bad.any():
            # 交易所份额为权威源，EM 仅为补充；两者间的份额差异属常态化系统性差异
            # （更新时间与口径不同），不应阻断流水线。仅作 WARNING 监控，
            # 真正的份额异常由"基金份额异常跳变"检查（与历史对比）兜底。
            issues.append(_issue("交易所与补充源份额偏差", "WARNING", bad.sum(),
                                 comparable=len(deviations), ratio=float(bad.mean())))

    if flow_valid_count:
        valid = facts.loc[flow_valid]
        unchanged_ratio = float(valid["estimated_net_flow"].eq(0).mean())
        changed_market = valid["pct_change"].abs().fillna(0) > 1e-9
        changed_ratio = float(changed_market.mean())
        if (
            unchanged_ratio > float(SETTINGS["unchanged_share_failure_ratio"])
            and changed_ratio > float(SETTINGS["changed_market_confirmation_ratio"])
        ):
            facts.loc[flow_valid, "flow_status"] = "ANOMALOUS"
            issues.append(_issue(
                "全市场份额静止异常", "ERROR", flow_valid_count,
                unchanged_ratio=unchanged_ratio, changed_market_ratio=changed_ratio,
            ))

    errors = [x for x in issues if x["severity"] == "ERROR"]
    stats = {
        "pool_count": pool_count,
        "valid_count": flow_valid_count,
        "coverage": flow_coverage,
        "market_valid_count": market_valid_count,
        "market_coverage": market_coverage,
        "share_valid_count": share_valid_count,
        "share_coverage": share_coverage,
        "baseline": baseline_run,
    }
    if errors:
        raise QualityGateError(issues)
    return issues, stats


def validate_metrics(facts: pd.DataFrame, instruments: pd.DataFrame,
                     category_metrics: pd.DataFrame, expected_date: str):
    """验证 1 日分类资金合计与 ETF 明细完全一致。"""
    current = facts.merge(
        instruments[["instrument_id", "primary_category"]],
        on="instrument_id", how="left",
    )
    detail = current.groupby("primary_category")["estimated_net_flow"].sum(min_count=1)
    one = category_metrics[category_metrics["window"] == 1].set_index("category")
    affected = 0
    for category, value in detail.items():
        metric = one.loc[category, "estimated_net_flow"] if category in one.index else None
        if pd.isna(value) and (metric is None or pd.isna(metric)):
            continue
        if metric is None or pd.isna(metric) or not math.isclose(
            float(value), float(metric), rel_tol=1e-9, abs_tol=1e-6
        ):
            # 分类覆盖不足时指标按设计为 N/A，不属于合计错误。
            row = one.loc[category] if category in one.index else None
            coverage = (
                float(row["valid_count"]) /
                max(float(row["valid_count"]) + float(row["missing_count"]), 1)
                if row is not None and pd.notna(row["valid_count"]) else 0
            )
            if coverage >= float(SETTINGS["coverage_failure"]):
                affected += 1
    if affected:
        raise QualityGateError([_issue(
            "分类与明细合计一致性", "ERROR", affected, trade_date=expected_date,
        )])

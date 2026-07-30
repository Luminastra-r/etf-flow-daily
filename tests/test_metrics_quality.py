import pandas as pd
import pytest

import db
from metrics import compute_metrics
from quality import QualityGateError, validate_snapshot


def _instrument():
    return pd.DataFrame([
        {"instrument_id": "SH.510300", "primary_category": "宽基"},
        {"instrument_id": "SH.510500", "primary_category": "宽基"},
    ])


def _facts(days=1):
    rows = []
    for i in range(days):
        day = f"2026-07-{20+i:02d}"
        for j, iid in enumerate(["SH.510300", "SH.510500"]):
            rows.append({
                "trade_date": day, "instrument_id": iid, "close": 4+i*.1+j,
                "pct_change": 1.0, "volume": 1, "amount": 1, "unit_nav": 4.0,
                "valuation_date": day, "shares": 100, "previous_aum": 400,
                "estimated_net_flow": 10 if j == 0 else -5, "flow_rate": .025 if j == 0 else -.0125,
                "shares_raw": 100, "shares_unit": "份", "shares_unit_factor": 1.0,
                "shares_date": day, "shares_source": "AKShare:SSE",
                "shares_updated_at": db.now_cn(), "flow_status": "VALID",
                "source": "AKShare", "data_status": "VALID", "collected_at": db.now_cn(),
            })
    return pd.DataFrame(rows)


def test_complete_windows_breadth_and_concentration(tmp_path):
    path = tmp_path / "metric.sqlite"
    db.migrate(path, create_backup=False)
    result = compute_metrics(_facts(5), _instrument(), "2026-07-24", path=path)
    one = result[result["window"] == 1].iloc[0]
    five = result[result["window"] == 5].iloc[0]
    assert one["breadth"] == pytest.approx(.5)
    assert five["available_days"] == 5
    assert five["estimated_net_flow"] == 25
    assert result[result["window"] == 20]["estimated_net_flow"].isna().all()


def test_quality_distinguishes_zero_and_missing(tmp_path):
    path = tmp_path / "quality.sqlite"
    db.migrate(path, create_backup=False)
    instruments = _instrument().assign(
        code=["510300", "510500"], exchange="SH", name=["a", "b"],
        fund_type="股票", list_date=None, delist_date=None,
        secondary_category="沪深300", tags_json="[]", track_index_code=None,
        track_index_name=None, source="AKShare", first_seen_date="2026-07-20",
        last_seen_date="2026-07-20", active=1, classification_version="test",
        updated_at=db.now_cn(),
    )
    facts = _facts(1)
    facts["estimated_net_flow"] = [0.0, None]
    facts["flow_status"] = ["VALID", "SOURCE_MISSING"]
    with pytest.raises(QualityGateError):
        validate_snapshot(instruments, facts, "2026-07-20", path)
    assert facts["estimated_net_flow"].iloc[0] == 0
    assert pd.isna(facts["estimated_net_flow"].iloc[1])
    with pytest.raises(QualityGateError):
        validate_snapshot(instruments, facts.iloc[0:0], "2026-07-20", path)

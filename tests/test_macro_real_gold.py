import pandas as pd
import pytest

import macro


def test_real_rate_uses_cpi_release_date_without_lookahead(monkeypatch):
    bond = pd.DataFrame({
        "date": pd.to_datetime(["2026-07-13", "2026-07-14", "2026-07-15"]),
        "cn10y": [2.0, 2.0, 2.0], "us10y": [4.4, 4.5, 4.6],
        "spread": [-2.4, -2.5, -2.6],
    })
    cpi = pd.DataFrame({
        "date": pd.to_datetime(["2026-06-10", "2026-07-14"]),
        "cpi_yoy": [3.0, 3.5], "cpi_period": ["2026-05", "2026-06"],
    })
    gold = pd.DataFrame({
        "date": pd.to_datetime(["2026-07-13", "2026-07-14", "2026-07-15"]),
        "gold": [3300.0, 3310.0, 3320.0],
    })
    monkeypatch.setattr(macro, "get_bond", lambda: bond)
    monkeypatch.setattr(macro, "get_cpi_yoy", lambda: cpi)
    monkeypatch.setattr(macro, "get_gold", lambda: gold)
    result = macro.get_real_rate_gold()
    before = result[result["date"] == pd.Timestamp("2026-07-13")].iloc[0]
    release = result[result["date"] == pd.Timestamp("2026-07-14")].iloc[0]
    assert before["real_rate"] == pytest.approx(1.4)
    assert release["real_rate"] == pytest.approx(1.0)
    assert release["cpi_release_date"] == pd.Timestamp("2026-07-14")
    assert release["gold"] == pytest.approx(3310.0)

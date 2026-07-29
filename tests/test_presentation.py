import pandas as pd

from presentation import format_money_wan, ranking_layout


def _frame(values):
    return pd.DataFrame({
        "secondary_category": [f"方向{i}" for i in range(len(values))],
        "estimated_net_flow": values,
        "etf_count": 1,
    })


def test_all_positive_and_negative_titles():
    positive = ranking_layout(_frame(range(1, 12)))
    negative = ranking_layout(_frame(range(-11, 0)))
    assert positive["status"] == "全部净流入"
    assert positive["lists"][1]["title"] == "流入较弱后 5"
    assert negative["status"] == "全部净流出"
    assert negative["lists"][0]["title"] == "流出较少前 5"


def test_small_lists_are_single_and_not_duplicated():
    for size in (4, 8):
        result = ranking_layout(_frame(range(size)))
        assert result["mode"] == "single"
        names = [x["secondary_category"] for x in result["lists"][0]["items"]]
        assert len(names) == len(set(names)) == size


def test_mixed_lists_are_disjoint_and_units_convert():
    result = ranking_layout(_frame([-5, -4, -3, -2, -1, 1, 2, 3, 4, 5, 6]))
    left = {x["secondary_category"] for x in result["lists"][0]["items"]}
    right = {x["secondary_category"] for x in result["lists"][1]["items"]}
    assert not left & right
    assert format_money_wan(-2747515) == "-274.75 亿元"


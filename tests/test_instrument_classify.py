import pandas as pd
import pytest

from classify import classify_etf, classify_frame
from instrument import infer_exchange, instrument_id, normalize_code


def test_code_normalization_and_exchange():
    assert normalize_code("sh.510300") == "510300"
    assert instrument_id("510300") == "SH.510300"
    assert instrument_id("SZ159919") == "SZ.159919"
    assert infer_exchange("159919") == "SZ"
    with pytest.raises(ValueError):
        normalize_code("123")


def test_classification_and_unclassified():
    assert classify_etf("国债ETF", fund_type="债券型")[0] == "固收"
    assert classify_etf("黄金ETF", fund_type="商品型")[0] == "商品"
    assert classify_etf("完全无法识别ETF")[0] == "未分类"
    frame = pd.DataFrame({"name": ["沪深300ETF", "未知ETF"], "fund_type": ["股票", "未知"]})
    result = classify_frame(frame)
    assert result["category"].tolist() == ["宽基", "未分类"]


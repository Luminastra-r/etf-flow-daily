"""排名语义与前端金额格式化所需的纯函数。"""
from __future__ import annotations

import pandas as pd


def format_money_wan(value) -> str:
    if value is None or pd.isna(value):
        return "N/A"
    value = float(value)
    if abs(value) >= 10000:
        return f"{value / 10000:+,.2f} 亿元"
    return f"{value:+,.0f} 万元"


def ranking_layout(frame: pd.DataFrame, n: int = 5) -> dict:
    """返回互斥榜单和条件化标题，不改变数值排序口径。"""
    if frame is None or frame.empty:
        return {"status": "历史不足", "mode": "single", "lists": [
            {"title": "暂无有效估算资金流", "items": []}
        ]}
    data = frame.dropna(subset=["estimated_net_flow"]).sort_values(
        "estimated_net_flow", ascending=False
    )
    if data.empty:
        return {"status": "历史不足", "mode": "single", "lists": [
            {"title": "暂无有效估算资金流", "items": []}
        ]}
    positive = int((data["estimated_net_flow"] > 0).sum())
    negative = int((data["estimated_net_flow"] < 0).sum())
    if negative == 0:
        status = "全部净流入" if positive == len(data) else "非负资金"
    elif positive == 0:
        status = "全部净流出" if negative == len(data) else "非正资金"
    else:
        status = f"{positive} 个方向净流入 · 资金分化"

    records = data.to_dict("records")
    if len(data) <= 9:
        return {"status": status, "mode": "single", "lists": [
            {"title": "完整排名", "items": records}
        ]}
    if negative == 0:
        left_title, right_title = f"净流入前 {n}", f"流入较弱后 {n}"
    elif positive == 0:
        left_title, right_title = f"流出较少前 {n}", f"净流出前 {n}"
    else:
        left_title, right_title = f"净流入前 {n}", f"净流出前 {n}"
    top = data.head(n)
    bottom = data.tail(n).sort_values("estimated_net_flow")
    top_ids = set(top["secondary_category"])
    bottom = bottom[~bottom["secondary_category"].isin(top_ids)]
    return {"status": status, "mode": "double", "lists": [
        {"title": left_title, "items": top.to_dict("records")},
        {"title": right_title, "items": bottom.to_dict("records")},
    ]}


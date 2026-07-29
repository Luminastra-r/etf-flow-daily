# 大类 ETF 资金流向日报

面向行业 ETF 和大类 ETF 观察的低频静态日报。项目每日通过 GitHub Actions 运行，以
AKShare 为 ETF 主数据源、Baostock 为交易日历和沪深300验证源，SQLite 是唯一历史库，
构建结果由 Cloudflare Pages 直接发布。

## 数据口径

**估算资金净流入（万元）**

```text
（当日基金份额 − 前一有效交易日基金份额）
× 当日单位净值 × 份额单位系数 ÷ 10,000
```

该指标不是交易所公布的真实资金流。QDII、港股和海外 ETF 可能存在净值日期滞后，
相关记录标记为 `PARTIAL`。缺失数据保持为空，不使用 `0` 代替。

资金流入率使用前一交易日份额 × 前一交易日单位净值作为规模分母。分母不可得时显示
`N/A`，不会静默改用其他口径。

## 运行

```bash
pip install -r requirements.txt
python run_daily.py
python run_daily.py --trade-date 2026-07-28 --force-refresh
python run_daily.py --rebuild-page
```

默认按 `Asia/Shanghai` 获取最近已完成交易日。非交易日记录 `SKIPPED`，不写入市场事实。
`--rebuild-page` 只读取 SQLite 并重建静态站。

本地查看页面：

```bash
python -m http.server 8000 --directory output
```

然后访问 `http://localhost:8000/`。

## 数据管道

```text
AKShare / Baostock
→ 内存 staging
→ 代码标准化与跨源去重
→ 数据质量门禁
→ 单事务 UPSERT 主数据、事实和指标
→ 临时目录构建 HTML / CSS / JS / JSON
→ 静态文件校验
→ 原子替换 output
→ GitHub Actions 提交 SQLite 与 output
```

覆盖率低于 95% 产生警告，低于 80% 阻止正式写入和发布。空 staging、日期错配、重复主键、
非法负值等错误同样阻止发布，上一版正常页面不会被覆盖。

## SQLite v2

- `etf_instrument`：ETF 主数据，主键形如 `SH.510300`。
- `etf_daily_fact`：每日事实，唯一键 `(trade_date, instrument_id)`。
- `category_daily_metric`：1/5/20/60 日分类指标。
- `pipeline_run`、`quality_issue`：运行状态和质量问题。
- `schema_migrations`：幂等迁移记录。

旧 `etf_daily`、`category_daily` 和 `meta` 表保留为只读历史。首次 v2 迁移前会在
`data/backups/` 创建一次本地备份，该目录不会提交到 Git。

## 静态输出

```text
output/
├─ index.html
├─ market.html
├─ methodology.html
├─ assets/
│  ├─ app.js
│  ├─ charts.js
│  └─ style.css
└─ data/
   ├─ latest.json
   ├─ overview.json
   ├─ category_latest.json
   ├─ industry_latest.json
   ├─ market_context.json
   └─ history/category_YYYY.json
```

首页只加载最新和短周期聚合数据，不再内嵌全部历史。

## 测试

```bash
python -m pytest -q
python -m py_compile *.py
node --check web/app.js
node --check web/charts.js
```

真实数据完整运行仍受 AKShare/Baostock 当时可用性影响；接口失败不会使用其他数据源补造。

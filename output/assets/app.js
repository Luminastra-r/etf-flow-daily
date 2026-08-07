(() => {
  const state = {period: 1, overview: null, rankings: null, industry: null, latest: null, daily: null};
  const money = v => v == null ? "N/A" : Math.abs(v)>=10000 ? `${v>=0?"+":""}${(v/10000).toFixed(2)} 亿元` : `${v>=0?"+":""}${Math.round(v).toLocaleString()} 万元`;
  const pct = (v, digits=1) => v == null ? "N/A" : `${(v*100).toFixed(digits)}%`;
  const cls = v => v == null ? "na" : v >= 0 ? "positive" : "negative";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const get = url => fetch(url).then(r => { if(!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); });
  const warningText = value => {
    const text=typeof value==="string"?value:JSON.stringify(value), ratio=text.match(/'ratio':\s*([0-9.]+)/), coverage=text.match(/'coverage':\s*([0-9.]+)/);
    if(text.startsWith("未分类ETF")&&ratio)return `未分类 ETF 占比 ${pct(Number(ratio[1]))}`;
    if(coverage)return `${text.split(":",1)[0]} ${pct(Number(coverage[1]))}`;
    return text.replace(/:\s*\{\}\s*$/,"");
  };

  function renderStatus() {
    const d=state.latest, el=document.querySelector("#status-panel"); if(!el)return;
    const warnings=(d.warnings||[]).map(warningText);
    const incomplete=d.status!=="VALID"||d.coverage<.95;
    const alert=(incomplete||warnings.length)?`<div class="quality-alert ${incomplete?"quality-alert-low":""}"><strong>${incomplete?"数据尚不完整，已启用错峰补跑":"数据提示"}</strong>${warnings.length?`<ul>${warnings.slice(0,3).map(x=>`<li>${esc(x)}</li>`).join("")}${warnings.length>3?`<li>另有 ${warnings.length-3} 项提示</li>`:""}</ul>`:""}</div>`:"";
    el.innerHTML=`<span class="status-pill">${esc(d.status)}</span>
      <div class="status-row"><span>数据交易日</span><b>${esc(d.trade_date)}</b></div>
      <div class="status-row"><span>全量 ETF 池</span><b>${d.pool_count.toLocaleString()} 只</b></div>
      <div class="status-row"><span>资金流有效 / 缺失</span><b>${d.valid_count} / ${d.missing_count}</b></div>
      <div class="status-row"><span>资金流 / 行情覆盖</span><b class="${d.coverage<.8?"negative":"positive"}">${pct(d.coverage)} / ${pct(d.market_coverage)}</b></div>
      <div class="status-row"><span>分类版本</span><b>${esc(d.classification_version)}</b></div>${alert}`;
  }
  function themeRows(items) {
    return items.map((r,i)=>`<div class="ledger-theme-row">
      <span class="theme-rank">${String(i+1).padStart(2,"0")}</span>
      <b>${esc(r.theme)}</b><span>${r.flow_valid_count}/${r.etf_count}只</span>
      <span class="${cls(r.estimated_net_flow_wan)}" title="${r.estimated_net_flow_wan==null?"不可计算":`${Number(r.estimated_net_flow_wan).toLocaleString()} 万元`}">${money(r.estimated_net_flow_wan)}</span>
      <span class="${cls(r.equal_weight_return)}">${pct(r.equal_weight_return,2)}</span>
    </div>`).join("");
  }
  function renderDailyTable() {
    const d=state.daily, root=document.querySelector("#daily-table-root"), meta=document.querySelector("#daily-ledger-meta");
    if(!root||!meta)return;
    meta.classList.remove("skeleton");
    meta.innerHTML=`<span class="flow-state ${esc(d.flow_status.toLowerCase())}">${esc(d.flow_status)}</span>
      <b>${esc(d.trade_date)}</b><span>${d.flow_valid_count}/${d.classified_count} 只可计算 · 覆盖 ${pct(d.flow_coverage)}</span>`;
    const signalItems=(d.signals?.contrarian_inflows||[]).slice(0,3);
    const signalStrip=signalItems.length?`<section class="signal-strip"><div><span>逆势承接观察</span><small>${esc(d.signals.disclaimer)}</small></div>${signalItems.map(s=>`<article><b>${esc(s.theme)}</b><em class="positive">${money(s.estimated_net_flow_wan)}</em><strong class="negative">${pct(s.equal_weight_return,2)}</strong><small>${esc(s.category)}</small></article>`).join("")}</section>`:"";
    const categories=d.categories.map(c=>{
      let detail;
      if(c.ranking_mode==="full") {
        detail=`<div class="ledger-rank-block full"><h4>完整排名</h4>${themeRows(c.full_ranking)||'<p class="na">可靠资金流不足</p>'}</div>`;
      } else {
        detail=`<div class="ledger-rank-block"><h4>净流入前三</h4>${themeRows(c.top_inflows)||'<p class="na">无可靠正流入主题</p>'}</div>
          <div class="ledger-rank-block"><h4>净流出前三</h4>${themeRows(c.top_outflows)||'<p class="na">无可靠净流出主题</p>'}</div>`;
      }
      return `<tr class="ledger-category-row">
        <td><span class="category-signal"></span><b>${esc(c.category)}</b><small>${esc(c.status)}</small></td>
        <td>${c.flow_valid_count} / ${c.etf_count}</td>
        <td class="${cls(c.estimated_net_flow_wan)}" title="${c.estimated_net_flow_wan==null?"覆盖不足或正在建立基线":`${Number(c.estimated_net_flow_wan).toLocaleString()} 万元`}">${money(c.estimated_net_flow_wan)}</td>
        <td class="${cls(c.equal_weight_return)}">${pct(c.equal_weight_return,2)}</td>
      </tr><tr class="ledger-detail-row"><td colspan="4"><div class="ledger-rank-grid ${c.ranking_mode}">${detail}</div></td></tr>`;
    }).join("");
    root.innerHTML=`${signalStrip}<table class="daily-table"><thead><tr><th>ETF 大类</th><th>可计算 / ETF只数</th><th>估算净申购</th><th>等权平均涨跌</th></tr></thead>
      <tbody>${categories}<tr class="ledger-total-row"><td><b>已分类总计</b><small>未分类 ${d.unclassified_count} 只另行披露</small></td>
      <td>${d.flow_valid_count} / ${d.classified_count}</td><td class="${cls(d.estimated_net_flow_wan)}">${money(d.estimated_net_flow_wan)}</td>
      <td class="${cls(d.equal_weight_return)}">${pct(d.equal_weight_return,2)}</td></tr></tbody></table>
      <p class="ledger-note">${esc(d.universe?.label||"全市场上市 ETF")}口径。${esc(d.universe?.note||"")}</p>`;
  }
  function rows() { return state.overview.by_period[String(state.period)] || []; }
  function total(field) { const xs=rows().map(r=>r[field]).filter(v=>v!=null); return xs.length ? xs.reduce((a,b)=>a+b,0) : null; }
  function renderPeriods() {
    const el=document.querySelector("#period-switch"); if(!el)return;
    el.innerHTML=state.overview.periods.map(p=>`<button data-period="${p}" class="${p===state.period?"active":""}">${p}日</button>`).join("");
    el.querySelectorAll("button").forEach(b=>b.onclick=()=>{state.period=Number(b.dataset.period);renderDashboard();});
  }
  function renderKpis() {
    const data=rows(), flow=total("estimated_net_flow"), breadths=data.map(r=>r.breadth).filter(v=>v!=null);
    const strongest=data.filter(r=>r.estimated_net_flow!=null).sort((a,b)=>b.estimated_net_flow-a.estimated_net_flow)[0];
    const resonance=data.filter(r=>r.observation_status==="资金价格共振").length;
    const breadth=breadths.length?breadths.reduce((a,b)=>a+b,0)/breadths.length:null;
    document.querySelector("#kpi-grid").innerHTML=[
      ["估算资金净流入",money(flow),cls(flow)],["平均资金广度",pct(breadth),"na"],
      ["资金最强方向",strongest?.category||"N/A",strongest?"positive":"na"],["资金价格共振",data.length?`${resonance} 个方向`:"N/A","na"]
    ].map(x=>`<article class="kpi"><div class="label">${x[0]}</div><div class="value ${x[2]}">${esc(x[1])}</div></article>`).join("");
  }
  function renderBrief() {
    const data=rows(), flow=total("estimated_net_flow"), valid=data.filter(r=>r.estimated_net_flow!=null);
    let text;
    if(!valid.length) text=`${state.period} 日完整历史不足，资金指标保持 <strong>N/A</strong>；当前覆盖率 ${pct(state.latest.coverage)}，没有用零值替代缺失数据。`;
    else {
      const sorted=[...valid].sort((a,b)=>b.estimated_net_flow-a.estimated_net_flow), first=sorted[0], last=sorted.at(-1);
      const direction=flow>=0?"净流入":"净流出";
      text=`全市场录得 <strong>${money(flow)} ${direction}</strong>。主要流入方向为 <strong>${esc(first.category)}</strong>，相对偏弱方向为 ${esc(last.category)}。${valid.filter(r=>r.inflow_streak>=2).length} 个方向出现连续流入。`;
    }
    if(state.latest.warnings?.length) text += ` 当前有 ${state.latest.warnings.length} 项数据提示。`;
    document.querySelector("#auto-brief").innerHTML=text;
  }
  function renderIndustry() {
    const body=document.querySelector("#industry-table tbody"); if(!body)return;
    const data=state.industry.filter(r=>r.window===state.period).sort((a,b)=>(b.estimated_net_flow??-Infinity)-(a.estimated_net_flow??-Infinity));
    body.innerHTML=data.length?data.map(r=>`<tr><td><b>${esc(r.secondary_category)}</b></td><td class="${cls(r.estimated_net_flow)}">${money(r.estimated_net_flow)}</td><td>${pct(r.flow_rate,2)}</td><td>${pct(r.breadth)}</td><td class="${cls(r.price_return)}">${pct(r.price_return,2)}</td><td>${r.etf_count}</td><td>${(r.representatives||[]).map(esc).join(" · ")||"N/A"}</td></tr>`).join(""):`<tr><td colspan="7" class="na">完整 ${state.period} 日历史不足</td></tr>`;
  }
  function renderRankings() {
    const root=document.querySelector("#ranking-grid"); if(!root)return;
    const groups=state.rankings[String(state.period)]||[];
    root.innerHTML=groups.length?groups.map(g=>`<article class="rank-category"><div class="rank-title"><h3>${esc(g.category)}</h3><span class="rank-status">${esc(g.status)}</span></div>
      <div class="rank-lists ${g.mode}">${g.lists.map(list=>`<div class="rank-list"><h4>${esc(list.title)}</h4>${list.items.map((r,i)=>`<div class="table-scroll"><div class="rank-item"><span class="rank-no">${String(i+1).padStart(2,"0")}</span><b>${esc(r.secondary_category)}</b><span>${r.etf_count}只</span><span class="${cls(r.estimated_net_flow)}">${money(r.estimated_net_flow)}</span><span>${pct(r.price_return,2)}</span></div></div>`).join("")||`<p class="na">历史不足</p>`}</div>`).join("")}</div></article>`).join(""):`<div class="panel na">完整 ${state.period} 日历史不足，暂无排名。</div>`;
  }
  function renderDashboard() {
    renderPeriods();renderKpis();renderBrief();renderIndustry();renderRankings();
    FlowCharts.contribution(document.querySelector("#category-chart"),rows());
    FlowCharts.quadrant(document.querySelector("#quadrant-chart"),rows());
  }
  async function dashboard() {
    [state.latest,state.daily,state.overview,state.rankings,state.industry]=await Promise.all([
      get("data/latest.json"),get("data/daily_table.json"),get("data/overview.json"),get("data/category_latest.json"),get("data/industry_latest.json")
    ]);
    state.period=state.overview.default_period;renderStatus();renderDailyTable();renderDashboard();
  }
  async function market() {
    const data=await get("data/market_context.json");document.querySelector("#market-status").textContent=data.status;
    const root=document.querySelector("#market-grid");
    if(!data.series?.length){root.innerHTML='<article class="panel na">当前没有通过健康检查的市场辅助字段。</article>';return;}
    data.series.forEach(item=>{const card=document.createElement("article");card.className=`market-card ${item.chart==="real_gold"?"market-card-wide":""}`;card.dataset.state=item.state;
      const latest=(item.data||[]).filter(r=>r.real_rate!=null).at(-1);
      const kpis=item.chart==="real_gold"&&latest?`<div class="market-kpis"><span>实际利率 <b>${Number(latest.real_rate).toFixed(2)}%</b></span><span>美债10Y <b>${Number(latest.us10y).toFixed(2)}%</b></span><span>CPI同比 <b>${Number(latest.cpi_yoy).toFixed(2)}%</b></span><span>CPI发布 <b>${esc(String(latest.cpi_release_date||"N/A").slice(0,10))}</b></span></div>`:"";
      card.insertAdjacentHTML("beforeend",`<div class="market-meta"><span>${esc(item.source)}</span><b>${esc(item.state)}</b><time>${esc(item.as_of||"N/A")}</time></div>${kpis}<div class="market-chart"></div>${item.note?`<p class="market-note">${esc(item.note)}</p>`:""}`);
      root.appendChild(card);if(item.chart==="real_gold")FlowCharts.realGold(card.querySelector(".market-chart"),item.data||[]);else FlowCharts.line(card.querySelector(".market-chart"),item.data||[],item.field,item.label);});
  }
  async function methodology() {
    const d=await get("data/latest.json"), el=document.querySelector("#quality-live");
    el.innerHTML=`<div class="status-row"><span>数据交易日</span><b>${d.trade_date}</b></div><div class="status-row"><span>覆盖率</span><b>${pct(d.coverage)}</b></div><div class="status-row"><span>分类版本</span><b>${esc(d.classification_version)}</b></div><div class="warning-list">${(d.warnings||[]).map(x=>`<p>△ ${esc(warningText(x))}</p>`).join("")||"<p>无构建警告</p>"}</div>`;
  }
  document.addEventListener("DOMContentLoaded",()=>{const page=document.body.dataset.page;({dashboard,market,methodology}[page]?.()).catch(err=>{console.error(err);document.querySelector("main").insertAdjacentHTML("afterbegin",`<p class="negative">数据加载失败：${esc(err.message)}</p>`);});});
})();

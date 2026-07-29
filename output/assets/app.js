(() => {
  const state = {period: 1, overview: null, rankings: null, industry: null, latest: null};
  const money = v => v == null ? "N/A" : Math.abs(v)>=10000 ? `${v>=0?"+":""}${(v/10000).toFixed(2)} 亿元` : `${v>=0?"+":""}${Math.round(v).toLocaleString()} 万元`;
  const pct = (v, digits=1) => v == null ? "N/A" : `${(v*100).toFixed(digits)}%`;
  const cls = v => v == null ? "na" : v >= 0 ? "positive" : "negative";
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const get = url => fetch(url).then(r => { if(!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); });

  function renderStatus() {
    const d=state.latest, el=document.querySelector("#status-panel"); if(!el)return;
    el.innerHTML=`<span class="status-pill">${esc(d.status)}</span>
      <div class="status-row"><span>数据交易日</span><b>${esc(d.trade_date)}</b></div>
      <div class="status-row"><span>全量 ETF 池</span><b>${d.pool_count.toLocaleString()} 只</b></div>
      <div class="status-row"><span>有效 / 缺失</span><b>${d.valid_count} / ${d.missing_count}</b></div>
      <div class="status-row"><span>覆盖率</span><b class="${d.coverage<.95?"negative":"positive"}">${pct(d.coverage)}</b></div>
      <div class="status-row"><span>分类版本</span><b>${esc(d.classification_version)}</b></div>`;
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
    body.innerHTML=data.length?data.map(r=>`<tr><td><b>${esc(r.secondary_category)}</b></td><td class="${cls(r.estimated_net_flow)}">${money(r.estimated_net_flow)}</td><td>${pct(r.flow_rate,2)}</td><td>${pct(r.breadth)}</td><td class="${cls(r.price_return)}">${r.price_return==null?"N/A":`${Number(r.price_return).toFixed(2)}%`}</td><td>${r.etf_count}</td><td>${(r.representatives||[]).map(esc).join(" · ")||"N/A"}</td></tr>`).join(""):`<tr><td colspan="7" class="na">完整 ${state.period} 日历史不足</td></tr>`;
  }
  function renderRankings() {
    const root=document.querySelector("#ranking-grid"); if(!root)return;
    const groups=state.rankings[String(state.period)]||[];
    root.innerHTML=groups.length?groups.map(g=>`<article class="rank-category"><div class="rank-title"><h3>${esc(g.category)}</h3><span class="rank-status">${esc(g.status)}</span></div>
      <div class="rank-lists ${g.mode}">${g.lists.map(list=>`<div class="rank-list"><h4>${esc(list.title)}</h4>${list.items.map((r,i)=>`<div class="table-scroll"><div class="rank-item"><span class="rank-no">${String(i+1).padStart(2,"0")}</span><b>${esc(r.secondary_category)}</b><span>${r.etf_count}只</span><span class="${cls(r.estimated_net_flow)}">${money(r.estimated_net_flow)}</span><span>${r.price_return==null?"N/A":Number(r.price_return).toFixed(2)+"%"}</span></div></div>`).join("")||`<p class="na">历史不足</p>`}</div>`).join("")}</div></article>`).join(""):`<div class="panel na">完整 ${state.period} 日历史不足，暂无排名。</div>`;
  }
  function renderDashboard() {
    renderPeriods();renderKpis();renderBrief();renderIndustry();renderRankings();
    FlowCharts.contribution(document.querySelector("#category-chart"),rows());
    FlowCharts.quadrant(document.querySelector("#quadrant-chart"),rows());
  }
  async function dashboard() {
    [state.latest,state.overview,state.rankings,state.industry]=await Promise.all([
      get("data/latest.json"),get("data/overview.json"),get("data/category_latest.json"),get("data/industry_latest.json")
    ]);
    state.period=state.overview.default_period;renderStatus();renderDashboard();
  }
  async function market() {
    const data=await get("data/market_context.json");document.querySelector("#market-status").textContent=data.status;
    const definitions=[["index","close","沪深300"],["valuation","pe_ttm","沪深300 PE-TTM"],["usdcny","usdcny","美元兑离岸人民币"],["bond","spread","中美十年期利差"],["margin","margin","融资余额"],["dxy","dxy","美元指数"]];
    const root=document.querySelector("#market-grid");
    definitions.forEach(([key,field,label])=>{const card=document.createElement("article");card.className="market-card";root.appendChild(card);FlowCharts.line(card,data.series[key]||[],field,label);});
  }
  async function methodology() {
    const d=await get("data/latest.json"), el=document.querySelector("#quality-live");
    el.innerHTML=`<div class="status-row"><span>数据交易日</span><b>${d.trade_date}</b></div><div class="status-row"><span>覆盖率</span><b>${pct(d.coverage)}</b></div><div class="status-row"><span>分类版本</span><b>${esc(d.classification_version)}</b></div><div class="warning-list">${(d.warnings||[]).map(x=>`<p>△ ${esc(typeof x==="string"?x:JSON.stringify(x))}</p>`).join("")||"<p>无构建警告</p>"}</div>`;
  }
  document.addEventListener("DOMContentLoaded",()=>{const page=document.body.dataset.page;({dashboard,market,methodology}[page]?.()).catch(err=>{console.error(err);document.querySelector("main").insertAdjacentHTML("afterbegin",`<p class="negative">数据加载失败：${esc(err.message)}</p>`);});});
})();

(() => {
  const ink = "#e8ebdf", muted = "#92998d", line = "#263129", inflow = "#ff6b59", outflow = "#49c58c", blue = "#70a8ff";
  const layout = (extra = {}) => ({
    paper_bgcolor: "transparent", plot_bgcolor: "transparent", font: {color: muted, family: "IBM Plex Sans, sans-serif"},
    margin: {l: 56, r: 20, t: 20, b: 48}, xaxis: {gridcolor: line, zerolinecolor: ink},
    yaxis: {gridcolor: line, zerolinecolor: ink}, showlegend: false, ...extra
  });
  const config = {responsive: true, displayModeBar: false};

  window.FlowCharts = {
    contribution(el, rows) {
      const valid = rows.filter(r => r.estimated_net_flow != null).sort((a,b) => a.estimated_net_flow-b.estimated_net_flow);
      if (!valid.length) return this.empty(el, "历史不足，暂无可计算资金贡献");
      Plotly.react(el, [{type:"bar",orientation:"h",y:valid.map(r=>r.category),x:valid.map(r=>r.estimated_net_flow),
        marker:{color:valid.map(r=>r.estimated_net_flow>=0?inflow:outflow)},
        customdata:valid.map(r=>r.estimated_net_flow),hovertemplate:"%{y}<br>%{customdata:,.0f} 万元<extra></extra>"}],
        layout({xaxis:{gridcolor:line,zerolinecolor:ink,title:"万元"}}), config);
    },
    quadrant(el, rows) {
      const valid = rows.filter(r => r.flow_rate != null && r.relative_return != null);
      if (!valid.length) return this.empty(el, "完整窗口不足，四象限暂不可用");
      Plotly.react(el,[{type:"scatter",mode:"markers+text",x:valid.map(r=>r.relative_return*100),y:valid.map(r=>r.flow_rate*100),
        text:valid.map(r=>r.category),textposition:"top center",marker:{size:valid.map(r=>Math.max(12,Math.sqrt(r.valid_count||1)*5)),color:inflow,opacity:.8,line:{color:ink,width:1}},
        customdata:valid.map(r=>[r.estimated_net_flow,r.observation_status]),hovertemplate:"%{text}<br>相对收益 %{x:.2f}%<br>流入率 %{y:.3f}%<br>净流入 %{customdata[0]:,.0f} 万<br>%{customdata[1]}<extra></extra>"}],
        layout({xaxis:{gridcolor:line,zerolinecolor:ink,title:"相对沪深300 (%)"},yaxis:{gridcolor:line,zerolinecolor:ink,title:"资金流入率 (%)"}}),config);
    },
    line(el, rows, field, label) {
      const valid = rows.filter(r=>r.date && r[field]!=null);
      if (!valid.length) return this.empty(el, `${label} 暂无稳定数据`);
      Plotly.react(el,[{type:"scatter",mode:"lines",x:valid.map(r=>r.date),y:valid.map(r=>r[field]),line:{color:inflow,width:2},hovertemplate:"%{x}<br>%{y:,.3f}<extra></extra>"}],
        layout({title:{text:label,font:{color:ink,size:15},x:0},margin:{l:54,r:18,t:52,b:44}}),config);
    },
    realGold(el, rows) {
      const valid=rows.filter(r=>r.date&&(r.real_rate!=null||r.gold!=null));
      if(!valid.length)return this.empty(el,"实际利率与黄金暂时不可用");
      const traces=[
        {type:"scatter",mode:"lines",name:"美国实际利率",x:valid.map(r=>r.date),y:valid.map(r=>r.real_rate),connectgaps:false,
          line:{color:blue,width:2},fill:"tozeroy",fillcolor:"rgba(112,168,255,.10)",
          customdata:valid.map(r=>[r.us10y,r.cpi_yoy,r.cpi_release_date]),
          hovertemplate:"%{x|%Y-%m-%d}<br>实际利率 %{y:.2f}%<br>美债10Y %{customdata[0]:.2f}%<br>CPI同比 %{customdata[1]:.2f}%<br>CPI发布 %{customdata[2]}<extra></extra>"},
        {type:"scatter",mode:"lines",name:"COMEX黄金 GC=F",x:valid.map(r=>r.date),y:valid.map(r=>r.gold),yaxis:"y2",connectgaps:false,
          line:{color:"#eab464",width:2.3},hovertemplate:"%{x|%Y-%m-%d}<br>GC=F %{y:,.1f} 美元/盎司<extra></extra>"}
      ];
      Plotly.react(el,traces,layout({title:{text:"实际利率与黄金价格",font:{color:ink,size:17},x:0},showlegend:true,
        legend:{orientation:"h",x:0,y:1.11,font:{color:muted}},hovermode:"x unified",margin:{l:58,r:66,t:76,b:46},
        xaxis:{gridcolor:line},yaxis:{gridcolor:line,zeroline:true,zerolinecolor:ink,title:"实际利率 (%)"},
        yaxis2:{overlaying:"y",side:"right",showgrid:false,title:"GC=F (美元/盎司)"}}),config);
    },
    empty(el, message) { el.innerHTML = `<div style="height:100%;display:grid;place-items:center;color:${muted};font-size:13px">${message}</div>`; }
  };
})();

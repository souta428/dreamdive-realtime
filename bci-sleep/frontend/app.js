const stageColor = {
  Wake: "#60a5fa",
  Light_NREM_candidate: "#34d399",
  REM_candidate: "#f59e0b",
  Deep_candidate: "#a78bfa",
};

let hypnoChart, brainWavesChart, motionChart;

function fmt(n, d=2){ return (n===null||n===undefined||isNaN(n)) ? "—" : Number(n).toFixed(d); }

function makeHypno(ctx){
  return new Chart(ctx, {
    type: "line",
    data: { datasets: [{label:"Stage", data: [], stepped: true, borderWidth: 2, pointRadius: 0}]},
    options: {
      animation:false, parsing:false,
      scales: {
        x: { type: "time", time: { unit:"minute" }, ticks: { color:"#cbd5e1" }, grid: { color:"rgba(148,163,184,.2)"} },
        y: { min:0.5, max:3.5,
          ticks: { stepSize:0.5, color:"#cbd5e1",
            callback: (v)=>({1:"Deep",1.5:"REM",2:"Light",3:"Wake"}[v] ?? "") },
          grid: { color:"rgba(148,163,184,.2)" }
        }
      },
      plugins:{ legend:{ display:false },
        tooltip:{ callbacks:{
          label:(ctx)=> {
            const d=ctx.raw;
            return `${new Date(d.x).toLocaleTimeString()}  ${d.stage}  (conf ${fmt(d.conf,2)})`;
          }}}
      }
    }
  });
}

function makeBrainWaves(ctx){
  return new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {label:"θ/α", borderWidth:2, pointRadius:0, data:[], borderColor:"#22d3ee"},
        {label:"β (rel)", borderWidth:2, pointRadius:0, data:[], borderColor:"#f472b6"},
      ]
    },
    options: {
      animation:false, parsing:false,
      scales:{
        x:{ type:"time", time:{unit:"minute"}, ticks:{color:"#cbd5e1"}, grid:{color:"rgba(148,163,184,.2)"} },
        y:{ min:0, max:5, ticks:{ color:"#cbd5e1", stepSize:1 }, grid:{color:"rgba(148,163,184,.1)"} },
      },
      plugins:{ legend:{ labels:{ color:"#e2e8f0"} } }
    }
  });
}

function makeMotion(ctx){
  return new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {label:"Motion RMS", yAxisID:"y1", borderWidth:2, pointRadius:0, data:[], borderColor:"#f59e0b"},
        {label:"EOG sacc/s", yAxisID:"y2", borderWidth:2, pointRadius:0, data:[], borderColor:"#a78bfa"},
      ]
    },
    options: {
      animation:false, parsing:false,
      scales:{
        x:{ type:"time", time:{unit:"minute"}, ticks:{color:"#cbd5e1"}, grid:{color:"rgba(148,163,184,.2)"} },
        y1:{ position:"left", min:0, max:100, ticks:{ color:"#f59e0b", stepSize:20 }, grid:{color:"rgba(148,163,184,.1)"}, title:{display:true, text:"Motion RMS", color:"#f59e0b"} },
        y2:{ position:"right", min:-2, max:2, ticks:{ color:"#a78bfa", stepSize:1 }, grid:{display:false}, title:{display:true, text:"EOG sacc/s", color:"#a78bfa"} },
      },
      plugins:{ legend:{ labels:{ color:"#e2e8f0"} } }
    }
  });
}

async function fetchSeries(){
  // 現在のユーザー名を取得（グローバル変数またはURLから）
  let currentUser = window.CURRENT_USER;
  if (!currentUser) {
    // URLからユーザー名を抽出
    const pathParts = window.location.pathname.split('/').filter(p => p);
    if (pathParts.length > 0) {
      currentUser = pathParts[0];
    }
  }
  
  // APIリクエストURLを構築
  let apiUrl = `/api/series?limit=720`;
  if (currentUser) {
    apiUrl += `&user=${encodeURIComponent(currentUser)}`;
  }
  
  const r = await fetch(apiUrl);
  if(!r.ok) return null;
  return await r.json();
}

function colorForStage(stage){ return stageColor[stage] || "#94a3b8"; }

function updateBadges(meta, last){
  const csvBadge = document.getElementById("csvBadge");
  csvBadge.textContent = `CSV: ${meta.csv}`;
  const eogBadge = document.getElementById("eogBadge");
  const eogOn = (last?.eog_on||0) >= 0.5;
  eogBadge.textContent = `EOG: ${eogOn? "ON":"OFF"}`;
  eogBadge.className = "badge " + (eogOn ? "ok":"");

  const sigBadge = document.getElementById("sigBadge");
  sigBadge.textContent = `Signal: ${fmt(last?.signal,2)}`;
  sigBadge.className = "badge " + ((last?.signal||0) >= 0.3 ? "ok":"warn");

  const statusBadge = document.getElementById("statusBadge");
  statusBadge.textContent = `Status: ${new Date(meta.now).toLocaleTimeString()} 更新`;
}

function updateKPIs(last){
  document.getElementById("kStage").textContent = last?.stage || "—";
  document.getElementById("kConf").textContent = fmt(last?.confidence,2);
  document.getElementById("kThetaAlpha").textContent = fmt(last?.theta_alpha,2);
  document.getElementById("kBeta").textContent = fmt(last?.beta_rel,2);
  document.getElementById("kMotion").textContent = fmt(last?.motion_rms,3);
  document.getElementById("kEOG").textContent = fmt(last?.eog_sacc,2);
  document.getElementById("kFac").textContent = fmt(last?.fac_rate,2);
  document.getElementById("kSignal").textContent = fmt(last?.signal,2);
}

function render(series){
  const ptsHyp = series.rows.map(r=>({
    x: r.time,
    y: r.stage_num ?? null,
    stage: r.stage, conf: r.confidence
  })).filter(p => p.y !== null);

  const last = series.rows[series.rows.length-1];

  // データがない場合の処理
  if (series.rows.length === 0) {
    document.getElementById("noDataMessage").style.display = "block";
    document.getElementById("chartHypno").style.display = "none";
    document.getElementById("chartBrainWaves").style.display = "none";
    document.getElementById("chartMotion").style.display = "none";
    
    // KPIをリセット
    updateKPIs(null);
    updateBadges(series, null);
    return;
  } else {
    document.getElementById("noDataMessage").style.display = "none";
    document.getElementById("chartHypno").style.display = "block";
    document.getElementById("chartBrainWaves").style.display = "block";
    document.getElementById("chartMotion").style.display = "block";
  }

  hypnoChart.data.datasets[0].data = ptsHyp;
  hypnoChart.data.datasets[0].borderColor = ptsHyp.length ? colorForStage(ptsHyp[ptsHyp.length-1].stage) : "#38bdf8";
  hypnoChart.update();

  brainWavesChart.data.datasets[0].data = series.rows.map(r=>({x:r.time, y:r.theta_alpha}));
  brainWavesChart.data.datasets[1].data = series.rows.map(r=>({x:r.time, y:r.beta_rel}));
  brainWavesChart.update();

  motionChart.data.datasets[0].data = series.rows.map(r=>({x:r.time, y:r.motion_rms}));
  motionChart.data.datasets[1].data = series.rows.map(r=>({x:r.time, y:r.eog_sacc}));
  motionChart.update();

  updateBadges(series, last);
  updateKPIs(last);
}

async function tick(){
  try{
    console.log('Fetching data...');
    const s = await fetchSeries();
    console.log('Data received:', s ? 'success' : 'no data');
    if(s) {
      console.log('Rendering data, rows:', s.rows.length);
      render(s);
    }
  }catch(e){
    console.error('Error in tick():', e);
  }finally{
    setTimeout(tick, 5000);
  }
}

window.addEventListener("DOMContentLoaded", ()=>{
  console.log('DOM loaded, initializing charts...');
  hypnoChart = makeHypno(document.getElementById("chartHypno"));
  brainWavesChart = makeBrainWaves(document.getElementById("chartBrainWaves"));
  motionChart = makeMotion(document.getElementById("chartMotion"));
  console.log('Charts initialized, starting tick...');
  tick();
});

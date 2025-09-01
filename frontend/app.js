const stageColor = {
  Wake: "#60a5fa",
  Light_NREM_candidate: "#34d399",
  REM_candidate: "#f59e0b",
  Deep_candidate: "#a78bfa",
};

// ユーザー別の色を定義
const userColors = {
  mitachi: "#60a5fa",
  hiratsuka: "#34d399", 
  gotou: "#f59e0b",
  default: "#94a3b8"
};

let hypnoChart, brainWavesChart, motionChart, eogChart, facChart;

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
        {label:"Motion RMS", borderWidth:2, pointRadius:0, data:[], borderColor:"#f59e0b"},
      ]
    },
    options: {
      animation:false, parsing:false,
      scales:{
        x:{ type:"time", time:{unit:"minute"}, ticks:{color:"#cbd5e1"}, grid:{color:"rgba(148,163,184,.2)"} },
        y:{ min:0, max:100, ticks:{ color:"#f59e0b", stepSize:20 }, grid:{color:"rgba(148,163,184,.1)"}, title:{display:true, text:"Motion RMS", color:"#f59e0b"} },
      },
      plugins:{ legend:{ labels:{ color:"#e2e8f0"} } }
    }
  });
}

function makeEOG(ctx){
  return new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {label:"EOG sacc/s", borderWidth:2, pointRadius:0, data:[], borderColor:"#a78bfa"},
      ]
    },
    options: {
      animation:false, parsing:false,
      scales:{
        x:{ type:"time", time:{unit:"minute"}, ticks:{color:"#cbd5e1"}, grid:{color:"rgba(148,163,184,.2)"} },
        y:{ min:-2, max:2, ticks:{ color:"#a78bfa", stepSize:1 }, grid:{color:"rgba(148,163,184,.1)"}, title:{display:true, text:"EOG sacc/s", color:"#a78bfa"} },
      },
      plugins:{ legend:{ labels:{ color:"#e2e8f0"} } }
    }
  });
}

function makeFAC(ctx){
  return new Chart(ctx, {
    type: "line",
    data: {
      datasets: [
        {label:"FAC Rate", borderWidth:2, pointRadius:0, data:[], borderColor:"#10b981"},
      ]
    },
    options: {
      animation:false, parsing:false,
      scales:{
        x:{ type:"time", time:{unit:"minute"}, ticks:{color:"#cbd5e1"}, grid:{color:"rgba(148,163,184,.2)"} },
        y:{ min:0, max:5, ticks:{ color:"#10b981", stepSize:1 }, grid:{color:"rgba(148,163,184,.1)"}, title:{display:true, text:"FAC Rate", color:"#10b981"} },
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
    stage: r.stage, 
    conf: r.confidence,
    user: r.user || 'unknown',
    display_name: r.display_name || 'Unknown'
  })).filter(p => p.y !== null);

  const last = series.rows[series.rows.length-1];

  // データがない場合の処理
  if (series.rows.length === 0) {
    document.getElementById("noDataMessage").style.display = "block";
    document.getElementById("chartHypno").style.display = "none";
    document.getElementById("chartBrainWaves").style.display = "none";
    document.getElementById("chartMotion").style.display = "none";
    document.getElementById("chartEOG").style.display = "none";
    document.getElementById("chartFAC").style.display = "none";
    
    // KPIをリセット
    updateKPIs(null);
    updateBadges(series, null);
    return;
  } else {
    document.getElementById("noDataMessage").style.display = "none";
    document.getElementById("chartHypno").style.display = "block";
    document.getElementById("chartBrainWaves").style.display = "block";
    document.getElementById("chartMotion").style.display = "block";
    document.getElementById("chartEOG").style.display = "block";
    document.getElementById("chartFAC").style.display = "block";
  }

  // ユーザー別のデータセットを作成
  const userDatasets = {};
  ptsHyp.forEach(point => {
    const user = point.user;
    if (!userDatasets[user]) {
      userDatasets[user] = [];
    }
    userDatasets[user].push(point);
  });

  // チャートのデータセットを更新
  const datasets = [];
  Object.keys(userDatasets).forEach(user => {
    const color = userColors[user] || userColors.default;
    datasets.push({
      label: userDatasets[user][0]?.display_name || user,
      data: userDatasets[user],
      stepped: true,
      borderWidth: 2,
      pointRadius: 0,
      borderColor: color,
      backgroundColor: color
    });
  });

  hypnoChart.data.datasets = datasets;
  hypnoChart.update();

  // 脳波データもユーザー別に分ける
  const brainWaveDatasets = {
    theta_alpha: {},
    beta_rel: {}
  };

  series.rows.forEach(row => {
    const user = row.user || 'unknown';
    if (!brainWaveDatasets.theta_alpha[user]) {
      brainWaveDatasets.theta_alpha[user] = [];
      brainWaveDatasets.beta_rel[user] = [];
    }
    brainWaveDatasets.theta_alpha[user].push({x: row.time, y: row.theta_alpha});
    brainWaveDatasets.beta_rel[user].push({x: row.time, y: row.beta_rel});
  });

  const brainWaveDatasetsArray = [];
  Object.keys(brainWaveDatasets.theta_alpha).forEach(user => {
    const color = userColors[user] || userColors.default;
    brainWaveDatasetsArray.push({
      label: `${user} - θ/α`,
      data: brainWaveDatasets.theta_alpha[user],
      borderWidth: 2,
      pointRadius: 0,
      borderColor: color
    });
    brainWaveDatasetsArray.push({
      label: `${user} - β (rel)`,
      data: brainWaveDatasets.beta_rel[user],
      borderWidth: 2,
      pointRadius: 0,
      borderColor: color,
      borderDash: [5, 5]
    });
  });

  brainWavesChart.data.datasets = brainWaveDatasetsArray;
  brainWavesChart.update();

  // モーションデータもユーザー別に分ける
  const motionDatasets = {
    motion_rms: {},
    eog_sacc: {}
  };

  series.rows.forEach(row => {
    const user = row.user || 'unknown';
    if (!motionDatasets.motion_rms[user]) {
      motionDatasets.motion_rms[user] = [];
      motionDatasets.eog_sacc[user] = [];
    }
    motionDatasets.motion_rms[user].push({x: row.time, y: row.motion_rms});
    motionDatasets.eog_sacc[user].push({x: row.time, y: row.eog_sacc});
  });

  const motionDatasetsArray = [];
  Object.keys(motionDatasets.motion_rms).forEach(user => {
    const color = userColors[user] || userColors.default;
    motionDatasetsArray.push({
      label: `${user} - Motion RMS`,
      data: motionDatasets.motion_rms[user],
      borderWidth: 2,
      pointRadius: 0,
      borderColor: color
    });
  });

  motionChart.data.datasets = motionDatasetsArray;
  motionChart.update();

  // EOGデータもユーザー別に分ける
  const eogDatasets = {
    eog_sacc: {}
  };

  series.rows.forEach(row => {
    const user = row.user || 'unknown';
    if (!eogDatasets.eog_sacc[user]) {
      eogDatasets.eog_sacc[user] = [];
    }
    eogDatasets.eog_sacc[user].push({x: row.time, y: row.eog_sacc});
  });

  const eogDatasetsArray = [];
  Object.keys(eogDatasets.eog_sacc).forEach(user => {
    const color = userColors[user] || userColors.default;
    eogDatasetsArray.push({
      label: `${user} - EOG sacc/s`,
      data: eogDatasets.eog_sacc[user],
      borderWidth: 2,
      pointRadius: 0,
      borderColor: color
    });
  });

  eogChart.data.datasets = eogDatasetsArray;
  eogChart.update();

  // FACデータもユーザー別に分ける
  const facDatasets = {
    fac_rate: {}
  };

  series.rows.forEach(row => {
    const user = row.user || 'unknown';
    if (!facDatasets.fac_rate[user]) {
      facDatasets.fac_rate[user] = [];
    }
    facDatasets.fac_rate[user].push({x: row.time, y: row.fac_rate});
  });

  const facDatasetsArray = [];
  Object.keys(facDatasets.fac_rate).forEach(user => {
    const color = userColors[user] || userColors.default;
    facDatasetsArray.push({
      label: `${user} - FAC Rate`,
      data: facDatasets.fac_rate[user],
      borderWidth: 2,
      pointRadius: 0,
      borderColor: color
    });
  });

  facChart.data.datasets = facDatasetsArray;
  facChart.update();

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
  eogChart = makeEOG(document.getElementById("chartEOG")); // EOGチャートを追加
  facChart = makeFAC(document.getElementById("chartFAC")); // FACチャートを追加
  console.log('Charts initialized, starting tick...');
  tick();
});

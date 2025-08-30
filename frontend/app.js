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
        {label:"FAC rate", yAxisID:"y3", borderWidth:2, pointRadius:0, data:[], borderColor:"#10b981"},
      ]
    },
    options: {
      animation:false, parsing:false,
      scales:{
        x:{ type:"time", time:{unit:"minute"}, ticks:{color:"#cbd5e1"}, grid:{color:"rgba(148,163,184,.2)"} },
        y1:{ position:"left", min:0, max:100, ticks:{ color:"#f59e0b", stepSize:20 }, grid:{color:"rgba(148,163,184,.1)"}, title:{display:true, text:"Motion RMS", color:"#f59e0b"} },
        y2:{ position:"right", min:-2, max:2, ticks:{ color:"#a78bfa", stepSize:1 }, grid:{display:false}, title:{display:true, text:"EOG sacc/s", color:"#a78bfa"} },
        y3:{ position:"right", min:0, max:1, offset:true, ticks:{ color:"#10b981", stepSize:0.2 }, grid:{display:false}, title:{display:true, text:"FAC rate", color:"#10b981"} },
      },
      plugins:{ legend:{ labels:{ color:"#e2e8f0"} } }
    }
  });
}

async function fetchSeries(){
  const r = await fetch(`/api/series?limit=720`);
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

  const eyeBadge = document.getElementById("eyeBadge");
  const eye = (last?.eye_act || "");
  eyeBadge.textContent = `EyeAct: ${eye || '—'}`;
  eyeBadge.className = "badge " + (/look_(left|right)/.test(eye) ? "ok" : "");

  const facBadge = document.getElementById("facBadge");
  const fac = last?.fac_rate;
  facBadge.textContent = `FAC: ${fmt(fac,2)}`;
  facBadge.className = "badge " + ((fac||0) >= 0.02 ? "ok":"");

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

  hypnoChart.data.datasets[0].data = ptsHyp;
  hypnoChart.data.datasets[0].borderColor = ptsHyp.length ? colorForStage(ptsHyp[ptsHyp.length-1].stage) : "#38bdf8";
  hypnoChart.update();

  brainWavesChart.data.datasets[0].data = series.rows.map(r=>({x:r.time, y:r.theta_alpha}));
  brainWavesChart.data.datasets[1].data = series.rows.map(r=>({x:r.time, y:r.beta_rel}));
  brainWavesChart.update();

  motionChart.data.datasets[0].data = series.rows.map(r=>({x:r.time, y:r.motion_rms}));
  motionChart.data.datasets[1].data = series.rows.map(r=>({x:r.time, y:r.eog_sacc}));
  motionChart.data.datasets[2].data = series.rows.map(r=>({x:r.time, y:r.fac_rate}));
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

# server_frontend.py
import os, csv, time, glob
from pathlib import Path
from flask import Flask, jsonify, send_from_directory, request

app = Flask(__name__, static_folder="frontend", static_url_path="")

def resolve_csv_path():
    # タイムスタンプ付きファイルを優先的に検索（backupを除外）
    timestamp_files = [f for f in glob.glob("sleep_candidates_*.csv") if "backup" not in f]
    if timestamp_files:
        # 最新のファイルを選択（ファイル名でソート）
        latest_file = sorted(timestamp_files)[-1]
        return latest_file
    
    # 従来のファイル名もチェック
    fallback_candidates = ["sleep_candidates_eog.csv", "sleep_candidates.csv"]
    for name in fallback_candidates:
        p = Path(name)
        if p.exists():
            return str(p)
    
    # デフォルト
    return "sleep_candidates.csv"

STAGE_TO_NUM = {
    "Wake": 3.0,
    "Light_NREM_candidate": 2.0,
    "REM_candidate": 1.5,
    "Deep_candidate": 1.0,
    "": None,
    None: None,
}

def read_rows(limit=720):
    csv_path = resolve_csv_path()
    if not os.path.exists(csv_path):
        return []
    
    out = []
    session_start_time = None
    current_time = time.time()
    
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        
        # セッション開始時刻を推定（最初の行の相対時間から）
        if rows:
            first_relative_time = float(rows[0].get("time", 0))
            # 最初の行が収集された絶対時刻を推定
            session_start_time = current_time - (float(rows[-1].get("time", 0)) - first_relative_time)
        
        for row in rows[-limit:]:
            _f = lambda k: float(row.get(k) or 0)
            ts_relative = _f("time")
            
            # 相対時間を絶対時間（ミリ秒）に変換
            if session_start_time:
                ts_absolute = (session_start_time + ts_relative) * 1000
            else:
                ts_absolute = ts_relative * 1000
            
            stage = row.get("stage", "")
            num = {"Wake": 3, "Light_NREM_candidate": 2, "REM_candidate": 1.5, "Deep_candidate": 1}.get(stage)
            
            out.append({
                "time": ts_absolute,
                "stage": stage,
                "stage_num": num,
                "confidence": _f("confidence"),
                "theta_alpha": _f("theta_alpha"),
                "beta_rel": _f("beta_rel"),
                "motion_rms": _f("motion_rms"),
                "fac_rate": _f("fac_rate"),
                "signal": _f("signal"),
                "eog_sacc": _f("eog_sacc"),
                "eog_on": _f("eog_on"),
            })
    return out

@app.get("/api/series")
def api_series():
    try:
        limit = int(request.args.get("limit", "720"))
    except:
        limit = 720
    return jsonify({"rows": read_rows(limit), "csv": resolve_csv_path(), "now": int(time.time()*1000)})

@app.get("/")
def root():
    return send_from_directory("frontend", "index.html")

@app.get("/app.js")
def appjs():
    return send_from_directory("frontend", "app.js")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"[INFO] serving frontend at http://localhost:{port}")
    print(f"[INFO] watching CSV: {resolve_csv_path()}")
    app.run(host="0.0.0.0", port=port, debug=False)

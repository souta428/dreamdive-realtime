# server_frontend.py
import os, csv, time, glob
from pathlib import Path
from flask import Flask, jsonify, send_from_directory, request, render_template_string

app = Flask(__name__, static_folder="frontend", static_url_path="")

# ユーザーデータディレクトリを作成
USER_DATA_DIR = "user_data"
USER_CSV_FILE = os.path.join(USER_DATA_DIR, "users.csv")
os.makedirs(USER_DATA_DIR, exist_ok=True)

def resolve_csv_path(username=None):
    if username:
        # ユーザー管理CSVから最新のセッションファイルを取得
        latest_session_file = get_latest_session_file(username)
        if latest_session_file:
            return latest_session_file
        
        # セッションファイルが見つからない場合は空のデータを返す
        return None
    else:
        # 全体ダッシュボードでは全ユーザーの最新データを統合
        return "combined"  # 特別な値で統合データを示す

def get_latest_session_file(username):
    """ユーザー管理CSVから最新のセッションファイルを取得"""
    if not os.path.exists(USER_CSV_FILE):
        return None
    
    with open(USER_CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['username'] == username:
                last_session = row.get('last_session', '')
                if last_session:
                    # ユーザー別ディレクトリ内のセッションファイルを返す
                    session_file = os.path.join(USER_DATA_DIR, username, last_session)
                    if os.path.exists(session_file):
                        return session_file
                break
    
    return None

STAGE_TO_NUM = {
    "Wake": 3.0,
    "Light_NREM_candidate": 2.0,
    "REM_candidate": 1.5,
    "Deep_candidate": 1.0,
    "": None,
    None: None,
}

def read_combined_data(limit=720):
    """全ユーザーのデータを統合して読み込み"""
    all_rows = []
    registered_users = get_registered_users()
    
    for user in registered_users:
        if user['has_data']:
            user_rows = read_rows(limit, user['username'])
            # ユーザー情報を各行に追加
            for row in user_rows:
                row['user'] = user['username']
                row['display_name'] = user['display_name']
            all_rows.extend(user_rows)
    
    # 時間順にソート
    all_rows.sort(key=lambda x: x['time'])
    return all_rows[-limit:] if len(all_rows) > limit else all_rows

def read_rows(limit=720, username=None):
    csv_path = resolve_csv_path(username)
    if not csv_path:
        return []
    
    # 統合データの場合
    if csv_path == "combined":
        return read_combined_data(limit)
    
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

def get_registered_users():
    """CSVファイルから登録済みユーザーリストを取得"""
    if not os.path.exists(USER_CSV_FILE):
        return []
    
    users = []
    with open(USER_CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # データファイルの存在確認
            user_csv_path = resolve_csv_path(row['username'])
            has_data = user_csv_path is not None and os.path.exists(user_csv_path)
            
            users.append({
                'username': row['username'],
                'display_name': row['display_name'],
                'notes': row['notes'],
                'total_sessions': int(row['total_sessions']),
                'last_session': row['last_session'],
                'has_data': has_data
            })
    return users

def update_user_session(username, session_file):
    """ユーザーのセッション情報を更新"""
    if not os.path.exists(USER_CSV_FILE):
        return False
    
    # 全ユーザーを読み込み
    users = []
    with open(USER_CSV_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            users.append(row)
    
    # 指定ユーザーを更新
    for user in users:
        if user['username'] == username:
            user['last_session'] = session_file
            user['total_sessions'] = str(int(user['total_sessions']) + 1)
            break
    
    # CSVファイルを再書き込み
    with open(USER_CSV_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "username", "display_name", "created_date", 
            "last_session", "total_sessions", "notes"
        ])
        for user in users:
            writer.writerow([
                user['username'], user['display_name'], user['created_date'],
                user['last_session'], user['total_sessions'], user['notes']
            ])
    
    return True

def get_available_users():
    """利用可能なユーザーリストを取得（後方互換性のため）"""
    registered_users = get_registered_users()
    return [user['username'] for user in registered_users]

@app.get("/api/series")
def api_series():
    try:
        limit = int(request.args.get("limit", "720"))
    except:
        limit = 720
    username = request.args.get("user")
    
    # セッション情報を更新
    if username:
        csv_path = resolve_csv_path(username)
        if csv_path:
            update_user_session(username, os.path.basename(csv_path))
    
    return jsonify({
        "rows": read_rows(limit, username), 
        "csv": resolve_csv_path(username), 
        "now": int(time.time()*1000),
        "user": username
    })

@app.get("/api/users")
def api_users():
    """登録済みユーザーリストを返す"""
    return jsonify({"users": get_registered_users()})

@app.get("/")
def root():
    return send_from_directory("frontend", "index.html")

@app.get("/<username>")
def user_dashboard(username):
    """ユーザー別ダッシュボード"""
    # ユーザー名の検証（簡単な文字列チェック）
    if not username or len(username) > 50 or not username.replace('_', '').replace('-', '').isalnum():
        return "Invalid username", 400
    
    # 登録済みユーザーかチェック
    registered_users = get_registered_users()
    user_info = None
    for user in registered_users:
        if user['username'] == username:
            user_info = user
            break
    
    if not user_info:
        return "User not found", 404
    
    # ユーザー別のHTMLを生成
    html_template = """
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Sleep Candidates Dashboard - {{ user_info.display_name }}</title>
  <link rel="preconnect" href="https://cdn.jsdelivr.net"/>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0"></script>
  <style>
    :root { --bg:#0f172a; --fg:#e2e8f0; --muted:#94a3b8; --card:#111827; --accent:#38bdf8; }
    body { margin:0; background:var(--bg); color:var(--fg); font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto;}
    .wrap { max-width:1100px; margin:32px auto; padding:0 16px;}
    .row { display:grid; gap:16px; grid-template-columns: 1fr; }
    @media(min-width:900px){ .row{ grid-template-columns: 2fr 1fr; } }
    .card { background:var(--card); border-radius:16px; padding:16px; box-shadow: 0 10px 30px rgba(0,0,0,.25);}
    .title { font-size:18px; margin:4px 0 12px; color:#fff;}
    .badges { display:flex; gap:8px; align-items:center; flex-wrap:wrap;}
    .badge { font-size:12px; padding:6px 10px; border-radius:999px; background:#1f2937; color:#cbd5e1; }
    .badge.ok{ background:#064e3b; color:#86efac;}
    .badge.warn{ background:#3f1d2e; color:#fda4af;}
    .badge.user{ background:#1e40af; color:#93c5fd;}
    .badge.info{ background:#1e293b; color:#94a3b8;}
    .kpi { display:grid; grid-template-columns: repeat(4,1fr); gap:10px; margin-top:8px;}
    .kpi .card { padding:12px; text-align:center;}
    .kpi .num { font-size:20px; font-weight:700;}
    .kpi .lbl { font-size:12px; color:var(--muted);}
    canvas { width:100% !important; height:360px !important;}
    footer{ margin-top:24px; color:var(--muted); font-size:12px;}
    a{ color: var(--accent); text-decoration: none; }
    .user-nav { margin-bottom: 16px; }
    .user-nav a { margin-right: 12px; }
    .user-info { margin-bottom: 16px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="user-nav">
      <a href="/">← 全体ダッシュボード</a>
      <span class="badge user">{{ user_info.username }}</span>
    </div>
    
    <div class="user-info">
      <h1>{{ user_info.display_name }}</h1>
      <div class="badges">
        <span class="badge info">セッション数: {{ user_info.total_sessions }}</span>
        <span class="badge info">最終セッション: {{ user_info.last_session or "なし" }}</span>
        <span class="badge info">{{ user_info.notes }}</span>
      </div>
    </div>
    
    <div class="badges">
      <span id="csvBadge" class="badge">CSV: —</span>
      <span id="eogBadge" class="badge">EOG: —</span>
      <span id="sigBadge" class="badge">Signal: —</span>
      <span id="statusBadge" class="badge">Status: 初期化中</span>
    </div>

    <div class="row" style="margin-top:16px;">
      <div class="card">
        <div class="title">Hypnogram-like Timeline</div>
        <canvas id="chartHypno"></canvas>
      </div>
      <div class="card">
        <div class="title">Latest KPIs</div>
        <div class="kpi">
          <div class="card"><div class="num" id="kStage">—</div><div class="lbl">Stage</div></div>
          <div class="card"><div class="num" id="kConf">—</div><div class="lbl">Confidence</div></div>
          <div class="card"><div class="num" id="kThetaAlpha">—</div><div class="lbl">θ/α</div></div>
          <div class="card"><div class="num" id="kBeta">—</div><div class="lbl">β (rel)</div></div>
        </div>
        <div class="kpi" style="margin-top:8px;">
          <div class="card"><div class="num" id="kMotion">—</div><div class="lbl">Motion RMS</div></div>
          <div class="card"><div class="num" id="kEOG">—</div><div class="lbl">EOG sacc/s</div></div>
          <div class="card"><div class="num" id="kFac">—</div><div class="lbl">FAC rate</div></div>
          <div class="card"><div class="num" id="kSignal">—</div><div class="lbl">Signal</div></div>
        </div>
      </div>
    </div>

    <div class="row" style="margin-top:16px;">
      <div class="card">
        <div class="title">Brain Waves (θ/α & β)</div>
        <canvas id="chartBrainWaves"></canvas>
      </div>
      <div class="card">
        <div class="title">Motion & EOG</div>
        <canvas id="chartMotion"></canvas>
      </div>
    </div>

    <footer>
      <p>表示は 30s 窓・5s 更新の <em>候補ステージ</em>（Wake / Light / REM / Deep）。AASM正式判定ではありません。</p>
      <p>ユーザー: {{ user_info.username }} | {{ user_info.notes }}</p>
    </footer>
  </div>

  <script>
    // ユーザー名をグローバル変数として設定
    window.CURRENT_USER = "{{ user_info.username }}";
  </script>
  <script src="/app.js"></script>
</body>
</html>
    """
    return render_template_string(html_template, user_info=user_info)

@app.get("/app.js")
def appjs():
    return send_from_directory("frontend", "app.js")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"[INFO] serving frontend at http://localhost:{port}")
    print(f"[INFO] watching CSV: {resolve_csv_path()}")
    print(f"[INFO] user data directory: {USER_DATA_DIR}")
    print(f"[INFO] user management CSV: {USER_CSV_FILE}")
    app.run(host="0.0.0.0", port=port, debug=False)

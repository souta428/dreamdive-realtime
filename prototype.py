# -*- coding: utf-8 -*-
# cortex_dashboard.py
#
# Registered Developer(無料)で購読できるストリームをひとまとめに監視するダッシュボード
# 対応ストリーム: pow, met(低分解能), mot, eq, dev, (任意: fac, com)
#
# 使い方:
#   1) 下の CLIENT_ID / CLIENT_SECRET をあなたのものに置き換え
#   2) Emotiv アプリ起動 & ヘッドセット接続
#   3) pip install dash plotly websocket-client
#   4) python cortex_dashboard.py → http://127.0.0.1:8050

import json
import ssl
import threading
import time
from collections import deque, defaultdict

import websocket  # websocket-client
from websocket import WebSocketApp

from dash import Dash, dcc, html, Output, Input
import plotly.graph_objs as go

# ==== あなたの資格情報（Emotiv Developerで取得）====
CLIENT_ID = "elEQNmVZbVOzSyV6PskFbdUtlI6wKZD2ZZ4vOJC6"
CLIENT_SECRET = "AeMMNghneyGBXUs69MsXrKhq4nRIyXeCrc7k84z8X9a5ubt2HGMrk4i16vXd8Nnqu9N95WtdQinUirki3umAucLvpJ3BzmuQ2wWbdMf7uhj8AIv39fgAK9GHSG59yh56"
PROFILE_NAME = "default"  # Mental Commands等で使う場合のプロフィール名（任意）

# ==== Cortex 接続先 ====
CORTEX_URL = "wss://localhost:6868"

# ==== 受信データ用の共有バッファ（スレッド間共有。最新を保持）====
latest = {
    "eq": {},            # {electrode: quality(0-4)}
    "dev": {},           # {status, battery, ...} 受け取ったものをそのまま保持
    "met": {},           # 低分解能 Performance Metrics（engagement等）
    "pow": {},           # 周波数帯: {"theta":val, "alpha":val, ...} などに集計
    "mot": deque(maxlen=200),   # 時系列: 加速度/ジャイロなど
    "fac": {},           # 顔表情イベント
    "com": {},           # メンタルコマンドイベント
}
pow_bands = ["delta", "theta", "alpha", "betaL", "betaH", "gamma"]  # powの典型的並び
mot_fields = ["accX","accY","accZ","gyroX","gyroY","gyroZ"]         # motの代表成分

# pow はチャンネルごとに帯域が来るので、各帯域の平均で簡易可視化
def aggregate_pow(sample):
    # sample 例: {"pow": [ts, ch1_delta, ch1_theta, ..., chN_gamma]}
    if "pow" in sample:
        arr = sample["pow"]
        if not arr or len(arr) < 2:
            return

        # 先頭は timestamp 想定 → 実値のみ抽出
        values = arr[1:]
        bands = 6  # delta, theta, alpha, betaL, betaH, gamma
        if len(values) % bands != 0:
            return
        
        per_band = len(values) // bands
        agg = {}
        for i, band in enumerate(pow_bands):
            seg = values[i*per_band:(i+1)*per_band]
            if seg:
                agg[band] = sum(seg) / len(seg)
        latest["pow"] = agg
        print(f"POW aggregated: {agg}")

# mot（時系列）格納
def append_mot(sample):
    if "mot" in sample:
        arr = sample["mot"]
        if not arr:
            return
        # 先頭がtimestamp 以降が各値の想定
        if len(arr) >= 7:
            ts = arr[0]
            vals = arr[1:7]  # ACCX, ACCY, ACCZ, GYRIX, GYRIY, GYRIZ
            entry = dict(ts=ts)
            for i, k in enumerate(mot_fields):
                if i < len(vals):
                    entry[k] = vals[i]
            latest["mot"].append(entry)
            print(f"MOT data added: timestamp={ts}, values={len(vals)}")

# EQ（電極品質）
def update_eq(sample):
    # EQデータの処理を改善
    if "eq" in sample:
        arr = sample["eq"]
        if isinstance(arr, list) and len(arr) >= 2:
            # タイムスタンプ + 電極品質データの場合
            if isinstance(arr[1], list):
                # [timestamp, [ch1_quality, ch2_quality, ...]] 形式
                eq_data = arr[1]
                # 仮定: 14チャンネル（AF3, F7, F3, FC5, T7, P7, O1, O2, P8, T8, FC6, F4, F8, AF4）
                channels = ['AF3', 'F7', 'F3', 'FC5', 'T7', 'P7', 'O1', 'O2', 'P8', 'T8', 'FC6', 'F4', 'F8', 'AF4']
                for i, quality in enumerate(eq_data):
                    if i < len(channels):
                        latest["eq"][channels[i]] = quality
            else:
                # [ch, quality] ペア形式
                ch, q = arr[0], arr[1]
                latest["eq"][str(ch)] = q
        elif isinstance(arr, dict):
            latest["eq"].update(arr)

def update_met(sample):
    # METデータの処理を改善
    if "met" in sample:
        data = sample["met"]
        if isinstance(data, list) and len(data) >= 2:
            # [timestamp, [engagement, excitement, stress, relaxation, interest, focus]] 形式
            timestamp = data[0]
            if isinstance(data[1], list):
                met_values = data[1]
                met_labels = ["engagement", "excitement", "stress", "relaxation", "interest", "focus"]
                for i, value in enumerate(met_values):
                    if i < len(met_labels):
                        latest["met"][met_labels[i]] = value
        elif isinstance(data, dict):
            latest["met"].update(data)

def update_dev(sample):
    data = sample.get("dev")
    if isinstance(data, dict):
        latest["dev"].update(data)

def update_fac(sample):
    data = sample.get("fac")
    if isinstance(data, dict):
        latest["fac"].update(data)

def update_com(sample):
    data = sample.get("com")
    if isinstance(data, dict):
        latest["com"].update(data)

# ==== Cortex JSON-RPC ラッパ ====
class CortexClient:
    def __init__(self, url, client_id, client_secret, profile_name=None):
        self.url = url
        self.client_id = client_id
        self.client_secret = client_secret
        self.profile_name = profile_name
        self.ws: WebSocketApp | None = None
        self.req_id = 1
        self.token = None
        self.session_id = None
        self.subscribed = set()
        self.lock = threading.Lock()

    def _next_id(self):
        with self.lock:
            self.req_id += 1
            return self.req_id

    def send(self, method, params=None):
        if params is None:
            params = {}
        req = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params
        }
        try:
            self.ws.send(json.dumps(req))
        except Exception as e:
            print("Send error:", e)

    # ---- 主要フロー ----
    def authorize(self):
        self.send("authorize", {
            "clientId": self.client_id,
            "clientSecret": self.client_secret
        })

    def query_headsets(self):
        self.send("queryHeadsets")

    def create_session(self, headset_id):
        self.send("createSession", {
            "cortexToken": self.token,
            "headset": headset_id,
            "status": "active"
        })

    def subscribe(self, streams):
        want = list(streams)
        self.send("subscribe", {
            "cortexToken": self.token,
            "session": self.session_id,
            "streams": want
        })

    def on_open(self, ws):
        print("Cortex connected.")
        # Authorize
        self.authorize()

    def on_message(self, ws, message):
        try:
            msg = json.loads(message)
        except:
            return

        # レスポンス or プッシュかで分岐
        # 1) authorize 取得
        if "result" in msg and isinstance(msg["result"], dict):
            res = msg["result"]
            if "cortexToken" in res:
                self.token = res["cortexToken"]
                print("Authorized. Token acquired.")
                # ヘッドセット検索
                self.query_headsets()
                return
            # queryHeadsets の結果
            if "headsets" in res:
                headsets = res["headsets"]
                if not headsets:
                    print("No headset found. Connect a headset in Emotiv app.")
                    return
                hs_id = headsets[0].get("id")
                print("Using headset:", hs_id)
                self.create_session(hs_id)
                return
            # createSession の結果
            if "id" in res and isinstance(res["id"], str):
                self.session_id = res["id"]
                print("Session created:", self.session_id)
                # 無料プランで購読できる主要ストリームをまとめて購読
                # 必要に応じて fac/com も有効化
                streams = ["pow", "met", "mot", "eq", "dev"]  # + ["fac","com"]
                self.subscribe(streams)
                return

        # 2) サブスクリプションのACK
        if "result" in msg and isinstance(msg["result"], list):
            # subscribe の戻りが配列で来ることがある
            print("Subscribed:", msg["result"])
            return

        # 3) データサンプル（push通知）
        if "method" in msg and msg["method"] == "subscription":
            params = msg.get("params", {})
            # データ受信をデバッグ出力
            print(f"Received data: {list(params.keys())}")
            
            # 各ストリームごとに分岐
            if "pow" in params:
                aggregate_pow(params)
                print("POW data processed")
            if "mot" in params:
                append_mot(params)
                print("MOT data processed")
            if "eq" in params:
                update_eq(params)
                print("EQ data processed")
            if "met" in params:
                update_met(params)
                print("MET data processed")
            if "dev" in params:
                update_dev(params)
                print("DEV data processed")
            if "fac" in params:
                update_fac(params)
                print("FAC data processed")
            if "com" in params:
                update_com(params)
                print("COM data processed")

        # 4) エラー
        if "error" in msg:
            print("Cortex error:", msg["error"])

    def on_close(self, ws, close_status_code, close_msg):
        print("Cortex closed:", close_status_code, close_msg)

    def on_error(self, ws, error):
        print("WS error:", error)

    def run_forever(self):
        sslopt = {"cert_reqs": ssl.CERT_NONE}  # 自己署名証明書を許可
        self.ws = websocket.WebSocketApp(
            self.url,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever(sslopt=sslopt)

# ==== 受信スレッド起動 ====
client = CortexClient(CORTEX_URL, CLIENT_ID, CLIENT_SECRET, PROFILE_NAME)
t = threading.Thread(target=client.run_forever, daemon=True)
t.start()

# ==== 診断関数 ====
def get_connection_status():
    """接続状態とデータ受信状況を診断"""
    status = {
        'general': '',
        'eq': '',
        'pow': '',
        'met': '',
        'mot': '',
        'dev': ''
    }
    
    # 基本的な接続状態
    if not client.token:
        status['general'] = "❌ Cortexに認証できていません"
        for key in ['eq', 'pow', 'met', 'mot', 'dev']:
            status[key] = "認証が必要です"
        return status
    
    if not client.session_id:
        status['general'] = "❌ セッションが作成されていません"
        for key in ['eq', 'pow', 'met', 'mot', 'dev']:
            status[key] = "セッション作成が必要です"
        return status
    
    status['general'] = "✅ Cortex接続済み"
    
    # 各データストリームの状態
    if not latest["eq"]:
        status['eq'] = "データなし - ヘッドセットの電極接触を確認してください"
    else:
        status['eq'] = f"✅ {len(latest['eq'])} 電極からデータ受信中"
        
    if not latest["pow"]:
        status['pow'] = "データなし - 脳波データの購読を確認してください"
    else:
        status['pow'] = f"✅ {len(latest['pow'])} 周波数帯域からデータ受信中"
        
    if not latest["met"]:
        status['met'] = "データなし - Performance Metricsが無効の可能性"
    else:
        status['met'] = f"✅ {len(latest['met'])} 指標からデータ受信中"
        
    if not latest["mot"]:
        status['mot'] = "データなし - モーションセンサーが無効の可能性"
    else:
        status['mot'] = f"✅ {len(latest['mot'])} データポイント受信中"
        
    if not latest["dev"]:
        status['dev'] = "データなし - デバイス情報が取得できません"
    else:
        status['dev'] = f"✅ デバイス情報受信中"
    
    return status

# ==== Dash UI ====
app = Dash(__name__)
app.title = "Cortex Realtime Dashboard (Free Tier)"

def make_eq_bar():
    eq = latest["eq"]
    if not eq:
        # データがない場合は診断情報を表示
        status = get_connection_status()
        fig = go.Figure()
        fig.add_annotation(
            text=f"EQ データなし<br><br>{status['eq']}",
            xref="paper", yref="paper",
            x=0.5, y=0.5, xanchor='center', yanchor='middle',
            showarrow=False, font=dict(size=14, color="red")
        )
        fig.update_layout(
            title="EQ (electrode contact quality: 0–4)", 
            height=300,
            xaxis=dict(visible=False),
            yaxis=dict(visible=False, range=[0, 4])
        )
        return fig
    chs = list(eq.keys())
    vals = [eq[k] for k in chs]
    fig = go.Figure(data=[go.Bar(x=chs, y=vals)])
    fig.update_layout(
        title="EQ (electrode contact quality: 0–4)", 
        yaxis=dict(range=[0, 4]),
        height=300
    )
    return fig

def make_pow_bar():
    pd = latest["pow"]
    if not pd:
        # データがない場合は診断情報を表示
        status = get_connection_status()
        fig = go.Figure()
        fig.add_annotation(
            text=f"POWデータなし<br><br>{status['pow']}",
            xref="paper", yref="paper",
            x=0.5, y=0.5, xanchor='center', yanchor='middle',
            showarrow=False, font=dict(size=14, color="red")
        )
        fig.update_layout(
            title="Frequency Bands (avg per band from pow)", 
            height=300,
            xaxis=dict(visible=False),
            yaxis=dict(visible=False, title="Relative Power (a.u.)")
        )
        return fig
    xs = list(pd.keys())
    ys = [pd[k] for k in xs]
    fig = go.Figure(data=[go.Bar(x=xs, y=ys)])
    fig.update_layout(
        title="Frequency Bands (avg per band from pow)", 
        yaxis_title="Relative Power (a.u.)",
        height=300
    )
    return fig

def make_met_gauges():
    # met は辞書（engagement, relaxation 等）想定。キーが動的でも対応。
    comps = []
    met_data = latest["met"]
    if not met_data:
        status = get_connection_status()
        comps.append(html.Div([
            html.Div("❌ METデータなし", style={"fontWeight":"bold","color":"red","marginBottom":"10px"}),
            html.Div(status['met'], style={"fontSize":"12px","color":"#666"}),
            html.Div(status['general'], style={"fontSize":"10px","color":"#999","marginTop":"5px"})
        ], style={"padding":"16px","border":"1px solid #ffcccb","borderRadius":"8px","backgroundColor":"#fff5f5"}))
        return comps
        
    for k, v in sorted(met_data.items()):
        # 値を0-1の範囲に正規化（Cortexの値は通常0-1）
        normalized_v = max(0, min(1, v))
        color = f"hsl({120 * normalized_v}, 70%, 50%)"  # 赤から緑へのグラデーション
        
        comps.append(html.Div([
            html.Div(k, style={"fontWeight":"600","fontSize":"12px"}),
            html.Div(f"{v:.3f}", style={"fontSize":"14px","color":color,"fontWeight":"bold"}),
            html.Div(style={
                "height":"4px",
                "backgroundColor":"#eee",
                "borderRadius":"2px",
                "marginTop":"4px",
                "position":"relative"
            }, children=[
                html.Div(style={
                    "height":"100%",
                    "width":f"{normalized_v*100}%",
                    "backgroundColor":color,
                    "borderRadius":"2px"
                })
            ])
        ], className="card", style={
            "padding":"12px",
            "border":"1px solid #ddd",
            "borderRadius":"8px",
            "width":"160px",
            "textAlign":"center",
            "backgroundColor":"white",
            "boxShadow":"0 2px 4px rgba(0,0,0,0.1)"
        }))
    
    return comps

def make_mot_lines():
    mot = list(latest["mot"])
    fig = go.Figure()
    if not mot:
        # データがない場合は診断情報を表示
        status = get_connection_status()
        fig.add_annotation(
            text=f"MOTIONデータなし<br><br>{status['mot']}",
            xref="paper", yref="paper",
            x=0.5, y=0.5, xanchor='center', yanchor='middle',
            showarrow=False, font=dict(size=14, color="red")
        )
        fig.update_layout(
            title="Motion (ACC/GYRO)", 
            height=300,
            xaxis=dict(visible=False, title="Time"),
            yaxis=dict(visible=False, title="Value")
        )
        return fig
    
    ts = [m["ts"] for m in mot]
    for k in mot_fields:
        if k in mot[0]:
            fig.add_trace(go.Scatter(x=ts, y=[m[k] for m in mot], mode="lines", name=k))
    
    fig.update_layout(
        title="Motion (ACC/GYRO)", 
        xaxis_title="Time", 
        yaxis_title="Value",
        height=300,
        showlegend=True
    )
    return fig

def kv_or_empty(name):
    d = latest.get(name, {})
    if not d:
        status = get_connection_status()
        return f"❌ {name.upper()} データなし: {status.get(name, '原因不明')}"
    try:
        return json.dumps(d, ensure_ascii=False)
    except:
        return str(d)

def get_detailed_status():
    """詳細な接続状況を返す"""
    status = get_connection_status()
    if not client.token:
        return "❌ Cortex認証失敗 - CLIENT_ID/CLIENT_SECREtを確認してください"
    elif not client.session_id:
        return "❌ セッション未作成 - Emotivアプリでヘッドセットを接続してください"
    else:
        device_info = latest.get("dev", {})
        if device_info:
            return f"✅ 接続済み - {status['general']}"
        else:
            return "⚠️ 接続済みだがデバイス情報なし"

app.layout = html.Div([
    html.H2("EMOTIV Cortex Realtime Dashboard (Registered Developer)"),
    html.Div("Streams: pow, met(low-res), mot, eq, dev (optional: fac, com)"),
    html.Div(id="dev-info", style={"marginBottom":"10px","padding":"8px","backgroundColor":"#f0f8ff","borderRadius":"4px","fontSize":"14px"}),
    
    # 接続診断パネル
    html.Details([
        html.Summary("🔍 接続診断情報", style={"cursor":"pointer","fontWeight":"bold","marginBottom":"10px"}),
        html.Div(id="diagnostic-info", style={"padding":"10px","backgroundColor":"#f9f9f9","borderRadius":"4px","fontSize":"12px"})
    ], style={"marginBottom":"20px"}),
    
    html.Div([
        html.Div([dcc.Graph(id="eq-graph", style={"height":"300px"})], style={"flex":"1","minWidth":"300px"}),
        html.Div([dcc.Graph(id="pow-graph", style={"height":"300px"})], style={"flex":"1","minWidth":"300px"}),
    ], style={"display":"flex","gap":"12px"}),

    html.Div([
        html.Div([
            html.H4("MET (low-res)"),
            html.Div(id="met-cards", style={"display":"flex","gap":"8px","flexWrap":"wrap"})
        ], style={"flex":"1","minWidth":"300px"}),

        html.Div([
            html.H4("MOTION"),
            dcc.Graph(id="mot-graph", style={"height":"300px"})
        ], style={"flex":"2","minWidth":"400px"})
    ], style={"display":"flex","gap":"12px","marginTop":"20px"}),

    html.Div([
        html.Div([
            html.H4("FAC (facial expression)"),
            html.Pre(id="fac-json", style={"background":"#f9f9f9","padding":"8px","borderRadius":"8px"})
        ], style={"flex":"1"}),
        html.Div([
            html.H4("COM (mental commands)"),
            html.Pre(id="com-json", style={"background":"#f9f9f9","padding":"8px","borderRadius":"8px"})
        ], style={"flex":"1"})
    ], style={"display":"flex","gap":"12px"}),

    dcc.Interval(id="tick", interval=500, n_intervals=0)
], style={"padding":"16px","fontFamily":"system-ui, -apple-system, Segoe UI, Roboto, Noto Sans JP"})

@app.callback(
    Output("eq-graph","figure"),
    Output("pow-graph","figure"),
    Output("mot-graph","figure"),
    Output("met-cards","children"),
    Output("fac-json","children"),
    Output("com-json","children"),
    Output("dev-info","children"),
    Output("diagnostic-info","children"),
    Input("tick","n_intervals")
)
def _update(_):
    eq_fig = make_eq_bar()
    pow_fig = make_pow_bar()
    mot_fig = make_mot_lines()
    met_cards = make_met_gauges()
    fac_text = kv_or_empty("fac")
    com_text = kv_or_empty("com")
    dev_text = get_detailed_status()
    
    # 診断情報
    status = get_connection_status()
    diagnostic_text = html.Div([
        html.Div(f"認証状態: {'✅ 完了' if client.token else '❌ 未完了'}", style={"marginBottom":"5px"}),
        html.Div(f"セッション: {'✅ 作成済み' if client.session_id else '❌ 未作成'}", style={"marginBottom":"5px"}),
        html.Div(f"EQ: {status['eq']}", style={"marginBottom":"5px"}),
        html.Div(f"POW: {status['pow']}", style={"marginBottom":"5px"}),
        html.Div(f"MET: {status['met']}", style={"marginBottom":"5px"}),
        html.Div(f"MOT: {status['mot']}", style={"marginBottom":"5px"}),
        html.Div(f"DEV: {status['dev']}")
    ])
    
    return eq_fig, pow_fig, mot_fig, met_cards, fac_text, com_text, dev_text, diagnostic_text

if __name__ == "__main__":
    app.run(debug=False, port=8051)

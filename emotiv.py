# -*- coding: utf-8 -*-
"""
Emotiv EPOC X (無料Developerプラン) で pow/met/mot/dev/eq を購読し、
リアルタイムに睡眠状態（Wake / NREM-Light / NREM-Deep / REM-like）を推定して可視化するデモ。

学術的背景（要旨）:
- AASMマニュアルでは30秒エポックでのスコアリングが標準。NREMは低周波(δ/θ)優位、REMは覚醒に似た高周波成分＋低運動。
- 本デモではRAWがないため紡錘/K複合/徐波%は扱えない。代替として帯域比(θ, α, βL+βH, γ)と運動量(mot)を使い、
  30秒エポック単位のラベルをルール + 遷移制約で整合する（最小持続時間・あり得ない遷移の抑制）。

可視化:
- 上段: Hypnogram (時間×ステージ)
- 中段: 帯域比(alpha, theta, beta)の推移
- 下段: 3軸加速度RMS（大きいほど動いている=覚醒寄り）
"""

import json
import ssl
import time
import math
import threading
from collections import deque, defaultdict
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import numpy as np
import matplotlib.pyplot as plt
from websocket import create_connection

# Web server for real-time visualization
from flask import Flask, render_template
from flask_socketio import SocketIO, emit

# ====== あなたのアプリ情報 ======
APP_ID = "com.souta.hirakka.realtime-app"
CLIENT_ID = "elEQNmVZbVOzSyV6PskFbdUtlI6wKZD2ZZ4vOJC6"
CLIENT_SECRET = "AeMMNghneyGBXUs69MsXrKhq4nRIyXeCrc7k84z8X9a5ubt2HGMrk4i16vXd8Nnqu9N95WtdQinUirki3umAucLvpJ3BzmuQ2wWbdMf7uhj8AIv39fgAK9GHSG59yh56"  # ← Developer Console で取得して入れてください

# ====== Cortex 接続設定 ======
CORTEX_URL = "wss://localhost:6868"

# ====== 睡眠推定設定 ======
EPOCH_SEC = 30                 # 30秒エポック（AASM)
POW_HZ = 0.5                   # pow更新 ~0.5Hz → 30秒で ~15サンプル
MOT_SRATE = 128                # 加速度は128Hz（目安）
MIN_REM_EPOCHS = 2             # REM-like 最短持続（= 1分）
MIN_DEEP_EPOCHS = 2            # Deep 最短持続
MIN_LIGHT_EPOCHS = 2           # Light 最短持続
MIN_WAKE_EPOCHS = 1

# ====== ステージ定義 ======
STAGES = ["Wake", "NREM_Light", "NREM_Deep", "REM_like"]
STAGE2IDX = {s:i for i,s in enumerate(STAGES)}

# ====== Web可視化サーバー ======
app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*")

class WebVisualizer:
    def __init__(self):
        self.connected_clients = 0
        
    def send_data(self, feat, stage, timestamp):
        """リアルタイムデータをWebクライアントに送信"""
        if self.connected_clients > 0:
            data = {
                'timestamp': timestamp,
                'stage': stage,
                'alpha': feat[1],
                'theta': feat[2], 
                'beta': feat[3],
                'moveRMS': feat[4],
                'time_str': time.strftime("%H:%M:%S", time.localtime(timestamp))
            }
            socketio.emit('sleep_data', data)

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    global web_viz
    web_viz.connected_clients += 1
    print(f"[WEB] クライアント接続: {web_viz.connected_clients}台")

@socketio.on('disconnect') 
def handle_disconnect():
    global web_viz
    web_viz.connected_clients -= 1
    print(f"[WEB] クライアント切断: {web_viz.connected_clients}台")

# グローバルインスタンス
web_viz = WebVisualizer()

# ====== ヘルパ ======
# 深さ指標: theta_ratio - 0.5*beta_ratio（Deepほど大きい）
DEPTH_BETA_WEIGHT = 0.5
# REMっぽさ: alpha+beta が高い、かつ運動が小さい
REM_SUM_AB_THRESHOLD = 0.55   # 個体正規化後の目安
# 覚醒: 運動 + 高周波（alpha+beta）高め
WAKE_MOVE_Z = 1.0

# ====== ヘルパ ======
def robust_z(x, baseline):
    """baseline=(median, mad) からロバストzに変換"""
    med, mad = baseline
    if mad <= 1e-9:
        return 0.0
    return (x - med) / (1.4826 * mad)

def running_robust_stats(values, maxlen=600):
    """dequeに対するmedian/mad"""
    if len(values) == 0:
        return (0.0, 1.0)
    arr = np.asarray(values, dtype=float)
    med = np.median(arr)
    mad = np.median(np.abs(arr - med)) + 1e-9
    return (float(med), float(mad))

# ====== Cortex RPC クライアント ======
class CortexClient:
    def __init__(self, url=CORTEX_URL):
        self.ws = create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE})
        self._id = 1
        self.push_queue = deque(maxlen=2048)

    def rpc(self, method, params=None):
        req = {"id": self._id, "jsonrpc": "2.0", "method": method, "params": params or {}}
        self._id += 1
        self.ws.send(json.dumps(req))
        while True:
            res = json.loads(self.ws.recv())
            if res.get("id") == req["id"]:
                if "error" in res:
                    raise RuntimeError(f"RPC Error: {res['error']}")
                return res["result"]
            else:
                # pushメッセージはキューへ
                self.push_queue.append(res)

    def get_push(self, block=True, timeout=1.0):
        start = time.time()
        while True:
            if self.push_queue:
                return self.push_queue.popleft()
            try:
                self.ws.settimeout(0.1)  # 短いタイムアウトで非ブロッキング的に
                msg = json.loads(self.ws.recv())
                if "id" not in msg:
                    return msg  # push
            except Exception:
                pass
            if not block or (time.time()-start) > timeout:
                return None

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

# ====== データ購読・蓄積 ======
@dataclass
class PowLayout:
    cols: List[str]                 # 例: ["AF3/theta", "AF3/alpha", ...]
    idxs: Dict[str, List[int]]      # バンド名→インデックス配列

class EmotivStreamer:
    def __init__(self, client: CortexClient):
        self.c = client
        self.token = None
        self.session = None
        self.pow_layout: Optional[PowLayout] = None

        # rolling buffers
        self.pow_buf = deque(maxlen=int(POW_HZ*EPOCH_SEC*10))  # 約5分ぶん
        self.acc_buf = deque(maxlen=MOT_SRATE*EPOCH_SEC*10)    # 約5分ぶん
        self.eq_state = 1.0
        self.dev_info = {}

    def connect_and_subscribe(self):
        # ログイン確認
        r = self.c.rpc("getUserLogin")
        print(f"Login check response: {r}")
        # レスポンスがリスト形式の場合に対応
        if isinstance(r, list) and len(r) > 0:
            user_info = r[0]
            if "username" not in user_info:
                raise RuntimeError("Emotiv Launcherにログインしてください。")
            print(f"ログイン済み: {user_info['username']}")
        elif not r.get("loggedIn", False):
            raise RuntimeError("Emotiv Launcherにログインしてください。")

        # アクセス要求 → 許可
        try:
            self.c.rpc("requestAccess", {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
            r = self.c.rpc("hasAccessRight", {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
            print(f"Access right response: {r}")
            if not r.get("accessGranted", False):
                raise RuntimeError("Launcherでアプリ許可が未承認です。")
        except RuntimeError as e:
            if "Client Credentials" in str(e):
                raise RuntimeError("CLIENT_IDまたはCLIENT_SECRETが無効です。Developer Consoleで正しい値を確認してください。")
            raise

        # 認可
        self.token = self.c.rpc("authorize", {
            "clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET, "debit": 0
        })["cortexToken"]

        # ヘッドセット選択
        hs = self.c.rpc("queryHeadsets")
        if not hs:
            raise RuntimeError("ヘッドセットが見つかりません。")
        hid = hs[0]["id"]

        # セッション開始
        self.session = self.c.rpc("createSession", {
            "cortexToken": self.token, "headset": hid, "status": "open"
        })["id"]

        # 購読
        need = ["pow", "mot", "dev", "eq", "met"]  # RAW eeg は無料プランでは不可
        res = self.c.rpc("subscribe", {
            "cortexToken": self.token, "session": self.session, "streams": need
        })
        # powの列配置を覚える
        for s in res["success"]:
            if s["streamName"] == "pow":
                cols = s["cols"]
                idxs = defaultdict(list)
                for i, name in enumerate(cols):
                    # name例: "AF3/theta"
                    try:
                        _, band = name.split("/")
                    except ValueError:
                        band = "unknown"
                    idxs[band].append(i)
                self.pow_layout = PowLayout(cols=cols, idxs=dict(idxs))
        return res

    def loop_once(self):
        msg = self.c.get_push(block=True, timeout=1.0)
        if not msg:
            return None

        t = msg.get("time", time.time())

        if "pow" in msg:
            # powベクトル（各電極×5バンド）
            v = np.asarray(msg["pow"], dtype=float)
            self.pow_buf.append((t, v))

        if "mot" in msg:
            mv = np.asarray(msg["mot"], dtype=float)
            # mot 配列の並びは Cortex 仕様に依存。まずは加速度3軸だけ抽出（例: ax, ay, az を末尾3つと想定）
            # 安全のため、長さや順序は実環境に合わせて調整してください。
            ax, ay, az = mv[-3:]
            self.acc_buf.append((t, float(math.sqrt(ax*ax + ay*ay + az*az))))

        if "eq" in msg:
            # 0..1 程度の品質指標もしくは辞書（機種により表現が違う）
            # ここでは簡易的に平均をとる
            try:
                vals = np.asarray(msg["eq"], dtype=float)
                self.eq_state = float(np.nanmean(vals))
            except Exception:
                self.eq_state = 1.0

        if "dev" in msg:
            self.dev_info = {"dev": msg["dev"]}

        if "met" in msg:
            # 今回は使用しないが購読しておく
            pass

        return msg

# ====== 睡眠推定器（ルール + 遷移制約） ======
class SleepStager:
    def __init__(self):
        self.epoch_features = []     # (t_center, alpha_ratio, theta_ratio, beta_ratio, move_rms)
        self.stage_seq = []          # 推定ステージ
        # ベースライン用の窓（20分くらいで安定させる）
        self.alpha_hist = deque(maxlen=40)  # 40*30s=20分
        self.theta_hist = deque(maxlen=40)
        self.beta_hist  = deque(maxlen=40)
        self.move_hist  = deque(maxlen=40)

    def _compute_epoch_features(self, pow_samples: List[np.ndarray], acc_samples: List[float], pow_layout: PowLayout):
        # pow_samples: [(t, vec), ...]  30秒分
        # acc_samples: [(t, acc_r), ...] 30秒分
        if len(pow_samples) == 0 or len(acc_samples) == 0:
            return None

        # 帯域ごとに全電極平均のメディアンを使う（ロバスト）
        pv = np.stack([v for _, v in pow_samples], axis=0)  # [N, ncols]
        bands = {}
        total = 0.0
        for band in ["theta","alpha","betaL","betaH","gamma"]:
            idxs = pow_layout.idxs.get(band, [])
            if idxs:
                bands[band] = float(np.median(pv[:, idxs]))
                total += bands[band]
            else:
                bands[band] = 0.0
        # 正規化（相対パワー）
        if total <= 0:
            total = 1.0
        theta_r = bands["theta"] / total
        alpha_r = bands["alpha"] / total
        beta_r  = (bands["betaL"] + bands["betaH"]) / total

        # 運動: RMS の中央値
        acc = np.asarray([a for _, a in acc_samples], dtype=float)
        move_rms = float(np.sqrt(np.mean(acc**2)))
        t_center = float(np.mean([t for t,_ in pow_samples]))
        return (t_center, alpha_r, theta_r, beta_r, move_rms)

    def _stage_rule(self, feat, baselines):
        """
        ルール判定（1エポック）。戻り値は "Wake"/"NREM_Light"/"NREM_Deep"/"REM_like" のいずれか。
        baselines: dict of robust baselines for ratios & movement.
        """
        _, alpha_r, theta_r, beta_r, move_rms = feat

        # ロバストzで個体差を補正
        az = robust_z(alpha_r, baselines["alpha"])
        tz = robust_z(theta_r, baselines["theta"])
        bz = robust_z(beta_r,  baselines["beta"])
        mz = robust_z(move_rms, baselines["move"])

        # 深さ指標（θ↑ & β↓）
        depth = tz - DEPTH_BETA_WEIGHT * max(bz, 0.0)

        # 覚醒（運動が大きい or 高周波が高い）
        if (mz > WAKE_MOVE_Z) or (az + max(bz,0.0) > 1.5):
            return "Wake"

        # REM-like（運動が非常に小さく、かつα+βが高め）
        if (mz < -0.5) and ((alpha_r + beta_r) > REM_SUM_AB_THRESHOLD):
            return "REM_like"

        # 深め（θ優位）
        if depth > 0.8:
            return "NREM_Deep"

        # それ以外は浅め
        return "NREM_Light"

    def update_epoch(self, pow_samples: List[Tuple[float,np.ndarray]], acc_samples: List[Tuple[float,float]], pow_layout: PowLayout):
        feat = self._compute_epoch_features(pow_samples, acc_samples, pow_layout)
        if feat is None:
            return None

        # ベースライン更新
        _, a, t, b, m = feat
        self.alpha_hist.append(a)
        self.theta_hist.append(t)
        self.beta_hist.append(b)
        self.move_hist.append(m)

        baselines = {
            "alpha": running_robust_stats(self.alpha_hist),
            "theta": running_robust_stats(self.theta_hist),
            "beta" : running_robust_stats(self.beta_hist),
            "move" : running_robust_stats(self.move_hist),
        }

        stage = self._stage_rule(feat, baselines)
        self.epoch_features.append(feat)
        self.stage_seq.append(stage)

        # 最小持続時間ルールで軽く平滑化
        self._duration_smooth()

        return feat, stage

    def _duration_smooth(self):
        """直近のエポック列に最小持続時間を適用して、瞬間的な誤判定を抑える。"""
        if len(self.stage_seq) < 3:
            return
        # 末尾から見て、短すぎる孤立エポックを前後に合わせる
        def min_len(stage):
            return {
                "Wake": MIN_WAKE_EPOCHS,
                "NREM_Light": MIN_LIGHT_EPOCHS,
                "NREM_Deep": MIN_DEEP_EPOCHS,
                "REM_like": MIN_REM_EPOCHS,
            }[stage]

        # 末尾のラン長
        tail_stage = self.stage_seq[-1]
        run = 1
        for i in range(len(self.stage_seq)-2, -1, -1):
            if self.stage_seq[i] == tail_stage:
                run += 1
            else:
                break
        if run < min_len(tail_stage) and len(self.stage_seq) >= run+1:
            # 直前のステージに吸着
            self.stage_seq[-run:] = [self.stage_seq[-run-1]]*run

# ====== 可視化 ======
class LivePlot:
    def __init__(self):
        self.fig, (self.ax_hyp, self.ax_band, self.ax_mov) = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
        self.fig.suptitle("Real-time Sleep Staging (Emotiv EPOC X / pow+mot)")
        self.ax_hyp.set_ylabel("Stage")
        self.ax_band.set_ylabel("Band ratios")
        self.ax_mov.set_ylabel("Accel RMS")
        self.ax_mov.set_xlabel("Time (min)")
        self.stage_line = None
        self.t0 = None

    def update(self, stager: SleepStager):
        if not stager.epoch_features:
            return
        t = np.array([f[0] for f in stager.epoch_features])
        if self.t0 is None:
            self.t0 = t[0]
        tx = (t - self.t0) / 60.0  # minutes

        alpha = np.array([f[1] for f in stager.epoch_features])
        theta = np.array([f[2] for f in stager.epoch_features])
        beta  = np.array([f[3] for f in stager.epoch_features])
        move  = np.array([f[4] for f in stager.epoch_features])

        # Hypnogram: カテゴリを数値へ
        y = np.array([STAGE2IDX[s] for s in stager.stage_seq], dtype=float)

        # クリアして再描画（簡単安定）
        self.ax_hyp.cla()
        self.ax_band.cla()
        self.ax_mov.cla()

        self.ax_hyp.step(tx, y, where="post")
        self.ax_hyp.set_yticks(range(len(STAGES)))
        self.ax_hyp.set_yticklabels(STAGES)
        self.ax_hyp.set_ylabel("Stage")

        self.ax_band.plot(tx, alpha, label="alpha")
        self.ax_band.plot(tx, theta, label="theta")
        self.ax_band.plot(tx, beta,  label="beta")
        self.ax_band.legend(loc="upper right")
        self.ax_band.set_ylabel("Band ratios")

        self.ax_mov.plot(tx, move, label="accRMS")
        self.ax_mov.set_ylabel("Accel RMS")
        self.ax_mov.set_xlabel("Time (min)")

        plt.pause(0.001)

# ====== メインループ ======
def run_emotiv_processing():
    """EMOTIV処理を別スレッドで実行"""
    print("[*] Connecting to Cortex...")
    try:
        c = CortexClient()
    except Exception as e:
        print(f"[ERROR] Cortex接続に失敗: {e}")
        print("Emotiv Launcherが起動していることを確認してください。")
        return
    
    try:
        streamer = EmotivStreamer(c)
        subres = streamer.connect_and_subscribe()
        print("[*] Subscribed:", [s["streamName"] for s in subres["success"]])
        if subres.get("failure"):
            print("[WARN] Failed subscriptions:", subres["failure"])
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        c.close()
        return
    except Exception as e:
        print(f"[ERROR] 予期しないエラー: {e}")
        c.close()
        return

    stager = SleepStager()
    # Matplotlibビューは起動時のオプションとして残す
    viz = None
    use_matplotlib = False  # デフォルトはWeb可視化

    # エポック収集用バッファ
    pow_epoch: List[Tuple[float, np.ndarray]] = []
    acc_epoch: List[Tuple[float, float]] = []
    epoch_start = None

    try:
        if use_matplotlib:
            plt.ion()
            viz = LivePlot()
        print("[*] データ収集開始... (Web: http://localhost:5000)")
        while True:
            msg = streamer.loop_once()
            now = time.time()

            # 30秒のエポック窓を切る（pow基準）
            if epoch_start is None:
                epoch_start = now

            # エポックの終端判定
            if (now - epoch_start) >= EPOCH_SEC:
                # 対象区間のデータを抜き出し
                pow_epoch = [(t, v) for (t, v) in streamer.pow_buf if epoch_start <= t < epoch_start + EPOCH_SEC]
                acc_epoch = [(t, v) for (t, v) in streamer.acc_buf if epoch_start <= t < epoch_start + EPOCH_SEC]

                if streamer.pow_layout and pow_epoch and acc_epoch:
                    feat_stage = stager.update_epoch(pow_epoch, acc_epoch, streamer.pow_layout)
                    if feat_stage is not None:
                        feat, stage = feat_stage
                        t_center = feat[0]
                        
                        # コンソール出力
                        t_str = time.strftime("%H:%M:%S", time.localtime(t_center))
                        print(f"[{t_str}] Stage={stage}  alpha={feat[1]:.2f} theta={feat[2]:.2f} beta={feat[3]:.2f} moveRMS={feat[4]:.3f}")
                        
                        # Webクライアントにデータ送信
                        web_viz.send_data(feat, stage, t_center)
                        
                        # Matplotlib表示（オプション）
                        if use_matplotlib and viz:
                            viz.update(stager)
                else:
                    print(f"[WARN] データ不足 - pow:{len(pow_epoch)} acc:{len(acc_epoch)}")

                epoch_start += EPOCH_SEC

            time.sleep(0.02)

    except KeyboardInterrupt:
        print("\n[*] ユーザーによって中断されました。")
    except Exception as e:
        print(f"\n[ERROR] 実行時エラー: {e}")
    finally:
        try:
            if streamer.session and streamer.token:
                c.rpc("updateSession", {"cortexToken": streamer.token, "session": streamer.session, "status": "close"})
                print("[*] セッションを閉じました。")
        except Exception:
            pass
        c.close()
        if use_matplotlib:
            plt.close('all')

def main():
    # EMOTIV処理を別スレッドで開始
    emotiv_thread = threading.Thread(target=run_emotiv_processing, daemon=True)
    emotiv_thread.start()
    
    # Webサーバー開始
    print("[*] Web可視化サーバーを開始...")
    print("[*] ブラウザで http://localhost:8080 にアクセスしてください")
    socketio.run(app, host='0.0.0.0', port=8080, debug=False)

if __name__ == "__main__":
    # CLIENT_SECRETが実際に設定されているかチェック
    if not CLIENT_SECRET or CLIENT_SECRET == "PUT_YOUR_CLIENT_SECRET_HERE" or len(CLIENT_SECRET) < 10:
        print("!!! CLIENT_SECRET をセットしてください（Developer Console で取得）。")
        print("1. https://www.emotiv.com/developer/ にアクセス")
        print("2. アプリを作成し、CLIENT_IDとCLIENT_SECRETを取得")
        print("3. コード内のCLIENT_SECRETを実際の値に置き換えてください")
        exit(1)
    
    # 必要パッケージの確認
    try:
        import numpy as np
        import matplotlib.pyplot as plt
        # websocketパッケージの確認は実行時に行う
    except ImportError as e:
        print(f"!!! 必要なパッケージが不足しています: {e}")
        print("以下のコマンドで必要パッケージをインストールしてください:")
        print("pip install numpy matplotlib websocket-client")
        exit(1)
    
    main()

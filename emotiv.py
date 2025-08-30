# server.py
# -*- coding: utf-8 -*-
import os, json, ssl, time, math
from collections import deque, defaultdict, Counter
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

import numpy as np
from scipy.stats import norm
from websocket import create_connection
from dotenv import load_dotenv

from flask import Flask, send_from_directory
from flask_socketio import SocketIO

load_dotenv()
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
CORTEX_URL = "wss://localhost:6868"

# ==== パラメータ ====
EPOCH_SEC = 30                 # 確定エポック（AASM準拠の単位）
MICRO_EPOCH_SEC = 10           # 速報エポック長
MICRO_HOP_SEC   = 2            # 速報更新間隔
POW_HZ = 0.5                   # powは~0.5Hz更新
MOT_SRATE = 128                # 加速度目安
DEPTH_BETA_WEIGHT = 0.5
REM_SUM_AB_THRESHOLD = 0.55
WAKE_MOVE_Z = 1.0
EQ_MIN = 0.3                   # 接触品質がこれ未満なら判定保留

# HMM: 状態と遷移（Wake, Light, Deep, REM）
STAGES = ["Wake","NREM_Light","NREM_Deep","REM_like"]
S2I = {s:i for i,s in enumerate(STAGES)}

# 遷移確率（対数）：睡眠アーキテクチャ寄り。必要なら微調整。
A = np.log(np.array([
    # to:   W     L      D      R
    [0.70, 0.25,  0.03,  0.02],   # from W
    [0.10, 0.70,  0.15,  0.05],   # from L
    [0.05, 0.20,  0.70,  0.05],   # from D
    [0.20, 0.60,  0.01,  0.19],   # from R (REM->Light/Wakeに戻りやすい, 0除去)
], dtype=float))
PI = np.log(np.array([0.6, 0.3, 0.09, 0.01]) + 1e-6)  # 初期確率（入床直後はW寄り）

app = Flask(__name__, static_folder=".")
socketio = SocketIO(app, cors_allowed_origins="*")

def _to_float_silent(x):
    try:
        return float(x)
    except Exception:
        return None

# ---- UIもここから配れるように（index.htmlを同ディレクトリに置く） ----
@app.route("/")
def root():
    return send_from_directory(".", "index.html")


# ==== Cortexクライアント ====
class CortexClient:
    def __init__(self, url=CORTEX_URL):
        self.ws = create_connection(url, sslopt={"cert_reqs": ssl.CERT_NONE})
        self._id = 1
        self.push = deque(maxlen=4096)

    def rpc(self, method, params=None):
        req = {"id": self._id, "jsonrpc":"2.0", "method": method, "params": params or {}}
        self._id += 1
        self.ws.send(json.dumps(req))
        while True:
            res = json.loads(self.ws.recv())
            if res.get("id") == req["id"]:
                if "error" in res:
                    raise RuntimeError(res["error"])
                return res["result"]
            else:
                self.push.append(res)

    def get_push(self, timeout=0.5):
        start = time.time()
        while True:
            if self.push:
                return self.push.popleft()
            try:
                self.ws.settimeout(0.1)
                msg = json.loads(self.ws.recv())
                if "id" not in msg:
                    return msg
            except Exception:
                pass
            if (time.time()-start) > timeout:
                return None

    def close(self): 
        try: self.ws.close()
        except: pass


@dataclass
class PowLayout:
    cols: List[str]
    idxs: Dict[str, List[int]]


class EmotivStreamer:
    def __init__(self, c: CortexClient):
        self.c = c
        self.token = None
        self.session = None
        self.pow_layout: Optional[PowLayout] = None
        self.pow_buf = deque(maxlen=int(POW_HZ*(EPOCH_SEC*10)))
        self.acc_buf = deque(maxlen=MOT_SRATE*(EPOCH_SEC*10))
        self.eq_state = 1.0
        self.last_data_time = time.time()  # データ最終受信時刻
        self.data_timeout = 30  # 30秒間データなしで再接続
        self.zero_data_count = 0  # pow=0, acc=0 の連続回数
        self.zero_data_threshold = 3  # 3回連続でpow=0, acc=0なら再接続

    def connect_and_subscribe(self):
        # ログイン
        print("[DEBUG] ログイン確認中...")
        r = self.c.rpc("getUserLogin")
        print(f"[DEBUG] Login response: {r}")
        if isinstance(r, list):
            logged = bool(r)
        else:
            logged = r.get("loggedIn", False)
        if not logged:
            raise RuntimeError("Emotiv Launcherにログインしてください。")

        print("[DEBUG] アクセス権確認中...")
        # アクセス権
        self.c.rpc("requestAccess", {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
        r = self.c.rpc("hasAccessRight", {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
        print(f"[DEBUG] Access response: {r}")
        if not r.get("accessGranted", False):
            raise RuntimeError("Launcherでアプリ許可が未承認です。")

        print("[DEBUG] 認可処理中...")
        # 認可
        self.token = self.c.rpc("authorize", {
            "clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET, "debit": 0
        })["cortexToken"]

        print("[DEBUG] ヘッドセット検索中...")
        # デバイス
        hs = self.c.rpc("queryHeadsets")
        print(f"[DEBUG] Headsets: {hs}")
        if not hs: raise RuntimeError("ヘッドセット未検出。")
        hid = hs[0]["id"]

        print("[DEBUG] セッション作成中...")
        # セッション
        self.session = self.c.rpc("createSession", {
            "cortexToken": self.token, "headset": hid, "status":"open"
        })["id"]

        print("[DEBUG] データストリーム購読中...")
        # 購読（fac: Facial Expressionも追加）
        need = ["pow","mot","dev","eq","met","fac"]
        res = self.c.rpc("subscribe", {"cortexToken": self.token, "session": self.session, "streams": need})
        print(f"[DEBUG] Subscribe result: {res}")
        for s in res["success"]:
            if s["streamName"]=="pow":
                idxs = defaultdict(list)
                for i, name in enumerate(s["cols"]):
                    band = name.split("/")[-1]
                    idxs[band].append(i)
                self.pow_layout = PowLayout(cols=s["cols"], idxs=dict(idxs))
        return res

    def loop_once(self):
        msg = self.c.get_push(timeout=0.5)
        if not msg: return
        t = msg.get("time", time.time())
        
        # データ受信時刻を更新
        data_received = False
        pow_data_received = False
        mot_data_received = False
        fac_data_received = False
        
        if "pow" in msg:
            self.pow_buf.append((t, np.asarray(msg["pow"], dtype=float)))
            data_received = True
            pow_data_received = True
        if "mot" in msg:
            mv = np.asarray(msg["mot"], dtype=float)
            ax, ay, az = mv[-3:]   # 実機の並びに合わせて調整可
            rms = float(math.sqrt(ax*ax+ay*ay+az*az))
            self.acc_buf.append((t, rms))
            data_received = True
            mot_data_received = True
        if "eq" in msg:
            try:
                self.eq_state = float(np.nanmean(np.asarray(msg["eq"], dtype=float)))
            except:
                self.eq_state = 1.0
        # Facial Expression
        if "fac" in msg:
            fac_payload = self._normalize_fac(msg["fac"], t)
            if fac_payload is not None:
                socketio.emit("fac_data", fac_payload)
                data_received = True
                fac_data_received = True
                
        # データを受信した場合は最終受信時刻を更新
        if data_received:
            self.last_data_time = time.time()
            # 有効なデータが来たらzero_data_countをリセット
            if pow_data_received or mot_data_received or fac_data_received:
                self.zero_data_count = 0

    def _normalize_fac(self, fac_value, fallback_time):
        """facストリームの形を正規化して UI へ送る辞書に変換。
        可能なキー:
          - time_str: HH:MM:SS
          - eyeAction, eyePower
          - upperAction, upperPower
          - lowerAction, lowerPower
        """
        # 統一出力
        out = {
            "time_str": time.strftime("%H:%M:%S", time.localtime(fallback_time)),
            "eyeAction": None,
            "eyePower": None,
            "upperAction": None,
            "upperPower": None,
            "lowerAction": None,
            "lowerPower": None,
        }

        def set_if_not_none(key, val):
            if val is not None:
                out[key] = val

        # 1) 配列ケース（世代によって並びが異なる）
        if isinstance(fac_value, list):
            vals = fac_value
            # 代表的: [time, eyeAct, eyePow, upperAct, upperPow, lowerAct, lowerPow]
            if len(vals) >= 7:
                try:
                    ts = float(vals[0])
                    out["time_str"] = time.strftime("%H:%M:%S", time.localtime(ts/1000.0 if ts>1e12 else (ts if ts>1e9 else fallback_time)))
                except Exception:
                    pass
                set_if_not_none("eyeAction", vals[1])
                set_if_not_none("eyePower", _to_float_silent(vals[2]))
                set_if_not_none("upperAction", vals[3])
                set_if_not_none("upperPower", _to_float_silent(vals[4]))
                set_if_not_none("lowerAction", vals[5])
                set_if_not_none("lowerPower", _to_float_silent(vals[6]))
            # 旧式: [eyeAct, uAct, uPow, lAct, lPow]
            elif len(vals) >= 5:
                set_if_not_none("eyeAction", vals[0])
                set_if_not_none("upperAction", vals[1])
                set_if_not_none("upperPower", _to_float_silent(vals[2]))
                set_if_not_none("lowerAction", vals[3])
                set_if_not_none("lowerPower", _to_float_silent(vals[4]))
            # 簡略: [time, eyeAct, eyePow] or [eyeAct, eyePow]
            elif len(vals) >= 3:
                try:
                    ts = float(vals[0])
                    out["time_str"] = time.strftime("%H:%M:%S", time.localtime(ts/1000.0 if ts>1e12 else (ts if ts>1e9 else fallback_time)))
                    set_if_not_none("eyeAction", vals[1])
                    set_if_not_none("eyePower", _to_float_silent(vals[2]))
                except Exception:
                    # assume no timestamp
                    set_if_not_none("eyeAction", vals[0])
                    set_if_not_none("eyePower", _to_float_silent(vals[1]))
                    set_if_not_none("upperAction", vals[2] if len(vals)>2 else None)
            elif len(vals) >= 2:
                set_if_not_none("eyeAction", vals[0])
                set_if_not_none("eyePower", _to_float_silent(vals[1]))
            else:
                return None

        # 2) dict ケース
        elif isinstance(fac_value, dict):
            # time
            ts = fac_value.get("time")
            if ts is not None:
                try:
                    tsf = float(ts)
                    out["time_str"] = time.strftime("%H:%M:%S", time.localtime(tsf/1000.0 if tsf>1e12 else (tsf if tsf>1e9 else fallback_time)))
                except Exception:
                    pass
            # オーソドックス
            for k_src, k_dst in [
                ("eyeAction","eyeAction"), ("eyeAct","eyeAction"), ("eye","eyeAction"), ("eye_action","eyeAction"),
                ("eyePower","eyePower"), ("eye_pow","eyePower"),
                ("upperFaceAction","upperAction"), ("uAct","upperAction"), ("upper_action","upperAction"),
                ("upperFacePower","upperPower"), ("uPow","upperPower"),
                ("lowerFaceAction","lowerAction"), ("lAct","lowerAction"), ("lower_action","lowerAction"),
                ("lowerFacePower","lowerPower"), ("lPow","lowerPower"),
            ]:
                if k_src in fac_value:
                    val = fac_value[k_src]
                    if k_dst.endswith("Power"):
                        set_if_not_none(k_dst, _to_float_silent(val))
                    else:
                        set_if_not_none(k_dst, val)
        else:
            return None

        return out

    def is_data_stalled(self):
        """データが停止しているかチェック"""
        return (time.time() - self.last_data_time) > self.data_timeout
        
    def is_zero_data_stalled(self):
        """pow=0, acc=0 状態が連続しているかチェック"""
        return self.zero_data_count >= self.zero_data_threshold
        
    def reconnect(self):
        """再接続処理"""
        print("[WARN] データ停止を検知。再接続を試行中...")
        try:
            # 既存セッションのクリーンアップ
            if self.session:
                try:
                    self.c.rpc("updateSession", {"cortexToken": self.token, "session": self.session, "status": "close"})
                except:
                    pass
            
            # 新しいセッションで再接続
            self.connect_and_subscribe()
            self.last_data_time = time.time()
            self.zero_data_count = 0  # カウンターリセット
            print("[INFO] 再接続が成功しました")
            return True
        except Exception as e:
            print(f"[ERROR] 再接続に失敗: {e}")
            return False


# ==== アルゴリズム（速報＆確定 + HMM） ====
def robust_stats(arr):
    if len(arr)==0: return (0.0, 1.0)
    med = np.median(arr)
    mad = np.median(np.abs(arr-med)) + 1e-9
    return float(med), float(mad)

def robust_z(x, baseline):
    med, mad = baseline
    return 0.0 if mad<=1e-9 else (x-med)/(1.4826*mad)

class Stager:
    def __init__(self):
        self.alpha_hist = deque(maxlen=40)  # 20分
        self.theta_hist = deque(maxlen=40)
        self.beta_hist  = deque(maxlen=40)
        self.move_hist  = deque(maxlen=40)
        self.micro_seq: List[Tuple[float,str,float]] = []  # (t_center, stage, conf)
        self.confirmed_seq: List[Tuple[float,str,float]] = []

    def _epoch_feats(self, pow_samples, acc_samples, layout: PowLayout):
        if not pow_samples or not acc_samples: return None
        pv = np.stack([v for _,v in pow_samples], axis=0)
        bands, total = {}, 0.0
        for b in ["theta","alpha","betaL","betaH","gamma"]:
            idxs = layout.idxs.get(b, [])
            val = float(np.median(pv[:, idxs])) if idxs else 0.0
            bands[b] = val; total += val
        total = total if total>0 else 1.0
        theta_r = bands["theta"]/total
        alpha_r = bands["alpha"]/total
        beta_r  = (bands["betaL"]+bands["betaH"])/total

        acc = np.asarray([a for _,a in acc_samples], dtype=float)
        move = float(np.sqrt(np.mean(acc**2)))
        t_center = float(np.mean([t for t,_ in pow_samples]))
        return t_center, alpha_r, theta_r, beta_r, move

    def rule_stage(self, feat, baselines):
        _, a, t, b, m = feat
        az = robust_z(a, baselines["alpha"])
        tz = robust_z(t, baselines["theta"])
        bz = robust_z(b, baselines["beta"])
        mz = robust_z(m, baselines["move"])
        depth = tz - DEPTH_BETA_WEIGHT*max(bz,0.0)

        # rule scores（疑似対数尤度っぽいスコアを返す）
        scores = {s: -5.0 for s in STAGES}
        scores["Wake"] = max(mz - WAKE_MOVE_Z, 0.0) + 0.6*(az + max(bz,0.0))
        scores["REM_like"] = max(-0.5 - mz, 0.0) + max((a+b) - REM_SUM_AB_THRESHOLD, 0.0)
        scores["NREM_Deep"] = max(depth - 0.8, 0.0)
        # NREM_Light はその他
        scores["NREM_Light"] = max(0.4 - abs(depth-0.4), 0.0)

        # 1つに決める（最大スコア）
        stage = max(scores, key=scores.get)
        # 簡易信頼度：maxスコアを0–1クリップ
        conf = max(0.0, min(1.0, scores[stage]))
        return stage, conf

    def micro_update(self, pow_samples, acc_samples, layout: PowLayout):
        feat = self._epoch_feats(pow_samples, acc_samples, layout)
        if feat is None: return None
        _, a, t, b, m = feat
        # ベースライン更新
        self.alpha_hist.append(a); self.theta_hist.append(t)
        self.beta_hist.append(b);  self.move_hist.append(m)
        baselines = {
            "alpha": robust_stats(self.alpha_hist),
            "theta": robust_stats(self.theta_hist),
            "beta" : robust_stats(self.beta_hist),
            "move" : robust_stats(self.move_hist),
        }
        stage, conf = self.rule_stage(feat, baselines)
        self.micro_seq.append((feat[0], stage, conf))
        return feat[0], stage, conf, (a,t,b,m)

    # ---- HMMで30秒を確定化 ----
    def confirm_last_30s(self):
        # 直近30秒のmicroステップを抽出
        if not self.micro_seq: return None
        t_now = self.micro_seq[-1][0]
        window = [(t,s,c) for (t,s,c) in self.micro_seq if t_now-30<=t<=t_now]
        if len(window)<2: return None
        obs = [S2I[s] for (_,s,_) in window]
        confs = np.array([c for (_,_,c) in window], dtype=float)
        T = len(obs)

        # 出力確率（対数）：自分の推定と同じ状態なら高い
        # confを使って sharpness を出す
        B = np.full((T, len(STAGES)), np.log(1e-6))
        for t in range(T):
            p_corr = 0.55 + 0.4*max(0.0, min(1.0, confs[t]))  # 0.55〜0.95
            p_rest = (1.0 - p_corr) / (len(STAGES)-1)
            for s in range(len(STAGES)):
                B[t, s] = np.log(p_corr if s==obs[t] else p_rest)

        # Viterbi
        V = np.full((T, len(STAGES)), -1e9)
        Ptr = np.zeros((T, len(STAGES)), dtype=int)
        V[0,:] = PI + B[0,:]
        for t in range(1,T):
            for j in range(len(STAGES)):
                prev = V[t-1,:] + A[:,j]
                Ptr[t,j] = int(np.argmax(prev))
                V[t,j] = np.max(prev) + B[t,j]
        path = np.zeros(T, dtype=int)
        path[-1] = int(np.argmax(V[-1,:]))
        for t in range(T-2, -1, -1):
            path[t] = Ptr[t+1, path[t+1]]

        # 最終（直近）の確定状態
        stage = STAGES[int(path[-1])]
        conf = float(np.exp(np.max(V[-1,:]) - (np.logaddexp.reduce(V[-1,:]))))  # softmax最大
        self.confirmed_seq.append((t_now, stage, conf))
        return t_now, stage, conf
        

# ==== メインループ：配信 ====
def main_loop():
    print("[MAIN] Starting main loop...")
    if not CLIENT_ID or not CLIENT_SECRET:
        print(f"[ERROR] CLIENT_ID: {CLIENT_ID}, CLIENT_SECRET: {bool(CLIENT_SECRET)}")
        raise RuntimeError("CLIENT_ID/CLIENT_SECRET を .env に設定してください。")
    
    print("[MAIN] Creating Cortex client...")
    try:
        c = CortexClient()
        print("[MAIN] Cortex client created successfully")
    except Exception as e:
        print(f"[ERROR] Cortex client creation failed: {e}")
        return
    
    print("[MAIN] Creating streamer...")
    em = EmotivStreamer(c)
    
    try:
        print("[MAIN] Connecting and subscribing...")
        em.connect_and_subscribe()
        print("[MAIN] Successfully connected and subscribed")
    except Exception as e:
        print(f"[ERROR] Connection failed: {e}")
        return
    
    st = Stager()

    last_micro = time.time()
    last_push_time = 0.0
    
    print("[MAIN] Entering data processing loop...")

    while True:
        try:
            em.loop_once()
            now = time.time()

            # データ停止チェック
            if em.is_data_stalled():
                if em.reconnect():
                    continue
                else:
                    print("[ERROR] 再接続に失敗しました。10秒後に再試行します。")
                    socketio.sleep(10.0)
                    continue

            # 速報（10秒窓、2秒ごと）
            if (now - last_micro) >= MICRO_HOP_SEC:
                micro_pow = [(t,v) for (t,v) in em.pow_buf if now-MICRO_EPOCH_SEC <= t <= now]
                micro_acc = [(t,a) for (t,a) in em.acc_buf if now-MICRO_EPOCH_SEC <= t <= now]

                print(f"[DEBUG] Micro update: pow={len(micro_pow)}, acc={len(micro_acc)}, eq={em.eq_state:.2f}")

                # pow=0, acc=0 状態をカウント
                if len(micro_pow) == 0 and len(micro_acc) == 0 and em.pow_layout:
                    em.zero_data_count += 1
                    print(f"[WARN] Zero data detected (count: {em.zero_data_count}/{em.zero_data_threshold})")
                    
                    # 即座の再接続チェック
                    if em.is_zero_data_stalled():
                        print("[WARN] pow=0, acc=0 状態が連続で検出されました。即座に再接続します。")
                        if em.reconnect():
                            continue
                        else:
                            print("[ERROR] 即座の再接続に失敗しました。")

                # 条件を緩和：powが3以上、accが10以上あればOK
                if em.pow_layout and len(micro_pow)>=3 and len(micro_acc)>=10:
                    if em.eq_state >= EQ_MIN:   # 品質が悪いときは保留
                        res = st.micro_update(micro_pow, micro_acc, em.pow_layout)
                        print(f"[DEBUG] Processing data...")
                    else:
                        res = None
                        print(f"[WARN] EQ too low: {em.eq_state:.2f} < {EQ_MIN}")
                    if res:
                        t_center, stage, conf, (a,t,b,m) = res
                        conf30 = st.confirm_last_30s()  # HMM確定
                        # HMMの確定結果があればそれを出す、なければ速報を出す
                        final_stage, final_conf = (stage, conf)
                        if conf30:
                            _, final_stage, final_conf = conf30

                        print(f"[DATA] {time.strftime('%H:%M:%S', time.localtime(t_center))} Stage={final_stage} Conf={final_conf:.2f}")

                        # UIへプッシュ（あなたのindex.htmlのpayloadに合わせる）
                        socketio.emit("sleep_data", {
                            "time_str": time.strftime("%H:%M:%S", time.localtime(t_center)),
                            "stage": final_stage,
                            "alpha": float(a), "theta": float(t), "beta": float(b),
                            "moveRMS": float(m),
                            "conf": float(final_conf),
                            "eq": float(em.eq_state)
                        })
                        print(f"[DEBUG] Data sent to UI")
                    else:
                        print(f"[DEBUG] No valid feature data generated")
                else:
                    print(f"[DEBUG] Insufficient data: pow={len(micro_pow)}, acc={len(micro_acc)}, layout={bool(em.pow_layout)}")
                last_micro += MICRO_HOP_SEC

            socketio.sleep(0.05)
        except Exception as e:
            print(f"[ERROR] Main loop error: {e}")
            socketio.sleep(1.0)


@socketio.on("connect")
def on_connect():
    print("[socket] client connected")


if __name__ == "__main__":
    socketio.start_background_task(main_loop)
    # index.html を同ディレクトリから配信
    socketio.run(app, host="0.0.0.0", port=8080)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emotiv Cortex Facial Expression API (fac stream) で
Look Left / Look Right / Look Up / Look Down を検出するサンプル。

使い方:
1) Emotiv Launcher を起動し、ヘッドセットを接続・ログイン。
2) Cortex 開発者ダッシュボードで作成した App の CLIENT_ID / CLIENT_SECRET を下に設定。
3) python このスクリプト.py
"""

import json
import ssl
import time
import threading
from datetime import datetime
from websocket import create_connection, WebSocketTimeoutException

# ==== 設定（ご自身のアプリの値に置き換えてください） ====
CLIENT_ID = "YOUR_CLIENT_ID_HERE"
CLIENT_SECRET = "YOUR_CLIENT_SECRET_HERE"
WS_URL = "wss://localhost:6868"

# タイムアウト・リトライ設定
WS_TIMEOUT_SEC = 5
RETRY_WAIT_SEC = 2

# ========= ユーティリティ =========
class CortexRPC:
    def __init__(self, url):
        self.url = url
        self.ws = None
        self.req_id = 0
        self.lock = threading.Lock()
        self.token = None
        self.session_id = None
        self.headset_id = None
        self.running = True

    def connect(self):
        ctx = ssl.create_default_context()
        # ローカルの自己署名証明書を許容
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.ws = create_connection(self.url, sslopt={"cert_reqs": ssl.CERT_NONE}, timeout=WS_TIMEOUT_SEC)

    def close(self):
        self.running = False
        try:
            if self.session_id and self.token:
                self.call("updateSession", {"cortexToken": self.token, "session": self.session_id, "status": "close"})
        except Exception:
            pass
        try:
            if self.ws:
                self.ws.close()
        finally:
            self.ws = None

    def _next_id(self):
        with self.lock:
            self.req_id += 1
            return self.req_id

    def send(self, method, params=None):
        if params is None:
            params = {}
        msg = {"jsonrpc": "2.0", "method": method, "params": params, "id": self._next_id()}
        self.ws.send(json.dumps(msg))
        return msg["id"]

    def recv_until_id(self, target_id, timeout=WS_TIMEOUT_SEC):
        end = time.time() + timeout
        while time.time() < end:
            try:
                raw = self.ws.recv()
            except WebSocketTimeoutException:
                continue
            if not raw:
                continue
            data = json.loads(raw)
            # 要求に対する応答（id一致）
            if isinstance(data, dict) and data.get("id") == target_id:
                return data
            # ストリームイベントは別スレやメインループで処理
        raise TimeoutError(f"RPC response timeout for id={target_id}")

    def call(self, method, params=None, timeout=WS_TIMEOUT_SEC):
        rid = self.send(method, params=params)
        resp = self.recv_until_id(rid, timeout=timeout)
        if "error" in resp:
            raise RuntimeError(f"{method} error: {resp['error']}")
        return resp.get("result", resp)

# ========= 眼球イベントの正規化 =========
EYE_ALIASES = {
    "lookleft": "LOOK_LEFT",
    "left": "LOOK_LEFT",
    "look_right": "LOOK_RIGHT",
    "lookright": "LOOK_RIGHT",
    "right": "LOOK_RIGHT",
    "lookup": "LOOK_UP",
    "up": "LOOK_UP",
    "lookdown": "LOOK_DOWN",
    "down": "LOOK_DOWN",
}

def normalize_eye_action(ev_dict):
    """
    fac ストリームは実装世代によってキー名・値表記が揺れることがあるため、
    代表的フィールドを総当りで拾って正規化する。
    """
    candidates = []
    for key in ("eyeAction", "eyeAct", "eye", "eye_action"):
        v = ev_dict.get(key)
        if v:
            candidates.append(v)
    action = None
    for v in candidates:
        s = str(v).strip().replace(" ", "").lower()
        if s in EYE_ALIASES:
            action = EYE_ALIASES[s]
            break
        # 典型: "look left" / "look_left"
        s2 = s.replace("_", "")
        if s2 in EYE_ALIASES:
            action = EYE_ALIASES[s2]
            break
    # Powerも拾う
    power = None
    for k in ("eyePower", "eye_power", "eyePow", "pow"):
        if k in ev_dict:
            try:
                power = float(ev_dict[k])
                break
            except Exception:
                pass
    # 上顔/下顔情報にも混じる場合があるので拾っておく（予備）
    for k in ("upperFaceAction", "lowerFaceAction"):
        v = ev_dict.get(k)
        if v and not action:
            s = str(v).strip().replace(" ", "").lower()
            if s in EYE_ALIASES:
                action = EYE_ALIASES[s]
    for k in ("upperFacePower", "lowerFacePower"):
        if k in ev_dict and power is None:
            try: power = float(ev_dict[k]); break
            except Exception: pass

    return action, power

# ========= メイン処理 =========
def main():
    rpc = CortexRPC(WS_URL)
    try:
        print("[*] Connecting to Cortex websocket ...")
        rpc.connect()
        print("[+] Connected.")

        # 1) requestAccess（初回のみユーザーの承認が必要）
        print("[*] Requesting access ...")
        rpc.call("requestAccess", {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
        print("[+] Access requested (if first time, ensure you approved it in Emotiv Launcher).")

        # 2) authorize（トークン取得）
        print("[*] Authorizing ...")
        res = rpc.call("authorize", {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET})
        rpc.token = res.get("cortexToken")
        if not rpc.token:
            raise RuntimeError("authorize did not return cortexToken")
        print("[+] Authorized. Token acquired.")

        # 3) ヘッドセット探索
        print("[*] Querying headsets ...")
        hs_list = rpc.call("queryHeadsets", {"id": "*"}).get("headsets", [])
        if not hs_list:
            raise RuntimeError("No headsets found. Connect your headset in Emotiv Launcher.")

        # 優先: connected / available なもの
        target = None
        for hs in hs_list:
            if hs.get("status") in ("connected", "available"):
                target = hs
                break
        if not target:
            target = hs_list[0]

        rpc.headset_id = target.get("id")
        status = target.get("status")
        print(f"[+] Found headset: {rpc.headset_id} (status={status})")

        # 必要なら接続コマンド
        if status != "connected":
            print("[*] Connecting headset ...")
            rpc.call("controlDevice", {"command": "connect", "headset": rpc.headset_id})
            # 接続安定待ち
            time.sleep(2)

        # 4) セッション作成（active）
        print("[*] Creating active session ...")
        sess = rpc.call("createSession", {"cortexToken": rpc.token, "headset": rpc.headset_id, "status": "active"})
        rpc.session_id = sess.get("id") or sess.get("session") or sess.get("result", {}).get("id")
        if not rpc.session_id:
            raise RuntimeError("Failed to create active session")
        print(f"[+] Session active: {rpc.session_id}")

        # 5) fac ストリーム購読
        print("[*] Subscribing to fac stream ...")
        sub = rpc.call("subscribe", {"cortexToken": rpc.token, "session": rpc.session_id, "streams": ["fac", "sys"]})
        print(f"[+] Subscribed: {sub}")

        print("\n=== Listening for eye movements (Look Left/Right/Up/Down) ===")
        print("Press Ctrl+C to stop.\n")

        # 6) イベント受信ループ
        while rpc.running:
            try:
                raw = rpc.ws.recv()
                if not raw:
                    continue
                data = json.loads(raw)

                # ストリームイベントは "fac" キー（配列）で届くケースが多い
                fac_ev = None
                if isinstance(data, dict):
                    if "fac" in data:
                        # 典型: {"fac": [timestamp, eyeAction, eyePower, upperFaceAction, upperFacePower, lowerFaceAction, lowerFacePower]}
                        # ドキュメント世代により shape が異なるので吸収
                        val = data["fac"]
                        # 配列 → フィールド割り当ての試み
                        if isinstance(val, list):
                            fac_ev = {}
                            # よくある並び: [time, eyeAction, eyePower, upperAction, upperPower, lowerAction, lowerPower]
                            if len(val) >= 3:
                                fac_ev["time"] = val[0]
                                fac_ev["eyeAction"] = val[1]
                                fac_ev["eyePower"] = val[2]
                            if len(val) >= 5:
                                fac_ev["upperFaceAction"] = val[3]
                                fac_ev["upperFacePower"] = val[4]
                            if len(val) >= 7:
                                fac_ev["lowerFaceAction"] = val[5]
                                fac_ev["lowerFacePower"] = val[6]
                        elif isinstance(val, dict):
                            fac_ev = val

                if fac_ev:
                    action, power = normalize_eye_action(fac_ev)
                    if action in {"LOOK_LEFT", "LOOK_RIGHT", "LOOK_UP", "LOOK_DOWN"}:
                        ts = fac_ev.get("time", time.time())
                        try:
                            _ts = float(ts)
                            # Cortex は秒/ミリ秒の系が混在し得るため補正
                            if _ts > 1e12:
                                when = datetime.fromtimestamp(_ts/1000.0)
                            elif _ts > 1e9:
                                when = datetime.fromtimestamp(_ts)
                            else:
                                when = datetime.now()
                        except Exception:
                            when = datetime.now()

                        pstr = f"{power:.2f}" if isinstance(power, (float, int)) else "N/A"
                        print(f"[{when.strftime('%H:%M:%S')}] Eye: {action} (power={pstr})")

            except KeyboardInterrupt:
                print("\n[!] Interrupted by user.")
                break
            except Exception as e:
                print(f"[!] recv error: {e}")
                time.sleep(RETRY_WAIT_SEC)

    finally:
        rpc.close()
        print("[*] Closed.")

if __name__ == "__main__":
    main()

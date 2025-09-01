# app_sleep.py
import time, csv, os, sys
from datetime import datetime
from cortex import Cortex
from sleep_engine import SleepEngine
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

# Read credentials from environment
CLIENT_ID     = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
HEADSET_ID    = os.getenv("HEADSET_ID", "")   # 任意
BASE_CSV_NAME = "sleep_candidates"
USER_CSV_FILE = "user_data/users.csv"

def _is_all_zero(vec):
    return all((v == 0 or v is None) for v in vec)

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

class SleepApp:
    def __init__(self, username=None):
        self.c = Cortex(CLIENT_ID, CLIENT_SECRET, debug_mode=False, headset_id=HEADSET_ID)
        self.eng = SleepEngine()
        self._last_data_ts = time.time()
        self._session_start_time = None  # 計測開始時刻
        self._subscribed = False
        self._csv_filename = None  # CSVファイル名
        self._username = username  # ユーザー名

        self.c.bind(create_session_done=self.on_create_session_done)
        self.c.bind(new_data_labels=self.on_new_data_labels)
        self.c.bind(new_pow_data=self.on_new_pow_data)
        self.c.bind(new_mot_data=self.on_new_mot_data)
        self.c.bind(new_dev_data=self.on_new_dev_data)
        self.c.bind(new_fe_data=self.on_new_fe_data)
        self.c.bind(warn_cortex_stop_all_sub=self.on_stop_all_streams)
        self.c.bind(inform_error=self.on_error)

    def _get_relative_time(self, absolute_time):
        """絶対時間を相対時間（秒）に変換"""
        if self._session_start_time is None:
            return 0.0
        return absolute_time - self._session_start_time

    def start(self):
        print("[INFO] Opening Cortex...")
        self.c.open()

    def on_create_session_done(self, *args, **kwargs):
        print("[INFO] Session created. Subscribing streams...")
        # セッション開始時にタイムスタンプ付きCSVファイル名を生成（再接続時も新しいファイルを作成）
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if self._username:
            # ユーザー別ディレクトリを作成
            user_data_dir = os.path.join("user_data", self._username)
            os.makedirs(user_data_dir, exist_ok=True)
            self._csv_filename = os.path.join(user_data_dir, f"{BASE_CSV_NAME}_{timestamp}.csv")
            print(f"[INFO] User-specific CSV: {self._csv_filename}")
            
            # ユーザーのセッション情報を更新
            session_filename = os.path.basename(self._csv_filename)
            if update_user_session(self._username, session_filename):
                print(f"[INFO] Updated session info for user: {self._username}")
        else:
            self._csv_filename = f"{BASE_CSV_NAME}_{timestamp}.csv"
        
        # セッション開始時刻を設定（再接続時もリセット）
        self._session_start_time = time.time()
        print(f"[INFO] Session start time set: {self._session_start_time}")
        print(f"[INFO] CSV filename: {self._csv_filename}")
        
        self.c.sub_request(['pow', 'mot', 'dev', 'fac'])
        self._subscribed = True

    def on_new_data_labels(self, *args, **kwargs):
        data = kwargs.get('data', {})
        if data.get('streamName') == 'pow':
            self.eng.set_pow_labels(data.get('labels', []))
            print("[INFO] pow labels:", data.get('labels', []))

    def on_new_pow_data(self, *args, **kwargs):
        d = kwargs.get('data', {})
        if not d: return
        vec = d.get('pow', [])
        t = d.get('time', time.time())
        relative_t = self._get_relative_time(t)
        if not vec or _is_all_zero(vec): return
        self._last_data_ts = time.time()
        self.eng.on_pow(relative_t, vec)
        self._maybe_step(relative_t)

    def on_new_mot_data(self, *args, **kwargs):
        d = kwargs.get('data', {})
        if not d: return
        self._last_data_ts = time.time()
        t = d.get('time', time.time())
        relative_t = self._get_relative_time(t)
        self.eng.on_mot(relative_t, d.get('mot', []))

    def on_new_dev_data(self, *args, **kwargs):
        d = kwargs.get('data', {})
        if not d: return
        self._last_data_ts = time.time()
        t = d.get('time', time.time())
        relative_t = self._get_relative_time(t)
        self.eng.on_dev(relative_t, float(d.get('signal', 1.0)))

    def on_new_fe_data(self, *args, **kwargs):
        d = kwargs.get('data', {})
        if not d: return
        self._last_data_ts = time.time()
        t = d.get('time', time.time())
        relative_t = self._get_relative_time(t)
        self.eng.on_fac(relative_t, d.get('eyeAct'), float(d.get('uPow', 0.0)), float(d.get('lPow', 0.0)))

    def on_stop_all_streams(self, *args, **kwargs):
        print("[WARN] Cortex stopped all streams. Recovering...")
        try:
            self.c.create_session()
            time.sleep(1.0)
            if self._subscribed:
                self.c.sub_request(['pow','mot','dev','fac'])
        except Exception as e:
            print("[ERR] recover failed:", e)

    def on_error(self, *args, **kwargs):
        print("[ERR]", kwargs.get('error_data', {}))

    def _maybe_step(self, t_now):
        row = self.eng.step(t_now)
        if not row: return
        self._print_row(row)
        self._append_csv(row)

        now = time.time()
        if now - self._last_data_ts > 5:
            print("[WARN] no data >5s, resubscribing...")
            try:
                self.c.sub_request(['pow','mot','dev','fac'])
                time.sleep(2)
                if time.time() - self._last_data_ts > 7:
                    print("[WARN] recreate session...")
                    self.c.create_session()
                    time.sleep(1)
                    self.c.sub_request(['pow','mot','dev','fac'])
            except Exception as e:
                print("[ERR] watchdog:", e)

    def _print_row(self, r):
        # 表示用には絶対時間を使用
        absolute_time = r['t'] + (self._session_start_time or 0)
        user_info = f"[{self._username}] " if self._username else ""
        msg = (f"{user_info}[{time.strftime('%H:%M:%S', time.localtime(absolute_time))}] "
               f"stage={r['stage']}, conf={r['confidence']} | "
               f"th/al={r['theta_alpha']:.2f}, beta_rel={r['beta_rel']:.2f}, "
               f"motRMS={r['motion_rms']:.3f}, facRate={r['fac_rate']:.3f}, "
               f"facActive={'ON' if int(r.get('fac_active',0)) else 'OFF'}, "
               f"signal={r['signal']:.2f}, EOG={'ON' if int(r.get('eog_on',0)) else 'OFF'}")
        print(msg)

    def _append_csv(self, r):
        if not self._csv_filename:
            return
        newfile = not os.path.exists(self._csv_filename)
        with open(self._csv_filename, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if newfile:
                w.writerow(["time","stage","confidence","theta_alpha","beta_rel","motion_rms","fac_rate","fac_active","signal","eog_on","eog_sacc"])
            w.writerow([
                round(r['t'], 1), r['stage'] or "", r['confidence'],
                r['theta_alpha'], r['beta_rel'], r['motion_rms'],
                r['fac_rate'], r.get('fac_active',0.0), r['signal'], r.get('eog_on',0.0), r.get('eog_sacc',0.0)
            ])

if __name__ == "__main__":
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("Please set CLIENT_ID / CLIENT_SECRET via environment variables or .env file")
    
    # コマンドライン引数からユーザー名を取得
    username = None
    if len(sys.argv) > 1:
        username = sys.argv[1]
        print(f"[INFO] Running for user: {username}")
    
    app = SleepApp(username)
    app.start()

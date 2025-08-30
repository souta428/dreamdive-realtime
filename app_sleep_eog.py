# app_sleep_eog.py
import time, csv, os, threading
from cortex import Cortex
from sleep_engine import SleepEngine
from quality import is_all_zero, safe_get
from eog_ingest import UDPJsonEOGSource  # or SerialCSVEOGSource
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

# Read credentials from environment
CLIENT_ID     = os.getenv("CLIENT_ID", "")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "")
HEADSET_ID    = os.getenv("HEADSET_ID", "")   # 任意
OUT_CSV = "sleep_candidates_eog.csv"

class SleepAppEOG:
    def __init__(self):
        self.c = Cortex(CLIENT_ID, CLIENT_SECRET, debug_mode=False, headset_id=HEADSET_ID)
        self.eng = SleepEngine()
        self._last_data_ts = time.time()
        self._subscribed = False

        # Cortex bindings
        self.c.bind(create_session_done=self.on_create_session_done)
        self.c.bind(new_data_labels=self.on_new_data_labels)
        self.c.bind(new_pow_data=self.on_new_pow_data)
        self.c.bind(new_mot_data=self.on_new_mot_data)
        self.c.bind(new_dev_data=self.on_new_dev_data)
        self.c.bind(warn_cortex_stop_all_sub=self.on_stop_all_streams)
        self.c.bind(inform_error=self.on_error)

        # ==== EOG受信（UDP例）====
        self.eog_src = UDPJsonEOGSource(port=9000)
        self.eog_q = self.eog_src.start()
        threading.Thread(target=self._drain_eog, daemon=True).start()

    def _drain_eog(self):
        while True:
            try:
                t, v = self.eog_q.get(timeout=1.0)
                self.eng.on_eog_sample(t, v, src_fs_hint=200.0)
                # 単独EOGでstepを進めたい時はこれでOK
                self._maybe_step(t)
            except Exception:
                pass

    def start(self):
        print("[INFO] Opening Cortex...")
        self.c.open()

    def on_create_session_done(self, *args, **kwargs):
        print("[INFO] Session created. Subscribing streams...")
        self.c.sub_request(['pow', 'mot', 'dev'])  # EOGは外部から
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
        if not vec or is_all_zero(vec): return
        self._last_data_ts = time.time()
        self.eng.on_pow(t, vec)
        self._maybe_step(t)

    def on_new_mot_data(self, *args, **kwargs):
        d = kwargs.get('data', {})
        if not d: return
        self._last_data_ts = time.time()
        self.eng.on_mot(d.get('time', time.time()), d.get('mot', []))

    def on_new_dev_data(self, *args, **kwargs):
        d = kwargs.get('data', {})
        if not d: return
        self._last_data_ts = time.time()
        self.eng.on_dev(d.get('time', time.time()), safe_get(d, 'signal', 1.0))

    def on_stop_all_streams(self, *args, **kwargs):
        print("[WARN] Cortex stopped all streams. Recovering...")
        try:
            self.c.create_session()
            time.sleep(1.0)
            if self._subscribed:
                self.c.sub_request(['pow','mot','dev'])
        except Exception as e:
            print("[ERR] recover failed:", e)

    def on_error(self, *args, **kwargs):
        print("[ERR]", kwargs.get('error_data', {}))

    def _maybe_step(self, t_now):
        row = self.eng.step(t_now)
        if not row: return
        self._print_row(row); self._append_csv(row)

        now = time.time()
        if now - self._last_data_ts > 5:
            print("[WARN] no Emotiv data >5s, resubscribing...")
            try:
                self.c.sub_request(['pow','mot','dev'])
                time.sleep(2)
                if time.time() - self._last_data_ts > 7:
                    print("[WARN] recreate session...")
                    self.c.create_session()
                    time.sleep(1)
                    self.c.sub_request(['pow','mot','dev'])
            except Exception as e:
                print("[ERR] watchdog:", e)

    def _print_row(self, r):
        msg = (f"[{time.strftime('%H:%M:%S', time.localtime(r['t']))}] "
               f"stage={r['stage']}, conf={r['confidence']} | "
               f"th/al={r['theta_alpha']:.2f}, beta_rel={r['beta_rel']:.2f}, "
               f"motRMS={r['motion_rms']:.3f}, EOGsacc/s={r.get('eog_sacc',0):.2f}, "
               f"signal={r['signal']:.2f}, EOG={'ON' if int(r.get('eog_on',0)) else 'OFF'}")
        print(msg)

    def _append_csv(self, r):
        newfile = not os.path.exists(OUT_CSV)
        with open(OUT_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if newfile:
                w.writerow(["time","stage","confidence","theta_alpha","beta_rel",
                            "motion_rms","eog_sacc","signal","eog_on"])
            w.writerow([
                int(r['t']), r['stage'] or "", r['confidence'],
                r['theta_alpha'], r['beta_rel'], r['motion_rms'],
                r.get('eog_sacc',0.0), r['signal'], r.get('eog_on',0.0)
            ])

if __name__ == "__main__":
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("Set CLIENT_ID / CLIENT_SECRET via environment or .env file")
    app = SleepAppEOG()
    app.start()

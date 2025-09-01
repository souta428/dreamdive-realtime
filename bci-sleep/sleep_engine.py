# sleep_engine.py
import time, math
from collections import deque, defaultdict
from typing import List, Dict, Tuple, Optional

EPOCH_SEC = 30      # 30秒で特徴量要約
HOP_SEC = 5         # 5秒ごとに更新
MIN_QUALITY = 0.3   # dev.signal の最低ライン

STAGES = ["Wake", "Light_NREM_candidate", "REM_candidate", "Deep_candidate"]

class Ring:
    def __init__(self, seconds: int):
        self.seconds = seconds
        self.buf = deque()  # (t, value)

    def push(self, t: float, v):
        self.buf.append((t, v))
        self._trim(t)

    def _trim(self, now: float):
        while self.buf and now - self.buf[0][0] > self.seconds:
            self.buf.popleft()

    def values(self):
        return [v for _, v in self.buf]

    def empty(self):
        return len(self.buf) == 0

class SleepEngine:
    """
    無料ストリーム（pow/mot/dev/fac）＋外部EOG（任意）で
    Wake / Light / REM_candidate / Deep_candidate を推定。
    EOGは自動フォールバック（無ければFAC/β/体動のみで推定を継続）。
    """
    def __init__(self):
        self.pow_labels: List[str] = []
        self.theta_idx: List[int] = []
        self.alpha_idx: List[int] = []
        self.beta_idx:  List[int] = []  # betaL + betaH

        self.pow_ring = Ring(EPOCH_SEC)
        self.mot_ring = Ring(EPOCH_SEC)
        self.fac_ring = Ring(EPOCH_SEC)
        self.eog_ring = Ring(EPOCH_SEC)

        self.dev_signal = 1.0
        self.last_epoch_time = 0.0
        self.last_stage = None
        self.hold_until = 0.0
        self.hold_min_sec = 20.0

        self._eog_decim = 0
        self._eog_fs_target = 50.0
        self.eog_last_ts = 0.0
        self.eog_available_window_sec = 10.0
        self.prev_eog_available = None

        self.rows = []

    # ------- ラベル処理 -------
    def set_pow_labels(self, labels: List[str]):
        self.pow_labels = labels
        self.theta_idx, self.alpha_idx, self.beta_idx = [], [], []
        for i, lab in enumerate(labels):
            if lab.endswith("/theta"):
                self.theta_idx.append(i)
            elif lab.endswith("/alpha"):
                self.alpha_idx.append(i)
            elif lab.endswith("/betaL") or lab.endswith("/betaH"):
                self.beta_idx.append(i)

    # ------- ストリーム入力 -------
    def on_pow(self, t: float, vec: List[float], consider_missing_zero: bool = True):
        if consider_missing_zero and all((v == 0 or v is None) for v in vec):
            return
        self.pow_ring.push(t, vec)

    def on_mot(self, t: float, mot_vec: List[float]):
        if len(mot_vec) >= 12:
            accx, accy, accz = mot_vec[9], mot_vec[10], mot_vec[11]
        else:
            accx = accy = accz = 0.0
        rms = (accx*accx + accy*accy + accz*accz) ** 0.5
        self.mot_ring.push(t, rms)

    def on_dev(self, t: float, signal: float):
        self.dev_signal = float(signal)

    def on_fac(self, t: float, eyeAct: Optional[str], uPow: float, lPow: float):
        """
        Facial expression (fac) stream handler
        Detects eye movements and facial actions for sleep stage analysis
        """
        # Validate input parameters
        if eyeAct is None:
            eyeAct = ""
        uPow = float(uPow or 0.0)
        lPow = float(lPow or 0.0)
        
        # Enhanced power threshold detection
        power_threshold = 0.5
        strong = max(uPow, lPow) >= power_threshold
        
        # Expanded eye movement detection for better REM detection
        eye_movements = ("look_left", "look_right", "lookLeft", "lookRight")
        facial_actions = ("wink_left", "wink_right", "blink", "furrow_brow", "raise_brow")
        
        # Score facial activity for sleep analysis
        activity_score = 0
        
        # Eye movements (primary indicator for REM sleep)
        if eyeAct in eye_movements and strong:
            activity_score = 2  # High score for eye movements
        # Other facial expressions (secondary indicators)
        elif eyeAct in facial_actions and strong:
            activity_score = 1  # Medium score for facial expressions
        # Weak but present activity
        elif eyeAct and max(uPow, lPow) >= 0.3:
            activity_score = 0.5  # Low score for weak activity
        
        self.fac_ring.push(t, activity_score)

    def on_eog_sample(self, t: float, v: float, src_fs_hint: float = 200.0):
        self._eog_decim += 1
        decim = max(1, int(src_fs_hint / self._eog_fs_target))
        if (self._eog_decim % decim) != 0:
            return
        self.eog_ring.push(t, float(v))
        self.eog_last_ts = t

    def eog_available(self, now: float) -> bool:
        return (self.eog_last_ts > 0.0) and ((now - self.eog_last_ts) <= self.eog_available_window_sec)

    # ------- 特徴量 -------
    def _mean(self, xs): return sum(xs) / len(xs) if xs else 0.0
    def _median(self, xs):
        if not xs: return 0.0
        s = sorted(xs); n = len(s)
        return s[n//2] if n % 2 else 0.5*(s[n//2-1] + s[n//2])
    def _var(self, xs):
        if not xs: return 0.0
        m = self._mean(xs)
        return sum((x-m)*(x-m) for x in xs)/len(xs)

    def _theta_alpha_ratio(self, vec):
        if not self.theta_idx or not self.alpha_idx: return 0.0
        th = self._mean([vec[i] for i in self.theta_idx if i < len(vec)])
        al = self._mean([vec[i] for i in self.alpha_idx if i < len(vec)])
        return (th / max(al, 1e-9)) if al > 0 else 0.0

    def _beta_rel(self, vec):
        if not self.beta_idx: return 0.0
        bt = self._mean([vec[i] for i in self.beta_idx if i < len(vec)])
        total = self._mean(vec) if vec else 1.0
        return bt / max(total, 1e-9)

    def _eog_saccade_rate(self, xs, fs=50.0):
        if len(xs) < 5: return 0.0
        diffs = [abs(xs[i]-xs[i-1]) for i in range(1, len(xs))]
        m = self._mean(diffs)
        sd = (self._mean([(d-m)*(d-m) for d in diffs]))**0.5
        if sd < 1e-9: return 0.0
        thr = m + 2.5*sd
        events, i = 0, 0
        while i < len(diffs):
            if diffs[i] > thr:
                events += 1
                i += int(0.1 * fs)  # 100msリフラクトリ
            else:
                i += 1
        return events / (len(xs)/fs)

    def _calculate_fac_activity_rate(self, fac_vals):
        """
        Calculate facial activity rate with weighted scoring
        Returns normalized activity rate between 0.0 and 1.0
        """
        if not fac_vals:
            return 0.0
        
        # Count different activity levels
        high_activity = sum(1 for v in fac_vals if v >= 2.0)  # Eye movements
        medium_activity = sum(1 for v in fac_vals if 1.0 <= v < 2.0)  # Facial expressions
        low_activity = sum(1 for v in fac_vals if 0.3 <= v < 1.0)  # Weak activity
        
        # Weighted calculation
        total_samples = len(fac_vals)
        weighted_score = (high_activity * 1.0 + medium_activity * 0.6 + low_activity * 0.3)
        
        return min(1.0, weighted_score / total_samples)

    def _is_fac_stream_active(self) -> bool:
        """
        Check if facial expression stream is actively providing data
        """
        if self.fac_ring.empty():
            return False
        
        # Check if we have recent facial expression data (within last 10 seconds)
        recent_data = [v for t, v in self.fac_ring.buf if time.time() - t <= 10.0]
        return len(recent_data) > 0

    def _epoch_features(self) -> Dict[str, float]:
        pow_vals = self.pow_ring.values()
        mot_vals = self.mot_ring.values()
        fac_vals = self.fac_ring.values()
        now = time.time()
        if not pow_vals or not mot_vals:
            return {}

        vec_len = len(pow_vals[0])
        ave_vec = [0.0] * vec_len
        for v in pow_vals:
            for i in range(min(vec_len, len(v))):
                ave_vec[i] += v[i]
        ave_vec = [x / len(pow_vals) for x in ave_vec]

        theta_alpha = self._theta_alpha_ratio(ave_vec)
        beta_rel = self._beta_rel(ave_vec)
        motion_rms = self._median(mot_vals)
        # Enhanced facial activity rate calculation
        fac_rate = self._calculate_fac_activity_rate(fac_vals)

        eog_on = self.eog_available(now)
        eog_vals = self.eog_ring.values()
        if eog_on and eog_vals:
            eog_var = self._var(eog_vals)
            eog_sacc = self._eog_saccade_rate(eog_vals, fs=self._eog_fs_target)
        else:
            eog_var = 0.0
            eog_sacc = 0.0

        if self.prev_eog_available is None or self.prev_eog_available != eog_on:
            print(f"[INFO] EOG availability changed: {self.prev_eog_available} -> {eog_on}")
            self.prev_eog_available = eog_on

        # Check facial expression stream status
        fac_active = self._is_fac_stream_active()

        return {
            "theta_alpha": theta_alpha,
            "beta_rel": beta_rel,
            "motion_rms": motion_rms,
            "fac_rate": fac_rate,
            "fac_active": 1.0 if fac_active else 0.0,
            "signal": self.dev_signal,
            "eog_var": eog_var,
            "eog_sacc": eog_sacc,
            "eog_on": 1.0 if eog_on else 0.0
        }

    def _raw_stage(self, f: Dict[str, float]) -> Tuple[str, float]:
        th_al = f["theta_alpha"]; mot = f["motion_rms"]; beta = f["beta_rel"]
        fac = f["fac_rate"]; eog_s = f.get("eog_sacc", 0.0); eog_on = f.get("eog_on", 0.0) > 0.5
        fac_active = f.get("fac_active", 0.0) > 0.5
        
        sleep_like = (th_al >= 1.2) and (mot <= 0.15)
        wake_like  = (th_al <  1.0) or  (mot >  0.25)

        scores = defaultdict(float)
        if wake_like:  scores["Wake"] += 0.7
        if sleep_like: scores["Light_NREM_candidate"] += 0.6

        # Enhanced REM detection with improved fac integration
        if eog_on:
            # Primary EOG-based REM detection
            rem_like = (mot <= 0.15) and ((eog_s >= 0.3) or (eog_s >= 0.2 and beta >= 0.30))
            if rem_like:
                scores["REM_candidate"] += 0.6
                if eog_s >= 0.6:
                    scores["REM_candidate"] += 0.2
                # Boost score if fac also indicates activity
                if fac_active and fac > 0.05:
                    scores["REM_candidate"] += 0.1
        else:
            # Enhanced fallback REM detection without EOG
            if fac_active:
                # Improved fac-based REM detection with multiple thresholds
                rem_like = (mot <= 0.15) and (
                    (beta >= 0.35 and fac > 0.03) or  # Strong beta + moderate fac
                    (beta >= 0.30 and fac > 0.08) or  # Moderate beta + high fac
                    (fac > 0.15)  # Very high fac activity alone
                )
                if rem_like:
                    scores["REM_candidate"] += 0.4  # Increased from 0.3
                    # Additional boost for very active facial expressions
                    if fac > 0.25:
                        scores["REM_candidate"] += 0.2
            else:
                # Fallback to basic beta detection when fac is not active
                rem_like = (mot <= 0.15) and (beta >= 0.40)  # Higher threshold
                if rem_like:
                    scores["REM_candidate"] += 0.25

        deep_like = (mot <= 0.10) and (beta <= 0.22)
        if deep_like:
            scores["Deep_candidate"] += 0.4

        if not scores:
            scores["Light_NREM_candidate"] = 0.5

        stage = max(scores.items(), key=lambda kv: kv[1])[0]
        conf = max(0.0, min(1.0, scores[stage]))
        return stage, conf

    def _smooth(self, now: float, new_stage: str, conf: float) -> str:
        prev = self.last_stage
        if prev is None:
            self.last_stage = new_stage
            self.hold_until = now + self.hold_min_sec
            return new_stage
        if now < self.hold_until and conf < 0.8 and new_stage != prev:
            return prev
        if prev == "Wake" and new_stage == "REM_candidate" and conf < 0.9:
            return prev
        if new_stage == "Deep_candidate" and conf < 0.7:
            return prev
        self.last_stage = new_stage
        self.hold_until = now + self.hold_min_sec
        return new_stage

    def step(self, now: float) -> Optional[Dict]:
        if self.dev_signal < MIN_QUALITY:
            return {
                "t": now, "stage": None, "confidence": 0.0,
                "theta_alpha": 0.0, "beta_rel": 0.0,
                "motion_rms": 0.0, "fac_rate": 0.0, "fac_active": 0.0,
                "signal": self.dev_signal, "eog_var": 0.0,
                "eog_sacc": 0.0, "eog_on": 0.0, "note": "poor_quality"
            }
        if now - self.last_epoch_time < HOP_SEC:
            return None
        self.last_epoch_time = now

        f = self._epoch_features()
        if not f:
            return None
        raw_stage, conf = self._raw_stage(f)
        stage = self._smooth(now, raw_stage, conf)
        row = {
            "t": now,
            "stage": stage,
            "confidence": round(conf, 3),
            **{k: round(v, 4) for k, v in f.items()}
        }
        self.rows.append(row)
        return row

# sleep_engine.py
import time
from collections import deque, defaultdict
from dataclasses import dataclass
from typing import Callable, Deque, Dict, List, Optional, Tuple

STAGES = ["Wake", "Light_NREM_candidate", "REM_candidate", "Deep_candidate"]


@dataclass
class SleepEngineConfig:
    """SleepEngine の閾値・パラメータ集約。

    既存挙動を保つためデフォルト値は従来のマジックナンバーに一致。
    """
    # 集約ウィンドウと更新周期
    epoch_sec: int = 30            # 30秒で特徴量要約
    hop_sec: int = 5               # 5秒ごとに更新
    min_quality: float = 0.3       # dev.signal の最低ライン

    # EOG 関連
    eog_fs_target: float = 50.0
    eog_available_window_sec: float = 10.0

    # 平滑化
    hold_min_sec: float = 20.0
    smooth_conf_ignore_change: float = 0.8
    smooth_wake_to_rem_conf_min: float = 0.9
    smooth_deep_conf_min: float = 0.7

    # しきい値（判定）
    theta_alpha_sleep: float = 1.2
    theta_alpha_wake: float = 1.0
    motion_sleep_max: float = 0.15
    motion_wake_min: float = 0.25
    motion_deep_max: float = 0.10
    beta_deep_max: float = 0.22
    beta_rem_assist_min: float = 0.30
    beta_rem_fallback_min: float = 0.35
    fac_rem_fallback_min: float = 0.02
    eog_sacc_rem_min: float = 0.3
    eog_sacc_rem_assist_min: float = 0.2
    eog_sacc_strong: float = 0.6

    # スコア重み
    w_wake: float = 0.7
    w_light: float = 0.6
    w_rem_eog: float = 0.6
    w_rem_fallback: float = 0.3
    w_deep: float = 0.4
    w_eog_strong_bonus: float = 0.2
    w_fallback_light: float = 0.5

class Ring:
    """時間窓付きリングバッファ。"""

    def __init__(self, seconds: int):
        self.seconds: int = seconds
        self.buf: Deque[Tuple[float, object]] = deque()  # (t, value)

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
    無料ストリーム（pow/mot/dev/fac）＋外部EOG（任意）から、
    Wake / Light_NREM_candidate / REM_candidate / Deep_candidate を推定する軽量エンジン。

    - EOGが無い環境でもFAC/β/体動で推定継続（自動フォールバック）。
    - 既存アプリからの利用を考慮し、公開メソッドのシグネチャは維持。
    """

    def __init__(self, config: Optional[SleepEngineConfig] = None, logger: Optional[Callable[[str], None]] = None):
        # 設定とロガー
        self.config = config or SleepEngineConfig()
        self._log: Optional[Callable[[str], None]] = logger

        # パワーバンドのラベル/インデックス
        self.pow_labels: List[str] = []
        self.theta_idx: List[int] = []
        self.alpha_idx: List[int] = []
        self.beta_idx: List[int] = []  # betaL + betaH

        # 入力リングバッファ
        self.pow_ring = Ring(self.config.epoch_sec)
        self.mot_ring = Ring(self.config.epoch_sec)
        self.fac_ring = Ring(self.config.epoch_sec)
        self.eog_ring = Ring(self.config.epoch_sec)

        # 状態
        self.dev_signal: float = 1.0
        self.last_epoch_time: float = 0.0
        self.last_stage: Optional[str] = None
        self.hold_until: float = 0.0

        # EOG状態
        self._eog_decim: int = 0
        self.eog_last_ts: float = 0.0
        self.prev_eog_available: Optional[bool] = None

        # 出力履歴
        self.rows: List[Dict] = []
        # 最後に観測したeyeAct（FEの生イベント表示用）
        self.fac_last_act: Optional[str] = ""
        self.fac_last_act_ts: float = 0.0

    # ------- ユーティリティ -------
    def _info(self, msg: str) -> None:
        if self._log:
            try:
                self._log(msg)
                return
            except Exception:
                pass
        # フォールバック出力
        print(msg)

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
    def on_pow(self, t: float, vec: List[float], consider_missing_zero: bool = True) -> None:
        if consider_missing_zero and all((v == 0 or v is None) for v in vec):
            return
        self.pow_ring.push(t, vec)

    def on_mot(self, t: float, mot_vec: List[float]) -> None:
        if len(mot_vec) >= 12:
            accx, accy, accz = mot_vec[9], mot_vec[10], mot_vec[11]
        else:
            accx = accy = accz = 0.0
        rms = (accx*accx + accy*accy + accz*accz) ** 0.5
        self.mot_ring.push(t, rms)

    def on_dev(self, t: float, signal: float) -> None:
        self.dev_signal = float(signal)

    def on_fac(self, t: float, eyeAct: Optional[str], uPow: float, lPow: float) -> None:
        strong = max(uPow or 0.0, lPow or 0.0) >= 0.5
        # eyeActの生文字列はそのまま保持（ダッシュボード表示用）
        if eyeAct is not None:
            self.fac_last_act = str(eyeAct)
            self.fac_last_act_ts = t
        # 左右注視の強いイベントはFACリングに1として蓄積
        if eyeAct in ("look_left", "look_right") and strong:
            self.fac_ring.push(t, 1)
        else:
            self.fac_ring.push(t, 0)

    def on_eog_sample(self, t: float, v: float, src_fs_hint: float = 200.0) -> None:
        self._eog_decim += 1
        decim = max(1, int(src_fs_hint / self.config.eog_fs_target))
        if (self._eog_decim % decim) != 0:
            return
        self.eog_ring.push(t, float(v))
        self.eog_last_ts = t

    def eog_available(self, now: float) -> bool:
        return (self.eog_last_ts > 0.0) and ((now - self.eog_last_ts) <= self.config.eog_available_window_sec)

    # ------- 特徴量 -------
    def _mean(self, xs: List[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    def _median(self, xs: List[float]) -> float:
        if not xs:
            return 0.0
        s = sorted(xs)
        n = len(s)
        return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])

    def _var(self, xs: List[float]) -> float:
        if not xs:
            return 0.0
        m = self._mean(xs)
        return sum((x - m) * (x - m) for x in xs) / len(xs)

    def _theta_alpha_ratio(self, vec: List[float]) -> float:
        if not self.theta_idx or not self.alpha_idx: return 0.0
        th = self._mean([vec[i] for i in self.theta_idx if i < len(vec)])
        al = self._mean([vec[i] for i in self.alpha_idx if i < len(vec)])
        return (th / max(al, 1e-9)) if al > 0 else 0.0

    def _beta_rel(self, vec: List[float]) -> float:
        if not self.beta_idx: return 0.0
        bt = self._mean([vec[i] for i in self.beta_idx if i < len(vec)])
        total = self._mean(vec) if vec else 1.0
        return bt / max(total, 1e-9)

    def _eog_saccade_rate(self, xs: List[float], fs: float = 50.0) -> float:
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

    def _epoch_features(self, now: float) -> Dict[str, float]:
        pow_vals = self.pow_ring.values()
        mot_vals = self.mot_ring.values()
        fac_vals = self.fac_ring.values()
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
        fac_rate = sum(1 for v in fac_vals if v >= 1) / max(len(fac_vals), 1)

        eog_on = self.eog_available(now)
        eog_vals = self.eog_ring.values()
        if eog_on and eog_vals:
            eog_var = self._var(eog_vals)
            eog_sacc = self._eog_saccade_rate(eog_vals, fs=self.config.eog_fs_target)
        else:
            eog_var = 0.0
            eog_sacc = 0.0

        if self.prev_eog_available is None or self.prev_eog_available != eog_on:
            self._info(f"[INFO] EOG availability changed: {self.prev_eog_available} -> {eog_on}")
            self.prev_eog_available = eog_on

        return {
            "theta_alpha": theta_alpha,
            "beta_rel": beta_rel,
            "motion_rms": motion_rms,
            "fac_rate": fac_rate,
            "signal": self.dev_signal,
            "eog_var": eog_var,
            "eog_sacc": eog_sacc,
            "eog_on": 1.0 if eog_on else 0.0,
            # eyeActの直近イベント名（ダッシュボードのバッジ表示で使用）
            "eye_act": self.fac_last_act or ""
        }

    def _raw_stage(self, f: Dict[str, float]) -> Tuple[str, float]:
        th_al = f["theta_alpha"]; mot = f["motion_rms"]; beta = f["beta_rel"]
        fac = f["fac_rate"]; eog_s = f.get("eog_sacc", 0.0); eog_on = f.get("eog_on", 0.0) > 0.5
        cfg = self.config

        sleep_like = (th_al >= cfg.theta_alpha_sleep) and (mot <= cfg.motion_sleep_max)
        wake_like = (th_al < cfg.theta_alpha_wake) or (mot > cfg.motion_wake_min)

        scores: Dict[str, float] = defaultdict(float)
        if wake_like:
            scores["Wake"] += cfg.w_wake
        if sleep_like:
            scores["Light_NREM_candidate"] += cfg.w_light

        if eog_on:
            rem_like = (mot <= cfg.motion_sleep_max) and (
                (eog_s >= cfg.eog_sacc_rem_min) or (eog_s >= cfg.eog_sacc_rem_assist_min and beta >= cfg.beta_rem_assist_min)
            )
            if rem_like:
                scores["REM_candidate"] += cfg.w_rem_eog
                if eog_s >= cfg.eog_sacc_strong:
                    scores["REM_candidate"] += cfg.w_eog_strong_bonus
        else:
            rem_like = (mot <= cfg.motion_sleep_max) and (beta >= cfg.beta_rem_fallback_min) and (fac > cfg.fac_rem_fallback_min)
            if rem_like:
                scores["REM_candidate"] += cfg.w_rem_fallback

        deep_like = (mot <= cfg.motion_deep_max) and (beta <= cfg.beta_deep_max)
        if deep_like:
            scores["Deep_candidate"] += cfg.w_deep

        if not scores:
            scores["Light_NREM_candidate"] = cfg.w_fallback_light

        stage = max(scores.items(), key=lambda kv: kv[1])[0]
        conf = max(0.0, min(1.0, scores[stage]))
        return stage, conf

    def _smooth(self, now: float, new_stage: str, conf: float) -> str:
        prev = self.last_stage
        cfg = self.config
        if prev is None:
            self.last_stage = new_stage
            self.hold_until = now + cfg.hold_min_sec
            return new_stage

        # ホールド時間内は弱い変更を抑制
        if now < self.hold_until and conf < cfg.smooth_conf_ignore_change and new_stage != prev:
            return prev
        # Wake → REM はより強い確信度が必要
        if prev == "Wake" and new_stage == "REM_candidate" and conf < cfg.smooth_wake_to_rem_conf_min:
            return prev
        # Deep 候補も最低限の確信度
        if new_stage == "Deep_candidate" and conf < cfg.smooth_deep_conf_min:
            return prev

        self.last_stage = new_stage
        self.hold_until = now + cfg.hold_min_sec
        return new_stage

    def step(self, now: float) -> Optional[Dict]:
        """一定間隔で特徴量を集約し、ステージ推定を返す。

        戻り値は既存アプリが想定するキーを維持する。poor quality ではダミー行を返す。
        """
        if self.dev_signal < self.config.min_quality:
            return {
                "t": now,
                "stage": None,
                "confidence": 0.0,
                "theta_alpha": 0.0,
                "beta_rel": 0.0,
                "motion_rms": 0.0,
                "fac_rate": 0.0,
                "signal": self.dev_signal,
                "eog_var": 0.0,
                "eog_sacc": 0.0,
                "eog_on": 0.0,
                "note": "poor_quality",
            }

        if now - self.last_epoch_time < self.config.hop_sec:
            return None
        self.last_epoch_time = now

        f = self._epoch_features(now)
        if not f:
            return None

        raw_stage, conf = self._raw_stage(f)
        stage = self._smooth(now, raw_stage, conf)
        row = {
            "t": now,
            "stage": stage,
            "confidence": round(conf, 3),
            **{k: (round(v, 4) if isinstance(v, (int, float)) else v) for k, v in f.items()},
        }
        self.rows.append(row)
        return row

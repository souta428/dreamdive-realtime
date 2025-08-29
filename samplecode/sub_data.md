# `Subcribe`（ストリーム購読）コードリーディング

> まず小ネタ：クラス名は **`Subcribe`（b が抜けてる）** です。もし外部から使うときは `Subscribe` と綴りを合わせたほうが他人にも伝わりやすいです。

このスクリプトは `cortex.py` の `Cortex` クライアントを使って、**EEG／Motion／Device／Performance Metric／Band Power** などのストリームを **購読（subscribe）→受信→ログ表示** する最小テンプレです。
低レベルの JSON-RPC / WebSocket は `Cortex` が受け持ち、ここは**フローと受信コールバック**に専念します。

---

## 構成とイベント束ね

```python
self.c = Cortex(app_client_id, app_client_secret, debug_mode=True, **kwargs)
self.c.bind(create_session_done=self.on_create_session_done)
self.c.bind(new_data_labels=self.on_new_data_labels)
self.c.bind(new_eeg_data=self.on_new_eeg_data)
self.c.bind(new_mot_data=self.on_new_mot_data)
self.c.bind(new_dev_data=self.on_new_dev_data)
self.c.bind(new_met_data=self.on_new_met_data)
self.c.bind(new_pow_data=self.on_new_pow_data)
self.c.bind(inform_error=self.on_inform_error)
```

* `Cortex` 側が `emit()` するイベントを**購読**して、ストリームごとの受信ハンドラへ振り分けます。
* `debug_mode=True` なので、送受信 JSON がターミナルに詳しく出ます（開発中は便利、運用では冗長）。

---

## 実行フロー

### `start(self, streams, headset_id='')`

1. `self.streams = streams` を保持
2. （任意）`set_wanted_headset(headset_id)`
3. **`self.c.open()`** で WebSocket 開始（※ `open()` は内部で `join()` するため**ブロッキング**。GUI等と併用するなら**別スレッド**で呼ぶのが安全）

> `open()` 後の「アクセス権→authorize→ヘッドセット→createSession」までは `cortex.py` が自動で進めます。

### `on_create_session_done(...)`

* セッション確立イベントを受け取ったら **`self.sub(self.streams)`** を実行し、目的のストリームを購読開始。

### `sub(self, streams)` / `unsub(self, streams)`

* それぞれ `subscribe`／`unsubscribe` を JSON-RPC で送る薄いラッパ。
* `streams` 候補（実装コメントより）：`'eeg'`, `'mot'`, `'dev'`, `'met'`, `'pow'`, `'eq'`

> **注意**：無料（Registered Developer）環境では `eeg` は利用不可のことが多く、**購読失敗**が返る点に留意（`pow/met/mot/dev/eq` はOK）。

---

## データラベルと各ストリームの受信

### `on_new_data_labels(...)`

* 初回購読成功時の**列ラベル情報**を受け取って表示します。以降の数値配列は、この順序に対応。

### 各データハンドラ

* `on_new_eeg_data`：`{'eeg': [...], 'time': ...}`（**末尾の MARKER 列は `cortex.py` 側で除外済み**）
* `on_new_mot_data`：IMU（四元数/加速度/磁気など）
* `on_new_dev_data`：デバイス状態（接触品質・バッテリ等）
* `on_new_met_data`：Performance metrics（eng/exc/rel/foc など）
* `on_new_pow_data`：Band power（各電極×帯域：theta, alpha, betaL, betaH, gamma）
* いずれも **`kwargs['data']` をそのまま `print`** しているだけなので、実装時は**ここで整形・保存・可視化**に差し替えるのが基本です。

### エラー

* `on_inform_error`：Cortex 側から返るエラー JSON をそのまま表示。
  実運用ではメッセージに応じて \*\*リトライや購読リストの見直し（`eeg`→外す等）\*\*を入れると堅牢。

---

## 実用 Tips

* **ブロック回避**：`open()` は戻らないため、CLI以外では

  ```python
  threading.Thread(target=lambda: s.start(streams), daemon=True).start()
  ```

  のように**別スレッドで起動**。
* **無料プランの構成例**：`streams = ['mot','dev','met','pow']`（`eeg` なし）。
  エクスポート時は `['MOTION','PM','BP']` を選ぶとよいです。
* **品質チェック**：`dev`/`eq` を入れて**接触品質**を監視し、しきい値を割ったら UI に警告したりデータを無視するなどのロジックを追加。
* **スループット**：生ログ `print` はコストになるので、実装では\*\*バッファリング／間引き（例：1Hz で要約）\*\*を推奨。
* **unsub/close**：デモでは明示していませんが、終了時は `unsub([...])`→`close_session()`→`close()` と**後始末**を。

---

## 使い方ミニ例

```python
s = Subcribe(client_id, client_secret)
streams = ['mot','dev','met','pow']       # 無料でもOKな構成
# 別スレッドで開始（推奨）
import threading
threading.Thread(target=lambda: s.start(streams), daemon=True).start()
```

---

### まとめ

* `Subcribe` は \*\*「購読開始→ラベル受信→ストリーム処理」\*\*を最短で試すための枠組み。
* **どのストリームを購読するか**と\*\*受信データの扱い（保存/可視化/分析）\*\*を差し替えるだけで、EPOC X＋Cortex のライブ処理にすぐ乗せ替えられます。
* 実運用では **ブロッキング回避・エラー/品質ハンドリング・ログの最適化**を加えると安定します。

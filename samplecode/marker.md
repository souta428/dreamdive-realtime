# `Marker`（記録＋マーカー注入＋エクスポート）コードリーディング

このスクリプトは **記録の開始→定期的なマーカー注入→記録停止→エクスポート** までを、`cortex.py` の `Cortex` クライアントにイベントで指示していく“実験運転テンプレ”です。
WSS/JSON-RPC の下回りは `Cortex` が担い、ここは**フローのオーケストレーション**に専念します。

---

## 全体構成

```python
class Marker:
    def __init__(...):
        # Cortex 初期化＆イベント購読
    def start(...):
        # 準備 → open()（以降はイベント自走）
    def create_record(...), stop_record(...), export_record(...)
    def add_markers(...), inject_marker(...), update_marker(...)
    # --- コールバック（イベントハンドラ） ---
    on_create_session_done, on_create_record_done, on_stop_record_done,
    on_inject_marker_done, on_export_record_done,
    on_warn_record_post_processing_done, on_inform_error
```

* **公開メソッド**：`start()` で始動、あとは**イベントコールバック**で順に進行。
* **非同期処理**：`add_markers()` は**別スレッド**で `time.sleep(3)` を挟みつつマーカーを自動注入。

---

## イベント購読（`__init__`）

```python
self.c.bind(
  create_session_done=self.on_create_session_done,
  create_record_done=self.on_create_record_done,
  stop_record_done=self.on_stop_record_done,
  inject_marker_done=self.on_inject_marker_done,
  export_record_done=self.on_export_record_done,
  inform_error=self.on_inform_error,
  warn_record_post_processing_done=self.on_warn_record_post_processing_done
)
```

`cortex.py` が `emit()` するイベントをここで受け、**ワークフローの各段階**を次へ進めます。

---

## 実行フロー（時系列）

### 1) `start(number_markers, headset_id)`

* カウンタを初期化（`self.number_markers`, `self.marker_idx=0`）。
* 任意の `headset_id` を指定可能（`set_wanted_headset`）。
* **`self.c.open()` を呼ぶ** → `cortex.py` で

  1. アクセス権チェック→承認（必要なら `requestAccess`）
  2. `authorize`（トークン取得）
  3. ヘッドセット検出・接続
  4. **セッション作成**
     までを自走 → 成功すると **`create_session_done`** が飛ぶ。

> 注意：`open()` は内部で WebSocket の `run_forever()` を **`join()`** しており**ブロッキング**です。CLI ならOK、GUI等と併用する場合は**別スレッドで `start()`** を。

### 2) `on_create_session_done`

* **記録を開始**：`self.create_record(self.record_title, description=...)`
  → `cortex.py` が `createRecord` を実行し、成功すると **`create_record_done`**。

### 3) `on_create_record_done(data)`

* `recordId` などを保存しログ表示。
* **マーカー注入スレッド**を起動：`threading.Thread(target=self.add_markers).start()`

### 4) `add_markers()`

* ループ `range(self.number_markers)` で

  * `marker_time = time.time()*1000`（**ミリ秒**）
  * `marker_label = f"{self.marker_label}_{m}"`
  * `inject_marker(marker_time, self.marker_value, marker_label, port='python_app')`
  * `time.sleep(3)`（**3秒おき**）
* 実際の送信は `inject_marker_request`（JSON-RPC `injectMarker`）。
* 成功すると **`inject_marker_done`** で uuid などが返る。

### 5) `on_inject_marker_done(data)`

* 1本注入ごとに `marker_idx += 1`。
* **規定本数に到達**したら **`stop_record()`** → `stopRecord` 実行 → **`stop_record_done`**。

### 6) `on_stop_record_done(data)`

* 記録の `uuid`・開始/終了時刻等をログ表示。
* 直ちにエクスポートはせず、\*\*post-processing 完了の警告（コード 30）\*\*を待つ設計。

### 7) `on_warn_record_post_processing_done(record_id)`

* Cortex からの **「ポストプロセス完了」通知**（warning 30）。
* これを受けて **`export_record(...)`** を呼ぶ：

  * `folder`（出力先パス）,
  * `stream_types`（例：`['EEG','MOTION','PM','BP']`）,
  * `format='CSV'`, `version='V2'`,
  * `record_ids=[record_id]`
* 成功で **`export_record_done`**。

### 8) `on_export_record_done(data)`

* 成功した `recordIds` を表示。
* **`self.c.close()`** でソケットを閉じて終了。

---

## ユーティリティメソッド

* `create_record(title, **kwargs)` → `createRecord`
* `stop_record()` → `stopRecord`
* `export_record(folder, stream_types, export_format, record_ids, version, **kwargs)` → `exportRecord`
* `inject_marker(time_ms, value, label, **kwargs)` → `injectMarker`
* `update_marker(marker_id, time_ms, **kwargs)` → `updateMarker`（今回は未使用）

> **時刻単位**：`inject_marker` / `update_marker` とも **ミリ秒**で送っている点が重要（`time.time()*1000`）。Cortex API の仕様に合わせる必要があります。

---

## 実務ポイント／落とし穴

* **record\_title / export\_folder の検証**
  空のタイトルや書き込み不可ディレクトリは `cortex.py` 側で警告→クローズになります。`main()` で**必ず有効値をセット**しましょう。
* **エクスポートのタイミング**
  記録停止直後はまだファイル化が完了していないため、\*\*warning 30（post-processing done）\*\*を待ってから `exportRecord` を呼ぶのが正解（このコード通り）。
* **ブロッキング動作**
  先述通り `open()` がブロックするので、**アプリから使うなら別スレッド起動**を。
* **スレッド安全性**
  `add_markers()` は別スレッドで `self.c.inject_marker_request` を叩きます。WebSocket送信は `websocket-client` が内部ロックしてくれる前提ですが、**大量同時送信**は避け、現在のように**一定間隔**で送るのが安全。
* **タイムスタンプ精度**
  Python の `time.time()` はUNIX秒(浮動小数)。**PCクロックと Cortex/ヘッドセットの時刻差**があると解析時にズレます。必要なら**時刻同期（NTP）**や**相対時刻での解析**を検討。
* **エラー処理**
  `on_inform_error` は内容を出力するだけ。運用では **リトライ戦略**（再接続、記録再開、失敗マーカーの再注入など）を入れると堅牢。

---

## 使い方（`main()` の意味と設定）

```python
m = Marker(your_app_client_id, your_app_client_secret)

# 記録メタ情報
m.record_title = 'my-session-001'          # 必須
m.record_description = 'stim A/B test'     # 任意

# マーカー情報（add_markers()で使用）
m.marker_value = 'stim_on'                 # 必須（例: 条件名/刺激ID）
m.marker_label = 'trial'                   # 必須（連番が付与される）

# エクスポート設定（warning 30 後に実施）
m.record_export_folder = '/path/to/out'    # 書込可能なパス
m.record_export_data_types = ['EEG','MOTION','PM','BP']
m.record_export_format = 'CSV'
m.record_export_version = 'V2'

marker_numbers = 10
m.start(marker_numbers)
```

* 起動後は **3秒間隔で 10 本のマーカー**を注入 → 記録停止 → ポスト処理完了を待って **CSVをエクスポート** → ソケットを閉じて終了。

---

**Answer:**

「マーカー (marker)」とは、**EEGや各種ストリーミングデータのタイムライン上に「イベントが発生した時刻」を記録する印**のことです。

### 役割

1. **イベントの同期**

   * 刺激提示ソフト（映像・音・課題）とEEGデータを同期させるために利用します。
   * 例：画面に画像を出した瞬間に「画像提示開始」というマーカーを注入 → 後でEEG解析でその時刻を基点にエポック（区間切り出し）できる。

2. **試行や条件の区別**

   * 実験で「条件A」「条件B」を提示したとき、それぞれのタイミングで異なるマーカーラベルを入れることで、後処理で条件別にデータを分けられる。

3. **記録ログの補強**

   * EEGや行動ログだけでなく、「被験者がボタンを押した」「刺激が終わった」などのイベントを正確な時刻で残せる。

### Cortex APIにおける仕様

* `injectMarker` API を呼ぶと、現在のセッションの記録に「マーカー」が追加される。
* **必須パラメータ**

  * `time`: ミリ秒単位の時刻（例：`time.time()*1000`）
  * `value`: マーカーの値（数値や文字列）
  * `label`: ラベル（イベント名）
* 追加の任意パラメータとして `port` や `extra` 情報を付けられる。

### データ解析での使い方

* EEGデータと一緒にエクスポートすると、マーカーの列がCSVなどに含まれる。
* EEGLABやMNE-Pythonなどの解析ツールで「イベント情報」として読み込まれ、刺激呈示や反応に対応する脳波区間（エポック）を切り出すのに使える。

---

✅ まとめると：
**「マーカー」とは、EEG記録の中で「ここで重要なイベントが起きた」という時点を示すタイムスタンプ付きのラベル**です。実験刺激や行動イベントとデータを正確に対応づけるために不可欠です。

---

**Recommendation:** 実際にEEG解析まで使う予定があるなら、マーカーのラベル体系（例：`stim_A_onset`, `stim_B_onset`, `response_correct`）を最初から統一的に設計しておくのがおすすめです。
**Next step:** Cortexの`injectMarker`で送ったマーカーが、エクスポートCSVのどの列にどう出力されるかを確認してみるとよいです。


## まとめ

* `Marker` は、**記録の開始→自動マーカー注入→停止→エクスポート** までを**イベント駆動**で回す最小構成。
* 実験系アプリに組み込む際は、**ブロック回避（別スレッド）**・**入力検証**・**時刻同期**・**失敗時のリトライ**を足すと実運用に耐えます。
* そのまま流用して、**外部刺激提示アプリからマーカーを注入**（値・ラベルに試行ID/条件を埋める）→**後段で EEG と突き合わせ**る、という一般的な設計にフィットします。

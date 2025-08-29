# `cortex.py` コードリーディング（詳細解説）

EPOC X 等の EMOTIV ヘッドセットと **Cortex（`wss://localhost:6868`）** を WebSocket/JSON-RPC でやり取りする**同期型クライアント**です。
クラス `Cortex` が**接続 → アクセス権確認 → 認可 → ヘッドセット検出/接続 → セッション作成 → ストリーム購読/解除 → 記録/エクスポート/マーカー/MC設定**までを一通りラップします。
イベント通知には `pydispatch.Dispatcher` を用い、**受信スレッドからシグナル（`emit()`）で上位へ**渡す設計です。

---

## ファイル冒頭：環境チェック

* **Python バージョン確認**：`2.7+` または `3.4+` でないと即終了。
* **外部依存**：

  * `websocket-client`（`websocket.WebSocketApp` を使用）
  * `python-dispatch`（`pydispatch.Dispatcher` を継承してイベント発火）

→ どれか無いと、**エラーメッセージ＋pip コマンド案内**を出して `sys.exit(1)`。

---

## 定数：リクエストID／警告コード

* `QUERY_HEADSET_ID`, `AUTHORIZE_ID`, `CREATE_SESSION_ID`, …
  各 JSON-RPC リクエストに**固定の id**を割り当て、**レスポンスの振り分け**で使用。
* 警告コード（`ACCESS_RIGHT_GRANTED`, `HEADSET_CONNECTED`, `CORTEX_STOP_ALL_STREAMS` など）
  Cortex 側の **warning** 通知を**意味ごとに分岐**するための定数。

---

## イベント（Dispatcher のチャンネル）

```python
_events_ = [
 'inform_error', 'create_session_done', 'query_profile_done',
 'load_unload_profile_done','save_profile_done','get_mc_active_action_done',
 'mc_brainmap_done','mc_action_sensitivity_done','mc_training_threshold_done',
 'create_record_done','stop_record_done','warn_cortex_stop_all_sub',
 'warn_record_post_processing_done','inject_marker_done','update_marker_done',
 'export_record_done','new_data_labels','new_com_data','new_fe_data',
 'new_eeg_data','new_mot_data','new_dev_data','new_met_data','new_pow_data',
 'new_sys_data'
]
```

* **API 呼び出しの完了**や**ストリーム新着**を、上位層へ**非同期通知**するための名前群。

---

## クラス `Cortex` と主なフィールド

### `__init__(self, client_id, client_secret, debug_mode=False, **kwargs)`

* **引数必須**：`client_id` / `client_secret`（空だと `ValueError`）
* 任意：`license`, `debit`, `headset_id`（`kwargs` で受け取り）
* 内部状態：

  * `session_id`, `headset_id`, `auth`（cortexToken）, `license`, `debit`
  * `isHeadsetConnected`（クオリティ維持のため“refresh”制御に使う）
  * `debug`（リクエスト/レスポンスの詳細ログ）

---

## 接続まわり

### `open(self)`

* `wss://localhost:6868` に `WebSocketApp` で接続。
* **自己署名 CA** を前提（`../certificates/rootCA.pem` を指定）。検証を外す場合は `sslopt={"cert_reqs": ssl.CERT_NONE}` に差し替え可。
* WebSocket の **イベントハンドラ**を自身の `on_open/on_message/on_error/on_close` にバインド。
* 新規スレッドで `run_forever` を起動し、**直後に `join()`** → **`open()` はソケットが閉じるまで戻らない**（＝前面ブロッキング動作）。

> **注意**：UI スレッドで `open()` を呼ぶと**戻ってこない**ため、上位アプリでは**別スレッド**で起動する設計が必要。

### `close(self)`

* WebSocket を明示的に閉じる。

### WebSocket コールバック

* `on_open`: “websocket opened” を出力し、**初期手順 `do_prepare_steps()`** を開始。
* `on_error`: 例外内容を標準出力。
* `on_close`: 終了ログ。

---

## 受信ディスパッチ

### `on_message(self, *args)`

* 受信文字列を `json.loads`。キーで分岐：

  * `'sid'` がある → **ストリームデータ**（`handle_stream_data`）
  * `'result'` → **成功レス**（`handle_result`）
  * `'error'` → **エラーレス**（`handle_error`）
  * `'warning'` → **警告**（`handle_warning`）

---

## 成功レス処理：`handle_result(self, recv_dic)`

`id`（＝送信時の定数）で**どのリクエストの応答か**を判定し、**後続処理**や**イベント発火**を行います。主な分岐：

* **アクセス権**

  * `HAS_ACCESS_RIGHT_ID`：`accessGranted` が `True` なら `authorize()`、`False` なら `request_access()`
  * `REQUEST_ACCESS_ID`：承認済みなら `authorize()`、未承認なら **Launcher 側承認待ち**（`warnings.warn`）

* **認可**

  * `AUTHORIZE_ID`：`cortexToken` を `self.auth` に保存 → `refresh_headset_list()` → `query_headset()`

* **ヘッドセット検出**

  * `QUERY_HEADSET_ID`：リストを走査し、**希望ID**が接続済みなら `create_session()`、`discovered` なら `connect_headset()`、`connecting` なら 3 秒待って再クエリ。
    希望ID未指定なら**最初の1台を既定**にして再クエリ。

* **セッション**

  * `CREATE_SESSION_ID`（作成時）：`self.session_id` を保存し、`create_session_done` を `emit()`
  * **セッション終了**は `close_session()`（ただしレスポンスは同じ ID を使う点に注意）

* **購読**

  * `SUB_REQUEST_ID`：`success` 側の各 `streamName` の **`cols`（列名）を取り出し**、`extract_data_labels()` で構造整形 → `new_data_labels` を `emit()`
    失敗は `message` をログ（例：ライセンス外ストリームなど）

  * `UNSUB_REQUEST_ID`：成功/失敗をログ

* **プロフィール**

  * `QUERY_PROFILE_ID`：一覧を `query_profile_done` で通知
  * `SETUP_PROFILE_ID`：`create` → 直後に `load`、`load/unload/save` はそれぞれのイベントを `emit()`
  * `GET_CURRENT_PROFILE_ID`：ロード済みプロファイル名をチェックし、状況に応じて `load/unload` を発行

* **MC/記録/エクスポート/マーカー**

  * `MENTAL_COMMAND_ACTIVE_ACTION_ID` → `get_mc_active_action_done`
  * `MENTAL_COMMAND_TRAINING_THRESHOLD` → `mc_training_threshold_done`
  * `MENTAL_COMMAND_BRAIN_MAP_ID` → `mc_brainmap_done`
  * `SENSITIVITY_REQUEST_ID` → `mc_action_sensitivity_done`
  * `CREATE_RECORD_REQUEST_ID`：`record.uuid` を保持 → `create_record_done`
  * `STOP_RECORD_REQUEST_ID` → `stop_record_done`
  * `EXPORT_RECORD_ID`：成功/失敗を仕分け → `export_record_done`
  * `INJECT_MARKER_REQUEST_ID` / `UPDATE_MARKER_REQUEST_ID` → 各 `*_done`

---

## エラー／警告処理

### `handle_error(self, recv_dic)`

* レスポンス `id` を含むエラーを受け、**要求IDつきで `inform_error`** を `emit()`。

  * 例）セッション上限、未許可ストリーム、無効パラメータなど。

### `handle_warning(self, warning_dic)`

* Cortex 側の **非致死的イベント**をハンドリング：

  * `ACCESS_RIGHT_GRANTED` → **再度 `authorize()`**
  * `HEADSET_CONNECTED` → **`query_headset()` → セッション作成へ**
  * `CORTEX_AUTO_UNLOAD_PROFILE` → ローカルの `profile_name` を空に
  * `CORTEX_STOP_ALL_STREAMS`：自セッションIDなら `warn_cortex_stop_all_sub` を通知し `session_id` を破棄
  * `CORTEX_RECORD_POST_PROCESSING_DONE`：`recordId` を通知
  * `HEADSET_SCANNING_FINISHED`：未接続なら **`refresh_headset_list()`**（接続済みなら推奨しない）

---

## ストリームデータ処理：`handle_stream_data(self, result_dic)`

受信 JSON のキー別に**辞書へ整形**し、イベント発火：

* `com`（メンタルコマンド）→ `{'action', 'power', 'time'}` → `new_com_data`
* `fac`（表情）→ `{'eyeAct','uAct','uPow','lAct','lPow','time'}` → `new_fe_data`
* `eeg`（生EEG）→ `{'eeg': array, 'time'}` ただし **末尾の MARKERS を `pop()` で除去** → `new_eeg_data`
* `mot`（IMU）→ `{'mot', 'time'}` → `new_mot_data`
* `dev`（デバイス）→ `{'signal', 'dev', 'batteryPercent', 'time'}` → `new_dev_data`
* `met`（メトリクス）→ `{'met', 'time'}` → `new_met_data`
* `pow`（バンドパワー）→ `{'pow', 'time'}` → `new_pow_data`
* `sys`（システムイベント）→ そのまま → `new_sys_data`

> **列名の対応付け**は、購読応答（`SUB_REQUEST_ID`）の `cols` を `extract_data_labels()` が処理して `new_data_labels` で通知します。デバイス系（`dev`）は入れ子レベルが異なるため特別扱い。

---

## JSON-RPC 送信系（主な公開メソッド）

> いずれも `self.ws.send(json.dumps(...))` で **直接 WebSocket へ**投げます。

### デバイス・認可・セッション

* `query_headset()`：近傍ヘッドセット列挙
* `connect_headset(headset_id)` / `disconnect_headset()`：接続/切断
* `refresh_headset_list()`：**再スキャン**（接続中は品質低下のため非推奨）
* `has_access_right()` / `request_access()` / `authorize()`：アクセス権確認 → 要承認ならリクエスト → 認可（`cortexToken` 取得）
* `create_session()` / `close_session()`：アクティブセッションの生成/終了
  ※既に `session_id` がある状態で `create_session()` を呼ぶと **警告で return**（二重作成防止）

### ストリーム

* `sub_request(streams)` / `unsub_request(streams)`
  `streams` は `["pow","met","eeg","mot","dev","eq","com","fac","sys"]` などの配列。
  成否は `SUB_REQUEST_ID` / `UNSUB_REQUEST_ID` の応答でログ・イベント。

### プロファイル（MC/Facial 用）

* `query_profile()` / `get_current_profile()` / `setup_profile(profile, status)`
  `status`：`create` / `load` / `unload` / `save` を切替。
* `train_request(detection, action, status)`：トレーニング開始/承認/破棄フロー。
  例：`detection="mentalCommand"`, `action="push"`, `status="start"/"accept"/"reject"`

### 記録・エクスポート・マーカー

* `create_record(title, **kwargs)` / `stop_record()`
  `title` が空だと警告→ソケットを閉じて return。
* `export_record(folder, stream_types, export_format, record_ids, version, **kwargs)`
  `CSV` の場合のみ `version` 必須。成功/失敗を仕分けて `export_record_done`。
* `inject_marker_request(time, value, label, **kwargs)` / `update_marker_request(marker_id, time, **kwargs)`
  実験の**同期マーカー**注入・更新。

### メンタルコマンド詳細

* `get_mental_command_action_sensitivity(profile)` / `set_mental_command_action_sensitivity(profile, values)`
* `get_mental_command_active_action(profile)` / `set_mental_command_active_action(actions)`
* `get_mental_command_brain_map(profile)`
* `get_mental_command_training_threshold(profile)`
  → それぞれ応答で `*_done` イベント。

---

## データラベル整形：`extract_data_labels(self, stream_name, stream_cols)`

* 共通：`{'streamName': name, 'labels': [...]}` を作って `new_data_labels` を `emit()`
* 特殊ケース：

  * `eeg`：**末尾の `MARKERS` 列を除外**（`stream_cols[:-1]`）
  * `dev`：`stream_cols[2]`（接触品質 `cq` 見出しのみ）を採用
  * その他：そのまま

→ **可視化や保存時に、配列データの**列名（センサー順・項目名）**を対応付ける用途**。

---

## 初期手順の自動実行：`do_prepare_steps(self)`

`on_open()` から呼ばれ、**最初に `has_access_right()`** を投げます。
以降のフローは **レスポンスに応じて `handle_result()` が段階的に連鎖**：

```
has_access_right
  ├─ accessGranted=True  → authorize
  └─ accessGranted=False → request_access → (承認後) authorize
authorize → refresh_headset_list → query_headset
query_headset → （状態に応じて）connect_headset / create_session / 再クエリ
create_session → create_session_done (event)
```

---

## 実行時の典型フロー（全体像）

1. `Cortex(client_id, client_secret, license=..., debit=..., headset_id=...)`
2. `open()`
   → `on_open()` → `do_prepare_steps()` → `has_access_right()`
3. `authorize()`（`cortexToken` 取得）
4. `refresh_headset_list()` → `query_headset()`
   → `connect_headset()` or 直接 `create_session()`
5. `sub_request(["pow","met","dev","eq","mot", ...])`
6. 以降：`on_message()` がストリームを受けて `new_*_data` を `emit()`
   終了時は `unsub_request()` → `close_session()` → `close()`

---

## 設計上のポイント／落とし穴

* **`open()` がブロッキング**：内部でスレッドを起動→**即 `join()`** するため、**WebSocket が閉じるまで戻りません**。上位で別スレッドに逃がすか、設計を合わせる必要があります。
* **ライセンス/アクセス権**：`authorize` までの一連フローは**Launcher 側の承認**に依存。承認されるまで自動で再試行分岐します。
* **デバイス再スキャン**：`HEADSET_SCANNING_FINISHED` 時のみ `refresh_headset_list()` を呼ぶなど、**品質低下を避ける**配慮あり。
* **`dev`/`eeg` 特殊処理**：`dev` の列ラベル、`eeg` の末尾 `MARKERS` 除外は**ダウンストリームの混乱を避ける**ための実務的対応。
* **イベント駆動**：API 応答・ストリーム更新は**すべてイベントで通知**。上位アプリは `Dispatcher.connect()` で必要なイベントに**購読**し、UI 更新や保存を行うのが前提。

---

## 使い始めるための最小コード（疑似）

```python
c = Cortex(client_id="...", client_secret="...", debug_mode=True)
# 任意: c.set_wanted_headset("EPOCPLUS-...")
# 任意: c.set_wanted_profile("myprofile")

# イベント購読
c.connect(c.create_session_done, lambda data: print("Session:", data))
c.connect(c.new_pow_data,        lambda d: print("POW:", d))
c.connect(c.new_data_labels,     lambda d: print("LABELS:", d))

# 接続開始（戻らない点に注意）
threading.Thread(target=c.open, daemon=True).start()

# 準備完了後に購読（例：どこかのイベントで）
# c.sub_request(["pow","met","dev","eq"])
```

---

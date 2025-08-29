# `LiveAdvance`（メンタルコマンドのライブ実行）コードリーディング

このスクリプトは、**学習済みプロファイル**を読み込み、**メンタルコマンド（`com`）のライブ値**を購読・表示する“実行（推論）用”の薄いオーケストレーターです。
低レベルの JSON-RPC/WSS は同ディレクトリの `cortex.py` が担当し、ここは **イベント駆動**でフロー制御だけを行います。

---

## 役割と全体フロー

### 何をする？

1. アクセス権確認 → 認可 → ヘッドセット接続 → セッション作成
2. **プロファイルの存在確認 → ロード or 作成**
3. **有効アクション（最大4つ）取得 → 感度値の取得/設定 → プロファイル保存**
4. `com` ストリームを購読して **`{'action', 'power', 'time'}`** を受信・表示

### 実行の時系列（イベントで自走）

```
start()
 └─ c.open()  ※以降はcortex.pyのイベントで進行
     └─ on_create_session_done → query_profile
         └─ on_query_profile_done
             ├─ 既存 → get_current_profile（cortex.py側でload/unload判断→完了時イベント）
             └─ 未存在 → setup_profile('create')→（完了時イベント）
                 └─ on_load_unload_profile_done(isLoaded=True)
                       └─ get_active_action(profile)
                           └─ on_get_mc_active_action_done → get_sensitivity(profile)
                               └─ on_mc_action_sensitivity_done
                                   ├─ 取得フェーズ → set_sensitivity([7,7,5,5])
                                   └─ 設定フェーズ → save_profile
                                       └─ on_save_profile_done → subscribe(['com'])
                                           └─ on_new_com_data（ライブ受信）
```

---

## クラス構成と状態

```python
class LiveAdvance:
    self.c               # Cortex クライアント（debug=True）
    self.profile_name    # 対象プロファイル名
```

### イベント購読（コンストラクタ）

```python
self.c.bind(
  create_session_done=self.on_create_session_done,
  query_profile_done=self.on_query_profile_done,
  load_unload_profile_done=self.on_load_unload_profile_done,
  save_profile_done=self.on_save_profile_done,
  new_com_data=self.on_new_com_data,
  get_mc_active_action_done=self.on_get_mc_active_action_done,
  mc_action_sensitivity_done=self.on_mc_action_sensitivity_done,
  inform_error=self.on_inform_error
)
```

`cortex.py` 側の `emit(...)` がここに流れ、**状態遷移をドライブ**します。

---

## パブリックメソッドの読み解き

### `start(profile_name, headset_id='')`

* 必須の `profile_name` を保存し、`cortex.py` へ

  * `set_wanted_profile(profile_name)`
  * （任意）`set_wanted_headset(headset_id)`
* **`self.c.open()` を呼ぶ**（内部で WebSocket `run_forever` → `join()` するため**呼び出しスレッドはブロック**）

  * CLI単体実行はOK。GUI等と併用なら**別スレッドで `start()`** を呼ぶのが安全。

### `load_profile(profile_name)` / `unload_profile(profile_name)` / `save_profile(profile_name)`

* それぞれ `setup_profile(..., 'load'|'unload'|'save')` の薄いラッパ。
* 本スクリプトでは **ロードは自動フロー内**で実行、**保存は感度設定後**・**購読直前**に行う。

### `subscribe_data(streams)`

* `self.c.sub_request(streams)` の薄いラッパ。
  ここでは保存後に `['com']` を購読。

### `get_active_action(profile_name)` / `get_sensitivity(profile_name)` / `set_sensitivity(profile_name, values)`

* **有効アクション**（ニュートラルを除く最大4つ）と、その\*\*感度値（1–10）\*\*の取得/設定 API。
* 感度 **`values` の順序は「有効アクションの順序」に厳密対応**（コメントに注意書きあり）。

---

## コールバック（イベントハンドラ）

### 準備段階

* `on_create_session_done`
  セッション確立 → **`query_profile()`** で存在確認へ。

* `on_query_profile_done(data=profile_list)`

  * 既存なら **`get_current_profile()`**（cortex側のハンドラが load/unload を誘導）
  * 未存在なら **`setup_profile('create')`**

* `on_load_unload_profile_done(isLoaded)`

  * `True`（ロード済み）→ **`get_active_action(profile)`**
  * `False`（アンロード済み）→ ログ出力のみ（※後述の注意点参照）

### 感度調整 → 保存 → 購読

* `on_get_mc_active_action_done(data)`
  → **`get_sensitivity(profile)`** を続けて要求。
* `on_mc_action_sensitivity_done(data)`

  * **取得フェーズ**（`data` が `list`）：
    デモとして固定値 **`[7,7,5,5]`** を設定 → `set_sensitivity(...)`
  * **設定結果フェーズ**（`data` が `list` 以外）：
    → **`save_profile`** を呼ぶ（感度変更を永続化）
* `on_save_profile_done`
  → **`subscribe(['com'])`**（ライブデータ購読を開始）
* `on_new_com_data(data)`
  `{'action': 'push', 'power': 0.85, 'time': 1647...}` の形で**リアルタイムに出力**。

### エラー

* `on_inform_error(error_data)`
  `ERR_PROFILE_ACCESS_DENIED`（プロファイルアクセス権エラー）時は **ヘッドセット切断**で復旧。

---

## 受信データ形式（`com`）

* `on_new_com_data` で受け取る `data` は **辞書**：

  * `action`: 予測ラベル（例：`neutral`, `push`, `pull`, `lift`, `drop` …）
  * `power`: 信頼度/強度（0.0–1.0）
  * `time`: 受信時刻（UNIX秒）
* **注意**：`neutral` は**有効アクションには含まれない**が、ライブ予測としては `neutral` が出ます。

---

## この設計の意図（ポイント）

* **イベント駆動の“自走”**：準備→ロード→調整→保存→購読 まで、**ユーザー操作なし**で到達する。
* **感度の取得→設定→保存**の一括流れ：
  実運用では**任意の感度**に（または**設定自体をスキップ**して既存値のまま購読開始してもOK）。
* **疎結合**：WSS/JSON-RPC・状態機械は `cortex.py` に吸収し、ここは**高レベル手順**に専念。

---

## 代表的な落とし穴＆改善ポイント

1. **`open()` がブロッキング**
   `cortex.py` の `open()` は `join()` するため戻りません。
   → **別スレッドで `start()`** を呼ぶ、または `cortex.py` 側に**非ブロッキングモード**を用意する。

2. **プロファイルが“他アプリでロード済み”のケース**
   `get_current_profile()` の結果、**他アプリがロード**していると `cortex.py` は `unload` を試み、
   本ハンドラ `on_load_unload_profile_done(False)` は **`self.profile_name=''` にするだけ**で次に進みません。
   → **改善案**：`False` のときに **すぐ `setup_profile(self.profile_name_backup,'load')`** を試みるか、
   **ユーザーに再ロードを促す**分岐を入れる。

3. **感度値の順序と個数**
   `set_sensitivity(values)` は **有効アクション順**（`mentalCommandActiveAction` の戻り）に対応。
   アクティブ数が 2〜3 の場合、**残りの値は無視**される実装想定。
   → 取得したアクション配列長に合わせて **値リストを動的生成**するのが無難。

4. **終了処理**
   このファイルでは `unsub` / `close_session` / `close()` の明示がないため、
   アプリとして組み込むなら**終了フック**を追加しておくとクリーン。

5. **品質チェック**（おすすめ）
   `com` 購読前に `dev`/`eq` を一時購読し、**接触品質（CQ）や欠損率**が閾値以上か検査してからライブ開始すると安定します。

---

## 使い方（最低限）

1. EMOTIV Launcher にログインし、ヘッドセット（ドングル or BT）を接続。
2. アプリの Client ID / Secret を取得。
3. **学習済みプロフィール名**を `trained_profile_name` に設定。
4. スクリプトを実行（CLIならそのまま、GUIや他スレッドと併用なら `start()` を別スレッドで）。

---

## まとめ

* `LiveAdvance` は、**学習済みプロファイルのロード→感度調整→保存→`com`購読**を**イベントで自走**するランタイム層。
* 受信する `com` は **`action`×`power`** のシンプルな辞書で、デモ用途や外部アプリ連携に扱いやすい。
* 運用では **ブロッキング回避・ロード競合時のリカバリ・品質ゲーティング**を加えると堅牢になります。

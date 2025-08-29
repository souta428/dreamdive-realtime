# `Train`（メンタルコマンド学習）コードリーディング

このスクリプトは `cortex.py` の `Cortex` クライアントを使って、**メンタルコマンド（`mentalCommand`）のトレーニング**をイベント駆動で自動進行させる薄いオーケストレーターです。
責務は「**準備（認可/接続/セッション）→プロフィール用意→`sys`購読→各アクションを順に学習→保存→アンロード**」。

---

## 全体構成

```python
class Train:
    self.c              # Cortex クライアント（debug=True）
    self.profile_name   # 学習対象プロフィール
    self.actions        # 学習するアクション配列（例: ['neutral','push','pull']）
    self.action_idx     # 現在のアクションの添字
```

* **イベント購読**（`__init__`）
  `create_session_done / query_profile_done / load_unload_profile_done / save_profile_done / new_data_labels / new_sys_data / inform_error` を `on_*` にバインド。
  → 低レベルの JSON-RPC 応答は `cortex.py` が `emit()` し、ここは**ハンドラで次の手を出す**。

---

## 起動と準備

### `start(profile_name, actions, headset_id='')`

* 引数検証後、内部状態をセット（`actions`, `action_idx=0`）。
* 希望プロフィール/ヘッドセットを `Cortex` に伝える（`set_wanted_profile`, `set_wanted_headset`）。
* **`self.c.open()`** を実行（WebSocket を開始）。

  > 注意：`open()` は内部で `run_forever()` → `join()` のため**呼び出しスレッドはブロック**。CLI ならOK、GUI/並行処理なら**別スレッド起動**推奨。

### 以降はイベントで自走（ざっくり時系列）

```
open()
 └ on_open（cortex.py）→ do_prepare_steps → hasAccessRight → authorize
   → refresh_headset_list → queryHeadset → createSession
     └ create_session_done → on_create_session_done → query_profile
         └ query_profile_done → (既存) get_current_profile / (未存在) setup_profile('create')
             └ load_unload_profile_done(True) → subscribe_data(['sys'])
                 └ new_data_labels(sys) 到着 → train_mc_action('start')
                     └ new_sys_data: MC_Succeeded → train_mc_action('accept')
                     └ new_sys_data: MC_Completed/MC_Rejected → 次アクションに進み 'start'
                     └ （最後まで終えたら）save_profile → save_profile_done → unload_profile → close
```

---

## コアAPIの薄いラッパ

* `subscribe_data(streams)` → `self.c.sub_request(streams)`
  学習イベント受信用に **`['sys']`** を購読。
* `load_profile/unload_profile/save_profile` → `setup_profile(..., 'load'|'unload'|'save')`
* `get_active_action/brain_map/training_threshold` → 上位APIの呼び出し（本ファイルでは表示用フック）

---

## 学習制御の要：`train_mc_action(status)`

```python
if self.action_idx < len(self.actions):
    action = self.actions[self.action_idx]
    self.c.train_request(detection='mentalCommand', action=action, status=status)
else:
    self.c.setup_profile(self.profile_name, 'save')
    self.action_idx = 0
```

* `status` は `start / accept / reject / erase / reset`。
* **この関数自体は送信だけ**。**進行は `sys` ストリームのイベント**で決まる（下記）。

---

## コールバック（イベントハンドラ）の動き

### 準備〜プロファイル

* `on_create_session_done` → `query_profile()`
* `on_query_profile_done(data=profile_list)`

  * 既存：`get_current_profile()`（誰がロードしたか等は `cortex.py` 側で処理）
  * 未存在：`setup_profile('create')`
* `on_load_unload_profile_done(isLoaded)`

  * `True`：**`subscribe_data(['sys'])`**（学習イベント受信を開始）
  * `False`：プロファイル名を空にして `close()`（終了）

### 学習イベント駆動

* `on_new_data_labels(data)`
  `streamName=='sys'` 到着を合図に **`train_mc_action('start')`** で最初のアクション学習を開始。
* `on_new_sys_data(data)`

  * `MC_Succeeded`：**`accept`** を送って確定
  * `MC_Failed`：**`reject`** でやり直し
  * `MC_Completed` or `MC_Rejected`：**次のアクションへ進み** `start`
  * 全アクション終了後は `train_mc_action` 内の else で **`save`** → `on_save_profile_done` へ。

### 保存・終了

* `on_save_profile_done`
  「保存できた」ログを出し、**`unload_profile`** を実行（クリーンアップ）。

### エラー

* `on_inform_error(error_data)`
  とくに **`ERR_PROFILE_ACCESS_DENIED`** を拾ってヘッドセット切断（次回のための後始末）。

---

## 設計上のポイント／注意

* **`sys` ストリームが唯一の進行トリガ**：
  `start/accept/reject` を送るだけでは完結せず、**Cortex からの `MC_*` イベント**で状態が進む。
* **アクション順序**：
  例の通り `['neutral','push','pull']` のように **neutral を最初に**学習するのがセオリー。
* **接触品質の事前確認**（推奨）：
  学習前に `dev`/`eq` を一時購読して **接触品質（CQ）や欠損率**を満たすことを確認すると成功率が上がる。
* **ブロッキング回避**：
  `open()` が戻らないので、アプリ組込み時は **別スレッドで `start()`** を。
* **例外系**：
  プロファイルが他アプリでロード中などは `cortex.py` 側の `get_current_profile` → `load/unload` 分岐に依存。必要なら**競合時の再試行**をこの層に追加すると良い。

---

## 使い方の最小例（流れ）

```python
t = Train(client_id, client_secret)
profile_name = "my_profile"
actions = ["neutral", "push", "pull"]
# GUI等なら別スレッドで:
# threading.Thread(target=lambda: t.start(profile_name, actions), daemon=True).start()
t.start(profile_name, actions)  # CLIならそのまま
```

* 以降はイベントで自走し、`sys` イベントに従って **各アクションを順に学習→保存→アンロード** まで到達します。

---

### まとめ

* この `Train` は **メンタルコマンド学習の状態機械**を実装した薄い制御層。
* 低レベル I/O は `cortex.py` が担い、ここは **イベント→次の API 呼び出し**の接着に徹しています。
* 実運用では **品質チェック**・**ブロッキング回避**・**競合時リカバリ**を加えるとより堅牢に動きます。

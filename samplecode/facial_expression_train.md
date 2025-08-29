# `Train` スクリプト（Facial Expressions のトレーニング）コードリーディング

このファイルは、 `cortex.py`（同期WebSocketクライアント＋イベント発火）を使って、**Facial Expressions（表情検出）**の**学習プロファイル作成／更新**を自動化する小さな制御レイヤです。
中核は `Train` クラスで、**イベント駆動**（`pydispatch.Dispatcher` の `.bind()` でコールバック登録）により、**アクセス権→認可→ヘッドセット→セッション→プロファイル→sys購読→学習の開始・承認**までを順に回します。

---

## 全体像（責務）

* `Train` は **学習フローのオーケストレーション**に特化：

  1. アクセス権確認・認可・デバイス準備（`Cortex.open()` により `cortex.py` 側が実行）
  2. プロファイル作成 or ロード
  3. `sys` ストリーム購読（学習イベント受信）
  4. **actions**（例：`['neutral','surprise','smile']`）を**1つずつ** `start → (succeeded) → accept` の順で学習
  5. 失敗時は `reject` して同アクションを再トライ
  6. 全アクション学習完了で `save` → `unload` → 終了

* 低レベルの Cortex API 呼び出し（`authorize`, `createSession`, `subscribe` など）は **`cortex.py` の `Cortex` クラス**が担当。
  `Train` はそのイベント（`create_session_done`, `new_data_labels`, `new_sys_data`, …）をハンドラで受けて次の一手を出します。

---

## 主要クラスとメソッド

### `class Train`

**属性**

* `self.c`: `Cortex` のインスタンス。`debug_mode=True` で詳細ログを出す設定。
* `self.profile_name`: 学習対象のプロファイル名。
* `self.actions`: 学習させる表情アクションのリスト（順番に消化）。
* `self.action_idx`: 現在のアクションのインデックス。

**コンストラクタ**

```python
def __init__(self, app_client_id, app_client_secret, **kwargs):
    self.c = Cortex(app_client_id, app_client_secret, debug_mode=True, **kwargs)
    self.c.bind(create_session_done=self.on_create_session_done)
    self.c.bind(query_profile_done=self.on_query_profile_done)
    self.c.bind(load_unload_profile_done=self.on_load_unload_profile_done)
    self.c.bind(save_profile_done=self.on_save_profile_done)
    self.c.bind(new_data_labels=self.on_new_data_labels)
    self.c.bind(new_sys_data=self.on_new_sys_data)
    self.c.bind(inform_error=self.on_inform_error)
```

* **目的**：Cortex クライアントを用意し、**必要イベントにコールバックをバインド**。
* バインドしているイベントは、すべて `cortex.py` 側で `emit()` されるもの（セッション作成完了、プロファイル系、購読ラベル到着、sysイベント、エラーなど）。

---

### 起動と準備

#### `start(self, profile_name, actions, headsetId='')`

* **入力**：プロファイル名、学習アクション配列、（任意）対象ヘッドセットID。
* **処理**：

  * 空の `profile_name` は例外。
  * 内部状態を初期化（`self.actions`, `self.action_idx=0`）。
  * `cortex.py` 側に**希望プロファイル／ヘッドセット**を伝える（`set_wanted_profile`, `set_wanted_headset`）。
  * **`self.c.open()` を呼ぶ**（※ `cortex.py` の `open()` は内部で `run_forever`→`join()` して**戻らない**ので、“以降の進行はイベントコールバック側に任せる”設計）。

> ここがポイント：`open()` の**ブロッキング仕様**により、`start()` 呼び出しスレッドは WebSocket が閉じるまで復帰しません。GUI などで使うときは、**別スレッドで `start()` を叩く**構成が必要です（または `open()` を非ブロッキング化する改修）。

---

### サブスクリプション

#### `subscribe_data(self, streams)`

* `self.c.sub_request(streams)` を薄くラップ。
* 本スクリプトでは **`['sys']`** を購読して「学習イベント（FE\_\*）」を受け取ります（`on_load_unload_profile_done` でロード完了後に呼ばれる）。

---

### プロファイル操作

#### `load_profile(self, profile_name)`

* `setup_profile(profile_name, 'load')` を発行。
  （この関数自身は未使用。`on_query_profile_done` 内から `get_current_profile()`→`setup_profile(...,'create'|'load')` が呼ばれます）

#### `unload_profile(self, profile_name)`

* 終了時に `setup_profile(..., 'unload')` を発行。
* `on_load_unload_profile_done` 側で `is_loaded=False` を受けるとソケット `close()`。

#### `save_profile(self, profile_name)`

* `setup_profile(..., 'save')`。
* 本スクリプトでは**全アクション完了時に自動で save**（`train_fe_action` の else 分岐）と、\*\*`on_save_profile_done` での後処理（unload）\*\*に使います。

---

### 学習制御のコア

#### `train_fe_action(self, status)`

* **Facial Expressions 用トレーニング制御**の薄いラッパ：

  * 送出：`self.c.train_request(detection='facialExpression', action=<現在のアクション>, status=<与えられた状態>)`
  * `status` は `start / accept / reject / erase / reset` に対応（このスクリプトでは `start/accept/reject` のみ使用）。
  * \*\*範囲外（全アクション消化）\*\*に到達すると、**プロファイルを `save`** し、`self.action_idx=0` に戻して終了フェーズへ。

> 実際の**学習タイムライン**は `sys` イベントでドライブされます（次項）。

---

## コールバック（イベントハンドラ）

### 1) セッション・プロファイル準備

#### `on_create_session_done(...)`

* セッションができたら **`query_profile()`**。
  → 既存プロファイルの有無を確認する入口。

#### `on_query_profile_done(..., data=profile_list)`

* `profile_name` が**存在**するなら `get_current_profile()` を呼び、**現在ロード中のプロファイル名**と**誰がロードしたか**を確認（この結果は `cortex.py` で処理→必要なら load/unload へ誘導）。
* **存在しない**なら `setup_profile(profile_name, 'create')` で作成。

#### `on_load_unload_profile_done(..., isLoaded=bool)`

* `True`（ロード完了）なら：**`sys` を購読**して、**学習イベント受信体制**を整える（→ `subscribe_data(['sys'])`）。
* `False`（アンロード完了）なら：プロファイル名を消して **`c.close()`**（終了）。

#### `on_save_profile_done(...)`

* セーブ成功ログ後、**`unload_profile(...)`** を実行してクリーンアップ。

---

### 2) 学習イベントでのステートマシン

#### `on_new_data_labels(..., data=labels)`

* `SUB_REQUEST_ID` の成功応答から渡された **ストリーム列名**（`new_data_labels`）のハンドラ。
* `data['streamName']=='sys'` であれば **購読が通った合図**としてすぐ **学習開始（`train_fe_action('start')`）**。

#### `on_new_sys_data(..., data=sys_sample)`

* **学習イベント**のメインループ。`data` は `['sessionId', 'FE_Succeeded' など, ...]` 想定で、**2番目の要素**を `train_event` として取り出し：

  * `'FE_Succeeded'` → 現在のアクションの学習が成功したので **`accept`** を送る（確定）
  * `'FE_Failed'` → **`reject`** して**再トライ**（`train_fe_action` 内で同アクションへ `start` を送り直す想定）
  * `'FE_Completed'` or `'FE_Rejected'` → **次のアクションへ進む**（`self.action_idx += 1`）→ **`start`**

    * ここで**最後のアクション**を終えて `self.action_idx == len(actions)` に達すると、`train_fe_action('start')` の内部ロジックで **`save`→`action_idx=0`** へ遷移します。

> つまり、**sysイベントがトリガー**になって
> `start → (FE_Succeeded) → accept → (FE_Completed) → 次へ`
> というステートマシンが **アクション配列の最後まで**自走します。

---

### 3) エラーハンドリング

#### `on_inform_error(..., error_data)`

* 低レベルの JSON-RPC エラーは `cortex.py` が `inform_error` で上げる。
* このスクリプトでは例として **`ERR_PROFILE_ACCESS_DENIED`** を拾い、**ヘッドセット切断**を実施（次回に備える）。

---

## 実行入口 `main()`

```python
def main():
    your_app_client_id = ''
    your_app_client_secret = ''
    t = Train(your_app_client_id, your_app_client_secret)

    profile_name = ''  # 既存 or 新規名
    actions = ['neutral', 'surprise', 'smile']
    t.start(profile_name, actions)
```

* **やること**：Client ID/Secret、**profile 名**（空は不可）、**学習する表情アクション**を指定して `start()`。
* 注意：前述のとおり **`start()`→`c.open()` は戻らない**ので、CLI ならOK、GUI/他処理と併用なら**別スレッド**化推奨。

---

## 挙動の時系列（ざっくり）

```
start()
 └─ c.open()  ← 以降はイベントで進行
     └─ on_open → do_prepare_steps → has_access_right
         ├─ (OK) authorize → refresh_headset_list → query_headset → create_session
         └─ (NG) request_access → (Launcherで承認) → authorize → ...
           └─ on_create_session_done → query_profile
               ├─ 既存 → get_current_profile → (cortex.py側で load/unload 誘導)
               └─ 未存在 → setup_profile('create') → (cortex.py側で自動 load)
                 └─ on_load_unload_profile_done(isLoaded=True)
                       └─ subscribe_data(['sys'])
                           └─ on_new_data_labels(streamName=='sys') → train_fe_action('start')
                               └─ on_new_sys_data: FE_Succeeded → accept
                               └─ on_new_sys_data: FE_Completed → 次アクション start
                               └─ ... 最後のアクション完了 → save → on_save_profile_done → unload → close
```

---

## 実務Tips／改善ポイント

* **非ブロッキング起動**：`c.open()` がブロッキングなので、**別スレッドで `start()`** を起動するor `cortex.py` を改修して `join()` しないモードを用意するとアプリが作りやすいです。
* **学習タイムアウト対策**：`FE_Succeeded/Failed` が来ないケースに備えて、\*\*ウォッチドッグ（一定時間でリトライ or 中止）\*\*を入れると堅牢。
* **接触品質の事前チェック**：`eq`/`dev` を一時購読して **接触品質（CQ）や欠損率**が一定以上か確認→OKなら `sys` 学習開始、にすると成功率が上がります。
* **アクションの順序／組み合わせ**：`neutral` は基準として最初に学習しておくのがセオリー。以降の `smile`, `surprise` などは被りにくい順序に。
* **ログと保存**：`save` の前後で `exportRecord` を呼んで**学習セッションのログを CSV で出力**しておくと後検証に便利。

---

## まとめ

* このスクリプトは **Facial Expressions 学習のための“イベント駆動ステートマシン”**。
* `sys` ストリームの `FE_*` イベントを**唯一の真実**として、`start/accept/reject` を投げ分け、**全アクションを自動で学習→保存→アンロード**します。
* 低レベルの JSON-RPC/WSS 処理やセッション・購読・イベント発火は**すべて `cortex.py` 側**で完結。`Train` は**フロー制御に専念**しています。

必要なら、この `Train` を**メンタルコマンド（`detection='mentalCommand'`）版**にリファクタし、`pow/met` の品質チェックや GUI 連携を含めた“実験運用テンプレ”も出せます。

# `Record`（記録→停止→エクスポート）コードリーディング

`cortex.py` の `Cortex` クライアントを使い、**セッション開始 → 記録開始 → 所定時間待機 → 記録停止 → ポスト処理完了待ち → エクスポート → 終了**をイベント駆動で回す最小テンプレです。低レイヤの JSON-RPC/WSS は `Cortex` が担当し、本スクリプトは**手順のオーケストレーション**に徹します。

---

## 構成と責務

```python
class Record:
    self.c  # Cortex クライアント（debug_mode=True）
    # 設定：record_title, record_description
    # エクスポート：record_export_folder, record_export_data_types, record_export_format, record_export_version
    # 実行時：record_duration_s, record_id
```

### イベント購読（`__init__`）

```python
self.c.bind(
  create_session_done=self.on_create_session_done,
  create_record_done=self.on_create_record_done,
  stop_record_done=self.on_stop_record_done,
  warn_record_post_processing_done=self.on_warn_record_post_processing_done,
  export_record_done=self.on_export_record_done,
  inform_error=self.on_inform_error
)
```

`cortex.py` 側が `emit()` するイベントで**各ステップを次へ進める**設計。

---

## 実行フロー（時系列）

1. `start(record_duration_s=20, headsetId='')`

   * 記録時間を保存。任意でターゲットのヘッドセットを指定（`set_wanted_headset`）。
   * **`self.c.open()`** を実行（WebSocket 開始）。

     > 注意：`open()` は内部で `run_forever()`→`join()` するため**ブロッキング**（CLIならOK。GUIや並行処理では**別スレッドで `start()`** 推奨）。

2. `on_create_session_done`

   * **記録開始**：`create_record(self.record_title, description=...)` を発行。

3. `on_create_record_done(data)`

   * `record_id` を保存しログ出力。
   * **所定時間の録音**：`wait(self.record_duration_s)` が 1 秒刻みで進捗を出しつつ sleep。
   * **記録停止**：`stop_record()` を発行。

4. `on_stop_record_done(data)`

   * 停止のメタ情報（開始/終了時刻等）をログに出す。
   * 直ちにエクスポートはせず、**ポスト処理完了の警告**を待つ。

5. `on_warn_record_post_processing_done(record_id)`

   * Cortex 側の **「ポストプロセス完了」通知（warning 30）**。
   * **エクスポート**：`export_record(folder, stream_types, format, [record_id], version)` を実行。

     * 例：`stream_types=['EEG','MOTION','PM','BP']`, `format='CSV'`, `version='V2'`

6. `on_export_record_done(data)`

   * 成功した `recordIds` を表示し、**`self.c.close()`** でソケットを閉じて終了。

7. `on_inform_error(error_data)`

   * 失敗内容をそのまま出力（必要に応じてリトライ戦略を足す余地あり）。

---

## パブリック API ラッパ

* `create_record(title, **kwargs)` → `Cortex.create_record`（空タイトルの場合は `cortex.py` 側で警告＆close）
* `stop_record()` → `Cortex.stop_record`
* `export_record(folder, stream_types, format, record_ids, version, **kwargs)` → `Cortex.export_record`
* `wait(record_duration_s)` → シンプルな sleep ループ（進捗ログ付き）

---

## データとエクスポートの扱い

* **ストリーム選択**は `record_export_data_types` で指定（例：`['EEG','MOTION','PM','BP']`）。

  * `EEG`（生波形）, `MOTION`（IMU）, `PM`（Performance Metrics = `met`）, `BP`（Band Power = `pow`）など。
* **無料プラン注意**：Registered Developer（無料）では**生EEG `EEG` は利用できません**。CSVに含めたいデータは、**そのプランで許可されているストリーム**に合わせて選んでください（`MOTION/PM/BP` は可）。

---

## よくある落とし穴と対策

* **ブロッキング**：`open()` が戻らない → **別スレッド起動**にするか、`cortex.py` を非ブロッキングに改修。
* **ポスト処理待ち**：記録停止直後にエクスポートすると失敗しやすい → **warning 30 を待ってからエクスポート**（本コードはOK）。
* **出力先フォルダ権限**：`record_export_folder` は**書き込み可能な絶対パス**に。
* **ヘッドセット接続**：USBは充電用。**ドングル or BT** で接続し、Launcher で状態が **connected** になっているか確認。
* **時間管理**：`wait()` は**スレッドをブロック**するシンプル実装。GUIや他処理と併用するなら**別スレッド**で回すか、タイマーベースに変更を。

---

## 使い方（`main()` での設定ポイント）

```python
r = Record(client_id, client_secret)

# 記録メタ
r.record_title = 'my-session'          # 必須
r.record_description = 'demo run'       # 任意

# エクスポート設定
r.record_export_folder = '/path/to/out' # 書込可能な場所
r.record_export_data_types = ['MOTION','PM','BP']  # 無料なら EEG は外す
r.record_export_format = 'CSV'
r.record_export_version = 'V2'

r.start(record_duration_s=10)           # 10秒記録
```

---

## 伸びしろ（実運用の改善案）

* **品質ゲート**：開始前に `dev/eq` を一時購読して**接触品質や欠損率**をチェック→基準未満ならアラート。
* **マーカー併用**：刺激同期が必要なら、`Marker` のロジック（`injectMarker`）を統合し、**同一記録にマーカー列**を残す。
* **非同期設計**：`wait()` を `threading.Timer` や `asyncio` に置き換え、UI応答性を確保。
* **失敗時リトライ**：`inform_error` で `-32019`（セッション上限）などを検知して**古いセッションを閉じて再試行**する処理を追加。

---

### まとめ

この `Record` は、\*\*「決められた長さだけ記録して正しいタイミングでエクスポートする」\*\*ための最小・素直なテンプレです。
プロダクトに組み込む際は、**ブロッキング回避・品質チェック・エラーハンドリング**を加えると堅牢に運用できます。

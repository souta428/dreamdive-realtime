# dreamdive-realtime
睡眠ステージをリアルタイムで監視するシステムです。

## 環境変数の設定（重要）
このリポジトリでは Emotiv Cortex API の `CLIENT_ID` と `CLIENT_SECRET` をハードコーディングせず、環境変数から読み込みます。ルートに `.env` を作成し、以下のように設定してください。

```
CLIENT_ID=your_client_id
CLIENT_SECRET=your_client_secret
# 任意（使用する場合のみ）
HEADSET_ID=your_headset_id
```

`.env.example` を参考にしてください。`.env` は `.gitignore` に含まれているため、誤ってコミットされません。

## プロジェクト構成メモ
- `prototype.py`: ダッシュボードで各種 API から取得できるデータを可視化
- `samplecode/`: 公式サンプルコードと解説
- `sleepstage.md`: 睡眠ステージ分析の手法説明

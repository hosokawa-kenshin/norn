# NORN

音声入力を主な操作手段とするローカルWebアプリです。音声はローカルのfaster-whisperで文字起こしし、送信前に認識結果を確認できます。Style-Bert-VITS2による音声合成はまだ接続していません。

## 必要環境

- Python 3.12推奨（3.10以上）
- Node.js 22以上
- WSL上で開発する場合は、WSLディストリビューション内のLinux版Node.jsを使用してください。Windows側のnpmはWSLのUNC作業パスを正しく扱えません。
- OpenAI互換のLLM接続先。ローカルのOllamaも設定できます

## 起動

NORN直下でバックエンド環境を作成します。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env
```

フロントエンドの依存関係も初回だけインストールします。

```bash
cd frontend
npm install
cd ..
```

`.env`に`LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL`を設定します。APIキー不要のローカルLLMでは`LLM_API_KEY`を空にします。Ollamaの例:

```dotenv
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_API_KEY=
LLM_MODEL=qwen2.5:7b
```

タスクの時間割当は既定で平日09:00〜18:00を使います。`.env`の`NORN_PLANNING_DAY_START`、`NORN_PLANNING_DAY_END`、`NORN_PLANNING_WORKDAYS`で変更できます。曜日は月曜を`0`、日曜を`6`としてカンマ区切りで指定します。見積時間のあるタスクはGeminiが期限まで（期限がなければ次の5営業日以内）の空き枠を検索して自動で割り当てます。予定・他タスクと重なる枠や営業時間外は登録されません。

`.env`の接続先とAPIキーを設定したら、NORN直下で両方のサーバーを起動します。

```bash
bash start.sh
```

スクリプトが表示するfrontend URLを開き、マイクの使用を許可してください。8000番・5173番ポートが使用中の場合は、それぞれ次の空きポートを選びます。初回の文字起こし時にfaster-whisperの`small`モデル（約500MB）がダウンロードされます。CPU推論が既定です。対応するCUDA環境がある場合は`.env`で`STT_DEVICE=cuda`と`STT_COMPUTE_TYPE=float16`を指定できます。

## データとプライバシー

- 録音データは文字起こしの一時処理後に削除し、保存しません。
- 会話履歴は`data/norn.sqlite3`に保存します。
- 長期記憶は会話履歴と分けてSQLiteに保存します。新しいチャット入力があり、前回の抽出から1時間以上経っている場合に、全会話共通で未処理分から最大3件を抽出します。時間になっただけでは動かず、バックグラウンド実行もしません。質問・確認文や「前に話した？」という想起依頼は、事実として自動保存しません。「覚えて」と明示された内容はすぐ保存でき、訂正や忘却も会話から依頼できます。抽出には別のLLMリクエストを使います。
- 記憶の言い換え検索にはローカルの多言語埋め込みモデルを使います。初回の利用時にモデル（約250MB）を取得しますが、質問文・記憶文のベクトル化と検索はローカルで行い、埋め込みサービスへ送信しません。初期しきい値はcosine類似度`0.72`です。これは候補検索用の基準で、返却候補はGeminiが質問の人物・行動・場所・時期・対象と照合してから回答します。モデルが利用できない場合は語句検索へフォールバックします。パスワード、APIキー、決済情報は保存層で拒否し、正確な住所など慎重な情報は抽出対象から除外する方針です。
- 音声認識はローカルで実行します。会話テキストは設定したLLM接続先へ送信され、記憶候補の抽出にも別のLLMリクエストを使用します。
- APIキー、DB、モデルファイルはGit管理対象外です。
- 開発サーバーとAPIはループバックアドレスにのみバインドします。

## テスト

```bash
source .venv/bin/activate
pytest backend/test_app.py
python backend/memory_eval.py
cd frontend
npm run build
```
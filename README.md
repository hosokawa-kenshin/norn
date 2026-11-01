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

`.env`の接続先とAPIキーを設定したら、NORN直下で両方のサーバーを起動します。

```bash
bash start.sh
```

スクリプトが表示するfrontend URLを開き、マイクの使用を許可してください。8000番・5173番ポートが使用中の場合は、それぞれ次の空きポートを選びます。初回の文字起こし時にfaster-whisperの`small`モデル（約500MB）がダウンロードされます。CPU推論が既定です。対応するCUDA環境がある場合は`.env`で`STT_DEVICE=cuda`と`STT_COMPUTE_TYPE=float16`を指定できます。

## データとプライバシー

- 録音データは文字起こしの一時処理後に削除し、保存しません。
- 会話履歴は`data/norn.sqlite3`に保存します。
- 音声認識はローカルで実行します。会話テキストは設定したLLM接続先へ送信されます。
- APIキー、DB、モデルファイルはGit管理対象外です。
- 開発サーバーとAPIはループバックアドレスにのみバインドします。

## テスト

```bash
source .venv/bin/activate
pytest backend/test_app.py
cd frontend
npm run build
```
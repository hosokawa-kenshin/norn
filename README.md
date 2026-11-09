# NORN

## NORNとは

NORNは、音声による自然な対話を中心に設計された、個人向けAIアシスタントです。
ユーザーの呼びかけに応じて会話するだけでなく、タスク・スケジュール・目標・長期記憶を統合し、日常生活や作業をサポートします。
音声認識・音声合成をローカル環境で動作させ、ハンズフリーでの会話やWindowsデスクトップへの常駐にも対応しています。
単なる音声操作ツールではなく、日常的に会話しながら使えるパーソナルAIを目指しています。

## 主な機能

### 音声対話
- ローカル音声認識（faster-whisper）
- ローカル音声合成（Style-Bert-VITS2）
- AIの返答音声の自動再生
- ウェイクワードによるハンズフリー起動
- 音声プロフィールによる呼びかけ検出
- 音声・テキスト両方での会話

### AIアシスタント
- 会話内容に基づくタスクの自動登録
- タスク・プロジェクト・長期目標の管理
- 会話内容の長期記憶と活用
- スケジュールの確認・調整
- GeminiなどのLLMとの連携

### Google Calendar連携
- Google OAuth 2.0によるアカウント連携
- 同期先カレンダーの選択
- Google Calendarとの双方向同期
- 5分ごとの差分同期
- 繰り返し予定への対応
- 予定の競合検出・解決

### Windowsデスクトップ
- Windowsデスクトップアプリ
- システムトレイへの常駐
- ログイン時の自動起動
- バックグラウンドでのハンズフリー待機

## 必要環境

- Python 3.12推奨（3.10以上）
- Node.js 22以上
- OpenAI互換APIに対応したLLM接続先
  - Geminiなどの外部API
  - OllamaなどのローカルLLM

## 導入方法

### 1. リポジトリを取得

```bash
git clone git@github.com:hosokawa-kenshin/norn.git
cd norn
```

### 2. バックエンドのセットアップ

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env
```

### 3. フロントエンドのセットアップ

```bash
cd frontend
npm install
cd ..
```

### 4. LLMの設定

`.env`を編集します。

```dotenv
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=your-api-key
LLM_MODEL=your-model-name
```

利用するLLMのAPI仕様に合わせて接続先を設定してください。

### 5. 起動

```bash
bash start.sh
```

表示されたURLをブラウザで開き、マイクの使用を許可してください。
初回の音声認識時にはfaster-whisperのモデルがダウンロードされます。

## 音声合成（TTS）の設定

NORNはStyle-Bert-VITS2を利用した日本語音声合成に対応しています。
TTSは任意機能です。無効の場合でもテキストによる会話や音声入力は利用できます。

### 1. 音声モデルの準備

音声モデルは `.safetensors` または `.pth` 形式に対応しています。
モデルに対応する `config.json` と `style_vectors.npy` も用意してください。

### 2. `.env`の設定

```dotenv
NORN_TTS_ENABLED=1

NORN_TTS_MODEL=/path/to/voice/model.safetensors

NORN_TTS_DEVICE=auto
NORN_TTS_SPEAKER_ID=0
NORN_TTS_STYLE=Neutral
```

主な設定項目：

| 変数 | 説明 |
|---|---|
| `NORN_TTS_ENABLED` | TTSの有効・無効 |
| `NORN_TTS_MODEL` | 使用する音声モデル |
| `NORN_TTS_DEVICE` | 推論デバイス（auto / cpu / cuda） |
| `NORN_TTS_SPEAKER_ID` | 使用する話者ID |
| `NORN_TTS_STYLE` | 使用する音声スタイル |

必要に応じて、Python実行環境やBERT、設定ファイルなどのパスも個別に指定できます。詳細は `.env.example` を参照してください。

### 3. 起動

設定を保存してNORNを再起動します。
モデルの読み込みとウォームアップが完了すると、AIの返答が音声で自動再生されます。
音声合成に失敗した場合もテキストでの会話は継続できます。

## Google Calendarの初期設定

1. [Google Cloud Console](https://console.cloud.google.com/)でプロジェクトを作成
2. Google Calendar APIを有効化
3. OAuth同意画面を設定
4. Desktop app形式のOAuthクライアントを作成
5. `.env`に認証情報を設定

```dotenv
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
```

6. NORNの設定画面からGoogle Calendarに接続
7. 同期先のカレンダーを選択

## Windowsデスクトップ版

Windows上で常駐アプリとして利用する場合は、WSL2上のNORNバックエンドとWindowsデスクトップアプリを組み合わせて動作させます。

初回セットアップはWindowsのPowerShellで実行します。

```powershell
pwsh -NoProfile -ExecutionPolicy Bypass -File "\\wsl.localhost\Ubuntu-22.04\home\hosokawa\voice_project\norn\desktop\install.ps1"
```

セットアップ後はスタートメニューから起動できます。
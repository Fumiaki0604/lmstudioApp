# lmstudio App

ローカルLLM（LM Studio）と OpenClaw Gateway を使ったキャラクターチャット・コンテンツ生成アプリ。

## 機能概要

| タブ | 内容 |
|------|------|
| 💬 Chat（相棒） | 1対1 / 3人 / 4人のキャラクターチャット。TTS読み上げ対応 |
| 🤖 自律会話 | キャラ同士がユーザー介在なしで自由に会話 |
| 📻 ニュースラジオ | RSSニュースをキャラクターが紹介・会話 |
| 📝 note記事 | A2Aパイプラインによるnote.com記事自動生成 |
| ⚙️ 設定 | TTS設定・システムプロンプト・note Cookie等 |

## 主な仕様

### Chat（相棒）
- **キャラクター**: VOICEVOX キャラ + Noah（OpenClaw Gateway経由、ChatGPT）+ Hermes（Groq経由）
- **TTS**: VOICEVOX（port 50021）/ AivisSpeech（port 10101）/ クラウドTTS
- **MOOD連動スタイル切替**: LLMが `[MOOD:happy]` などを出力すると、対応する音声スタイルに自動切替
  - `happy` → あまあま、`angry` → ツンツン、`sad` → なみだめ、`calm` → おちつき 等
- **Noah専用機能**: OpenClaw ワークスペース記憶（IDENTITY/RELATIONSHIP/KNOWLEDGE/OBSERVATIONS.md）をシステムプロンプトに自動注入

### 自律会話
- **フリースペース**: 登録キャラ全員が参加し、ランダムなタイミングで発言する自由雑談
- **Soulシステム**: `~/.lmstudio_assistant/souls/{キャラ名}.md` に各キャラの自己認識・記憶・他キャラへの印象を保存
  - 20ターンに1回、LLMがsoul.mdを自動更新
  - systemプロンプトに動的注入
- **親密度スコア**: soul.md内の `## 親密度スコア` セクション（0〜100）でキャラ間の関係性を管理。発言者本人には非公開
- **X（Twitter）連携**: VoiceVoxキャラの公式Xアカウントから最近の投稿を取得し、soul.mdに注入（毎週土曜 5:00 自動実行）
- **LM Studio競合対策**: バックグラウンドタスクはセマフォで直列化。note生成中はバックグラウンドをスキップ

### note記事（A2Aパイプライン）
- **役割**: 調査役 / 執筆役 / 編集役 / アドバイザー（Noah）
- **HermesAgent連携**: `hermes -z` サブプロセスで学習・改善ループ（Hermes 4.3 36B使用）
- **投稿**: note.com 非公式API経由でドラフト保存（cookie認証）

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install streamlit requests trafilatura twikit
```

### 必要なサービス
- **LM Studio**: `http://localhost:1234/v1`（ローカルモデル用）
- **VOICEVOX**: `http://localhost:50021`
- **AivisSpeech**: `http://localhost:10101`（Noah用）
- **OpenClaw Gateway**: `http://127.0.0.1:18789/v1`（Noah/ChatGPT用）

```bash
streamlit run app.py
```

### X（Twitter）連携セットアップ
```bash
python twitter_setup.py  # ブラウザのcookieを保存
python twitter_fetch.py  # 手動実行テスト
```
cookie保存先: `~/.lmstudio_assistant/twitter_cookies.json`

## ファイル構成

- `app.py` - メインアプリ（全機能）
- `chat.py` - LLM呼び出し・LM Studioセマフォ管理
- `speakers.py` - キャラクター管理・Soul読み書き
- `tts.py` - TTS合成
- `news.py` - RSSニュース取得
- `note_api.py` - note.com API連携
- `twitter_fetch.py` - X公式アカウントからツイート取得・soul.md更新
- `twitter_setup.py` - Xのcookie認証セットアップ
- `noah_config.json` - Noah のキャラクター設定
- `speakers_all.json` - VOICEVOX/AivisSpeech 話者データキャッシュ
- `icons/` - キャラクターアイコン
- `~/.lmstudio_assistant/souls/` - キャラクターSoulファイル
- `~/.lmstudio_assistant/prompts.json` - システムプロンプト保存先

## 作者

Fumiaki

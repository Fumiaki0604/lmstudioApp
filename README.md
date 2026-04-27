# lmstudio App

ローカルLLM（LM Studio）と OpenClaw Gateway を使ったキャラクターチャット・コンテンツ生成アプリ。

## 機能概要

| タブ | 内容 |
|------|------|
| 💬 Chat（相棒） | 1対1 / 3人 / 4人のキャラクターチャット。TTS読み上げ対応 |
| 📻 ニュースラジオ | RSSニュースをキャラクターが紹介・会話 |
| 📝 note記事 | A2Aパイプラインによるnote.com記事自動生成 |
| ⚙️ 設定 | TTS設定・システムプロンプト・note Cookie等 |

## 主な仕様

### Chat（相棒）
- **キャラクター**: VOICEVOX キャラ + Noah（OpenClaw Gateway経由、ChatGPT）
- **TTS**: VOICEVOX（port 50021）/ AivisSpeech（port 10101）/ クラウドTTS
- **MOOD連動スタイル切替**: LLMが `[MOOD:happy]` などを出力すると、対応する音声スタイルに自動切替
  - `happy` → あまあま、`angry` → ツンツン、`sad` → なみだめ、`calm` → おちつき 等
- **Noah専用機能**: OpenClaw ワークスペース記憶（IDENTITY/RELATIONSHIP/KNOWLEDGE/OBSERVATIONS.md）をシステムプロンプトに自動注入

### note記事（A2Aパイプライン）
- **役割**: 調査役 / 執筆役 / 編集役 / アドバイザー（Noah）
- **HermesAgent連携**: `hermes -z` サブプロセスで学習・改善ループ（Hermes 4.3 36B使用）
- **投稿**: note.com 非公式API経由でドラフト保存（cookie認証）

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install streamlit requests trafilatura
```

### 必要なサービス
- **LM Studio**: `http://localhost:1234/v1`（ローカルモデル用）
- **VOICEVOX**: `http://localhost:50021`
- **AivisSpeech**: `http://localhost:10101`（Noah用）
- **OpenClaw Gateway**: `http://127.0.0.1:18789/v1`（Noah/ChatGPT用）

```bash
streamlit run app.py
```

## ファイル構成

- `app.py` - メインアプリ（全機能）
- `noah_config.json` - Noah のキャラクター設定
- `speakers_all.json` - VOICEVOX/AivisSpeech 話者データキャッシュ
- `icons/` - キャラクターアイコン
- `~/.lmstudio_assistant/prompts.json` - システムプロンプト保存先

## 作者

Fumiaki

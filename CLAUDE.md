# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ローカルLLM（LM Studio）を使ったチャット＆URL要約アプリ。Streamlit製。VOICEVOX音声合成（TTS Quest API経由）対応。

## Commands

```bash
# アプリ起動
source .venv/bin/activate && streamlit run app.py

# LM Studio接続テスト
source .venv/bin/activate && python smoke_test_lmstudio.py

# CLI版URL要約
source .venv/bin/activate && python url_to_summary_lmstudio.py <URL>
```

## Architecture

### app.py (メインアプリ)
単一ファイル構成。セクションは以下の順:

1. **Constants** - デフォルトプロンプト、API URL等
2. **Persistence** - `~/.lmstudio_assistant/`にJSON保存（prompts.json, settings.json）
3. **VOICEVOX TTS** - TTS Quest API呼び出し、長文は200文字で分割
4. **LM Studio helpers** - OpenAI互換API呼び出し、エンベディングモデル除外
5. **Web text extraction** - trafilatura使用
6. **Streamlit UI** - 3タブ構成（Chat / URL要約 / 設定）

### 外部依存
- **LM Studio**: `http://localhost:1234/v1`（OpenAI互換API）
- **TTS Quest API**: `https://api.tts.quest/v3/voicevox/synthesis`（非同期、ポーリング必要）
- **speakers_all.json**: VOICEVOX話者リスト（ローカルファイル）

## Key Implementation Details

- Python 3.9互換（`Optional[T]`使用、`T | None`不可）
- TTS APIは非同期生成のため`audioStatusUrl`でポーリングして`isAudioReady`を待つ
- 長文TTSは句読点で分割→複数MP3を連結
- LM Studioのモデル一覧から`text-embedding-*`等は除外して表示

## Pending Tasks

### 音声キャラクター連動プロンプト機能
**目的**: 話者（VOICEVOX）を選択すると、そのキャラクターの性格に合わせたプロンプト本文に自動変更する

**実装方針**:
1. キャラクター情報JSONを用意（ユーザーが作成予定）
   - 例: `speakers_with_profiles.json` または `speakers_all.json`を拡張
   - 構造案: 各speakerに`personality`や`prompt_template`フィールドを追加
2. `get_speaker_options()`を拡張してプロンプト情報も返す
3. 話者選択時にプロンプトを自動適用（または「キャラ連動」チェックボックスで切替）
4. 既存の「相棒プロンプト」機能との共存を検討（上書き or マージ）

**関連ファイル**:
- `app.py`: `get_speaker_options()`, `current_buddy_prompt()`, サイドバーの話者選択部分
- `speakers_all.json`: 現在は話者ID・名前・スタイルのみ。性格情報を追加予定

**待ち**: ユーザーがキャラクター性格入りJSONを準備

### Tailscale遠隔アクセス設定
**目的**: 外出先からWake on LAN + Tailscale経由でLM Studioを利用

**手順**:
1. Tailscaleインストール（Mac + 外出先端末）
2. Macの「ネットワークアクセスによるスリープ解除」ON
3. WoLアプリ設定
4. LM Studioをログイン項目に追加（自動起動）
5. LM StudioのHostを`0.0.0.0`に変更

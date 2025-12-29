# LM Studio App

LM Studioを使ったローカルLLMによるURL要約アプリケーション

## 概要

このアプリケーションは、LM Studioで動作するローカルLLMを使用して、WebページのURLから本文を抽出し、要約を生成します。

## 機能

- **URL要約**: WebページのURLを入力すると、本文を抽出して要約を生成
- **ローカルLLM**: LM Studioを使用して、プライバシーを保ちながら処理
- **カスタマイズ可能**: 入力文字数、出力トークン数、Temperatureなどを調整可能
- **リアルタイム接続確認**: LM Studioとの接続状態を常時表示

## ファイル構成

- `app.py` - Streamlitベースのメインアプリケーション
- `url_to_summary_lmstudio.py` - URL要約のスタンドアロン版
- `smoke_test_lmstudio.py` - LM Studio接続テスト用スクリプト
- `init/` - 初期化用ディレクトリ

## セットアップ

### 1. 依存パッケージのインストール

```bash
# 仮想環境を作成（推奨）
python -m venv .venv
source .venv/bin/activate  # Windowsの場合: .venv\Scripts\activate

# 依存パッケージをインストール
pip install streamlit requests trafilatura
```

### 2. LM Studioのセットアップ

1. [LM Studio](https://lmstudio.ai/)をダウンロード・インストール
2. お好みのモデルをダウンロード（例: `openai/gpt-oss-20b`）
3. モデルをLoadし、Local ServerをRunningに設定
4. デフォルトのポート（`http://localhost:1234/v1`）を確認

## 使い方

### メインアプリケーション（Streamlit版）

```bash
streamlit run app.py
```

ブラウザで開いたら：

1. サイドバーでLM Studioとの接続を確認
2. モデルを選択
3. 生成設定（入力文字数、出力トークン数、Temperature）を調整
4. 要約したいURLを入力
5. 「要約する」ボタンをクリック

### スタンドアロン版

```bash
python url_to_summary_lmstudio.py
```

### 接続テスト

```bash
python smoke_test_lmstudio.py
```

## 設定パラメータ

### 入力の最大文字数
- **1000-2500**: 高速だが要点中心
- **2500-4000**: バランスが良く普段使いに最適
- **4000-6000**: 文脈をより詳細に把握
- **6000-12000**: 最も詳細だが処理時間が長い

### 出力トークン上限
- **100-400**: 超短文（結論のみ）
- **400-800**: 標準（読みやすい要約）
- **800-1200**: 丁寧な要約
- **1200-2000**: 詳細な要約

### Temperature
- **0.0-0.2**: 堅め・事実寄り
- **0.2-0.6**: 自然でバランスが良い
- **0.6-1.0**: 表現が豊か
- **1.0-1.5**: 創造的だが不確実性が増す

## トラブルシューティング

### 接続エラー
- LM Studioでモデルがロードされているか確認
- Local Serverが起動しているか確認
- ポート番号が正しいか確認（デフォルト: 1234）

### 本文抽出失敗
- JavaScriptでレンダリングされるページの場合、抽出できない可能性があります
- その場合はPlaywright版への移行が必要です

## ライセンス

MIT License

## 作者

Fumiaki

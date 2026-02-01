#!/bin/bash
# 相棒LLMアプリ自動起動スクリプト

# LM Studioを先に起動（まだ起動していなければ）
if ! pgrep -x "LM Studio" > /dev/null; then
    open -a "LM Studio"
    sleep 5  # LM Studioの起動待ち
fi

cd /Users/apple/lmstudio
source .venv/bin/activate
streamlit run app.py --server.headless true

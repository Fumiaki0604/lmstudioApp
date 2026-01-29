#!/bin/bash
# 相棒LLMアプリ自動起動スクリプト

cd /Users/apple/lmstudio
source .venv/bin/activate
streamlit run app.py --server.headless true

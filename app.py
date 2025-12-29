import time
from datetime import datetime

import requests
import trafilatura
import streamlit as st

# -----------------------------
# Constants / Utilities
# -----------------------------
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)

def lmstudio_models(base_url: str, timeout: int = 3):
    """LM Studioが起動しているか確認し、モデル一覧を返す。"""
    r = requests.get(base_url.rstrip("/") + "/models", timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return [m.get("id") for m in data.get("data", []) if m.get("id")]

def fetch_html(url: str, timeout: int = 20) -> str:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": DEFAULT_UA})
    r.raise_for_status()
    return r.text

def extract_main_text(html: str) -> str:
    text = trafilatura.extract(
        html,
        output_format="txt",     # ← trafilaturaの仕様に合わせて txt
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    return (text or "").strip()

def build_prompt(url: str, text: str, max_chars: int) -> str:
    # 長文対策：冒頭70% + 末尾30%（情報の偏りを少し減らす）
    if len(text) <= max_chars:
        clipped = text
    else:
        head = text[: int(max_chars * 0.7)]
        tail = text[-int(max_chars * 0.3):]
        clipped = head + "\n\n...(中略)...\n\n" + tail

    return f"""次のWebページ本文を要約してください。

制約:
- 重要ポイントを箇条書き（5〜10個）
- 数値・固有名詞・結論は落とさない
- 可能なら「意思決定の注意点」も1〜3個

URL: {url}

本文:
\"\"\"\n{clipped}\n\"\"\"
"""

def call_lmstudio_chat(
    base_url: str,
    model: str,
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 800,
    timeout: int = 180,
) -> str:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "あなたはプロの要約者です。日本語で簡潔に要約し、重要ポイントと注意点を整理してください。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    r = requests.post(endpoint, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"]

# -----------------------------
# Dynamic UI helper text
# -----------------------------
def label_max_chars(n: int) -> str:
    if n <= 2500:
        return "⚡ かなり高速（要点中心）。長い記事だと抜けが出やすい。"
    if n <= 4000:
        return "🚴 高速寄り（だいたい外さない）。普段使いにちょうど良い。"
    if n <= 6000:
        return "⚖️ バランス（文脈の取りこぼしが減る）。少し重くなる。"
    if n <= 9000:
        return "🧠 高精度（背景まで拾いやすい）。⏳遅くなりやすい＆上限注意。"
    return "🐢 特盛（詳細まで粘る）。⏳時間かかる＋コンテキスト超えリスク高め。"

def label_max_tokens(t: int) -> str:
    if t <= 400:
        return "🧾 超短文（結論だけ）。⚡最速。"
    if t <= 800:
        return "📝 標準（読みやすく要点が揃う）。速度も安定。"
    if t <= 1200:
        return "📌 丁寧（補足も入る）。⏳少し遅くなる。"
    return "📚 詳細（抜けを減らす）。⏳遅くなりやすい。"

def label_temperature(x: float) -> str:
    if x <= 0.2:
        return "🧊 かなり堅め（ブレにくい／事実寄り）。"
    if x <= 0.6:
        return "🙂 ちょうど良い（自然／安定）。"
    if x <= 1.0:
        return "🎨 表現豊か（言い回しが増える／少しブレやすい）。"
    return "🎲 揺らぎ大（発想は出るが誤差も増えがち）。"

def speed_meter(max_chars: int, max_tokens: int) -> tuple[int, str]:
    """
    “体感用”の超ざっくり速度メーター。
    厳密ではなく「待ち時間の不安」を減らす目的。
    """
    score = 100
    score -= int((max_chars - 1000) / 200)   # 文字数が増えるほど重い
    score -= int((max_tokens - 100) / 30)    # 出力が増えるほど重い
    score = max(5, min(100, score))
    label = "速い" if score >= 70 else "ふつう" if score >= 40 else "遅い"
    return score, label

# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="URL要約（LM Studio / ローカルLLM）", layout="centered")
st.title("URL要約（LM Studio / ローカルLLM）")

# session state for URL input
if "url" not in st.session_state:
    st.session_state["url"] = ""

with st.sidebar:
    st.header("接続設定")
    base_url = st.text_input("LM Studio Base URL", value="http://localhost:1234/v1")

    if st.button("🔄 接続を再確認"):
        st.rerun()

    # --- 接続チェック（常時表示） ---
    models = []
    lm_ok = False
    err = None
    t0 = time.time()
    try:
        models = lmstudio_models(base_url, timeout=3)
        lm_ok = True
    except Exception as e:
        err = e
    elapsed_ms = int((time.time() - t0) * 1000)
    checked_at = datetime.now().strftime("%H:%M:%S")

    if lm_ok:
        st.success(f"🟢 接続中（{checked_at} / {elapsed_ms}ms）")
        st.caption(f"検出モデル数: {len(models)}")
        if models:
            st.caption(f"例: {models[0]}")
    else:
        st.error(f"🔴 未接続（{checked_at} / {elapsed_ms}ms）")
        st.caption("LM StudioでモデルをLoadし、Local ServerをRunningにしてください。")
        st.caption(f"詳細: {err}")

    st.divider()

    st.header("生成設定")

    max_chars = st.slider("入力の最大文字数", min_value=1000, max_value=12000, value=3500, step=500)
    st.caption(f"入力: {label_max_chars(max_chars)}")

    max_tokens = st.slider("出力トークン上限", min_value=100, max_value=2000, value=800, step=50)
    st.caption(f"出力: {label_max_tokens(max_tokens)}")

    temperature = st.slider("Temperature", min_value=0.0, max_value=1.5, value=0.2, step=0.1)
    st.caption(f"温度: {label_temperature(temperature)}")

    score, speed_label = speed_meter(max_chars, max_tokens)
    st.progress(score)
    st.caption(f"体感速度の目安: **{speed_label}**（ざっくり）")

# 未接続ならメイン画面も止める（UX的に迷わせない）
if not lm_ok:
    st.stop()

# モデル選択（接続できている前提）
default_model = "openai/gpt-oss-20b" if "openai/gpt-oss-20b" in models else (models[0] if models else "")
model = st.selectbox("使用モデル", options=models, index=models.index(default_model) if default_model in models else 0)

url = st.text_input("要約したいURLを入力", key="url", placeholder="https://...")

col1, col2 = st.columns([1, 1])
with col1:
    run = st.button("要約する", type="primary")
with col2:
    if st.button("クリア"):
        st.session_state["url"] = ""
        st.rerun()

if run:
    if not url.strip():
        st.warning("URLを入力してください。")
        st.stop()

    # 進捗を段階表示（体感速度UP）
    with st.status("処理中...", expanded=True) as status:
        try:
            status.update(label="🌐 ページ取得中...", state="running")
            html = fetch_html(url.strip(), timeout=20)

            status.update(label="✂️ 本文抽出中...", state="running")
            text = extract_main_text(html)
            if not text:
                status.update(label="本文抽出に失敗", state="error")
                st.error("本文抽出に失敗しました（JS描画/ブロック/本文なしの可能性）。Playwright版が必要かもしれません。")
                st.stop()

            status.update(label="🧠 要約生成中...", state="running")
            prompt = build_prompt(url.strip(), text, max_chars=max_chars)
            summary = call_lmstudio_chat(
                base_url=base_url,
                model=model,
                prompt=prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=180,
            )

            status.update(label="✅ 完了", state="complete", expanded=False)

        except Exception as e:
            status.update(label="❌ エラー", state="error", expanded=True)
            st.exception(e)
            st.stop()

    st.subheader("要約結果")
    st.markdown(summary)

    with st.expander("抽出した本文（先頭）を見る"):
        st.text(text[:2000])
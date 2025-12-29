import json
import time
from datetime import datetime
from pathlib import Path

import requests
import trafilatura
import streamlit as st

# =============================
# Constants
# =============================
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)

DEFAULT_BUDDY_PROMPT = """あなたはユーザーの「助手兼相棒」です。
口調: フレンドリーで軽快。ただし馴れ馴れしすぎず、敬語とタメ口の中間。
方針:
- 結論→理由→次の一手、の順で話す。
- 事実と推測を分け、曖昧な点は正直に「不確か」と言う。
- ユーザーが“次に動ける”形で返す。
- 無駄に長くしない。読みやすく、実務向きに。
"""

SUMMARY_ADDON = """追加ルール（URL要約）:
- 重要ポイントを箇条書き（5〜10）
- 数値・固有名詞・結論は落とさない
- 最後に「意思決定の注意点」を1〜3個
"""

STORE_DIR = Path.home() / ".lmstudio_assistant"
PROMPTS_FILE = STORE_DIR / "prompts.json"


# =============================
# Persistence
# =============================
def _default_store():
    return {
        "active": "default",
        "prompts": {
            "default": DEFAULT_BUDDY_PROMPT,
        },
    }


def load_store() -> dict:
    try:
        if PROMPTS_FILE.exists():
            data = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
            if "prompts" not in data or not isinstance(data["prompts"], dict):
                return _default_store()
            if "active" not in data or data["active"] not in data["prompts"]:
                data["active"] = next(iter(data["prompts"].keys()), "default")
            return data
    except Exception:
        pass
    return _default_store()


def save_store(store: dict) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def current_buddy_prompt() -> str:
    store = st.session_state["prompt_store"]
    active = store.get("active", "default")
    prompts = store.get("prompts", {})
    return (prompts.get(active) or DEFAULT_BUDDY_PROMPT).strip()


# =============================
# LM Studio helpers
# =============================
def lmstudio_models(base_url: str, timeout: int = 3):
    r = requests.get(base_url.rstrip("/") + "/models", timeout=timeout)
    r.raise_for_status()
    return [m["id"] for m in r.json().get("data", [])]


def call_lmstudio_chat_messages(
    base_url: str,
    model: str,
    messages: list,
    temperature: float,
    max_tokens: int,
    timeout: int,
):
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    r = requests.post(endpoint, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


# =============================
# Web text extraction
# =============================
def fetch_html(url: str, timeout: int = 20) -> str:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": DEFAULT_UA})
    r.raise_for_status()
    return r.text


def extract_main_text(html: str) -> str:
    text = trafilatura.extract(
        html,
        output_format="txt",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    return (text or "").strip()


def build_summary_prompt(url: str, text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        clipped = text
    else:
        head = text[: int(max_chars * 0.7)]
        tail = text[-int(max_chars * 0.3):]
        clipped = head + "\n\n...(中略)...\n\n" + tail

    return f"""次のWebページ本文を要約してください。

URL: {url}

本文:
\"\"\"\n{clipped}\n\"\"\"
"""


# =============================
# UI helpers
# =============================
def label_max_chars(n: int) -> str:
    if n <= 3000:
        return "⚡ 速い（要点中心）"
    if n <= 6000:
        return "⚖️ バランス良し"
    return "🧠 高精度（やや遅い）"


def label_max_tokens(n: int) -> str:
    if n <= 500:
        return "🧾 短め"
    if n <= 900:
        return "📝 標準"
    return "📚 しっかり"


def normalize_model_output(text: str) -> str:
    if not text:
        return text
    return (
        text.replace("<br/>", "\n")
        .replace("<br>", "\n")
        .replace("&nbsp;", " ")
    )


# =============================
# Streamlit UI
# =============================
st.set_page_config(page_title="相棒LLM（ローカル）", layout="centered")
st.title("相棒LLM（ローカル / LM Studio）")

# ---- session state ----
if "chat_messages" not in st.session_state:
    st.session_state["chat_messages"] = []
if "url" not in st.session_state:
    st.session_state["url"] = ""
if "last_user_prompt" not in st.session_state:
    st.session_state["last_user_prompt"] = ""
if "prompt_store" not in st.session_state:
    st.session_state["prompt_store"] = load_store()

# ---- sidebar ----
with st.sidebar:
    st.header("接続設定")
    base_url = st.text_input("LM Studio Base URL", "http://localhost:1234/v1")
    if st.button("🔄 接続を再確認"):
        st.rerun()

    models, lm_ok, err = [], False, None
    t0 = time.time()
    try:
        models = lmstudio_models(base_url)
        lm_ok = True
    except Exception as e:
        err = e
    elapsed = int((time.time() - t0) * 1000)
    checked_at = datetime.now().strftime("%H:%M:%S")

    if lm_ok:
        st.success(f"🟢 接続中（{checked_at} / {elapsed}ms）")
    else:
        st.error("🔴 未接続")
        st.caption(err)

    st.divider()
    st.header("生成設定")
    max_chars = st.slider("入力文字数（要約）", 2000, 12000, 4000, 500)
    st.caption(label_max_chars(max_chars))

    max_tokens = st.slider("出力トークン", 200, 2000, 800, 50)
    st.caption(label_max_tokens(max_tokens))

    temperature = st.slider("Temperature", 0.0, 1.5, 0.3, 0.1)

    st.divider()
    st.header("相棒設定（表示のみ）")
    store = st.session_state["prompt_store"]
    active_name = store.get("active", "default")
    st.caption(f"現在の相棒プロンプト: **{active_name}**")
    st.caption("※編集は⚙️設定タブで行います（ここには表示しません）。")

if not lm_ok:
    st.stop()

model = st.selectbox("使用モデル", models)

tab_chat, tab_summary, tab_settings = st.tabs(["💬 Chat（相棒）", "📄 URL要約", "⚙️ 設定"])

# =============================
# Chat tab (LINE風：入力欄1つ + 下固定)
# =============================
with tab_chat:
    st.caption("雑談・相談・思考整理。普通に話しかけてOK。")

    st.markdown(
        """
        <style>
        .dock {
            position: fixed;
            left: 0;
            right: 0;
            bottom: 0;
            padding: 0.75rem 1rem;
            background: rgba(15, 16, 18, 0.92);
            backdrop-filter: blur(8px);
            border-top: 1px solid rgba(255,255,255,0.08);
            z-index: 1000;
        }
        .spacer { height: 110px; }
        footer {visibility: hidden;}
        </style>
        """,
        unsafe_allow_html=True,
    )

    # 会話ログ
    for msg in st.session_state["chat_messages"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # 入力バーに被らないためのスペーサー
    st.markdown('<div class="spacer"></div>', unsafe_allow_html=True)

    # 下固定入力バー（1つ）
    st.markdown('<div class="dock">', unsafe_allow_html=True)
    with st.form("dock_form", clear_on_submit=True):
        col1, col2 = st.columns([8, 1])
        with col1:
            user_prompt = st.text_input(
                "message",
                placeholder="相棒に話しかける…",
                label_visibility="collapsed",
            )
        with col2:
            submitted = st.form_submit_button("▶︎")
    st.markdown("</div>", unsafe_allow_html=True)

    if submitted and user_prompt.strip():
        user_prompt = user_prompt.strip()
        st.session_state["last_user_prompt"] = user_prompt

        # ユーザー発話を履歴へ
        st.session_state["chat_messages"].append({"role": "user", "content": user_prompt})

        system = current_buddy_prompt()
        history = st.session_state["chat_messages"][-12:]
        messages = [{"role": "system", "content": system}] + history

        with st.spinner("考え中…"):
            try:
                reply = call_lmstudio_chat_messages(
                    base_url=base_url,
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=180,
                )
                reply = normalize_model_output(reply)
            except Exception as e:
                reply = f"ごめん、今ちょい失敗した。エラー: {e}"

        st.session_state["chat_messages"].append({"role": "assistant", "content": reply})

        # 送信後は再描画して最新ログを表示
        st.rerun()

    if st.button("🧹 会話をリセット"):
        st.session_state["chat_messages"] = []
        st.session_state["last_user_prompt"] = ""
        st.rerun()

# =============================
# URL Summary tab
# =============================
with tab_summary:
    url = st.text_input("要約したいURL", key="url", placeholder="https://...")

    if st.button("要約する", type="primary"):
        if not url.strip():
            st.warning("URLを入力してください")
            st.stop()

        with st.spinner("取得・要約中…"):
            html = fetch_html(url)
            text = extract_main_text(html)
            prompt = build_summary_prompt(url, text, max_chars)

            system = (current_buddy_prompt() + "\n\n" + SUMMARY_ADDON).strip()
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ]

            summary = call_lmstudio_chat_messages(
                base_url=base_url,
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=180,
            )
            summary = normalize_model_output(summary)

        st.subheader("要約結果")
        st.markdown(summary)

        with st.expander("抽出した本文（先頭）を見る"):
            st.text(text[:2000])

# =============================
# Settings tab (Prompt editor + persistence)
# =============================
with tab_settings:
    st.subheader("相棒プロンプト（保存・切替）")
    st.caption("ここでだけ編集できます。Chat/URL要約画面には表示しません。")

    store = st.session_state["prompt_store"]
    prompts = store.get("prompts", {})
    if not prompts:
        store = _default_store()
        prompts = store["prompts"]
        st.session_state["prompt_store"] = store
        save_store(store)

    names = sorted(prompts.keys())
    active = store.get("active", names[0])

    col1, col2 = st.columns([2, 1])
    with col1:
        selected = st.selectbox("プリセット選択", options=names, index=names.index(active) if active in names else 0)
    with col2:
        if st.button("✅ このプリセットを使う"):
            store["active"] = selected
            st.session_state["prompt_store"] = store
            save_store(store)
            st.success(f"適用しました: {selected}")

    edit_key = f"prompt_edit_{selected}"
    if edit_key not in st.session_state:
        st.session_state[edit_key] = prompts.get(selected, "").strip()

    edited = st.text_area(
        "プロンプト本文（ここで編集）",
        value=st.session_state[edit_key],
        height=260,
    )

    cA, cB, cC = st.columns([1, 1, 2])
    with cA:
        if st.button("💾 上書き保存"):
            prompts[selected] = edited.strip()
            store["prompts"] = prompts
            st.session_state["prompt_store"] = store
            save_store(store)
            st.success("保存しました。")

    with cB:
        if st.button("↩︎ デフォルトに戻す"):
            prompts[selected] = DEFAULT_BUDDY_PROMPT
            store["prompts"] = prompts
            st.session_state["prompt_store"] = store
            save_store(store)
            st.session_state[edit_key] = DEFAULT_BUDDY_PROMPT
            st.success("デフォルトに戻して保存しました。")

    with cC:
        st.caption(f"保存先: `{PROMPTS_FILE}`")

    st.divider()
    st.subheader("プリセット管理")

    colN1, colN2, colN3 = st.columns([2, 1, 1])
    with colN1:
        new_name = st.text_input("新しいプリセット名", placeholder="例: buddy_casual / buddy_strict")
    with colN2:
        if st.button("➕ 新規作成"):
            nn = (new_name or "").strip()
            if not nn:
                st.warning("プリセット名を入力してください。")
            elif nn in prompts:
                st.warning("同名のプリセットが既にあります。")
            else:
                prompts[nn] = DEFAULT_BUDDY_PROMPT
                store["prompts"] = prompts
                store["active"] = nn
                st.session_state["prompt_store"] = store
                save_store(store)
                st.success(f"作成して適用しました: {nn}")
                st.rerun()
    with colN3:
        if st.button("🗑 選択プリセット削除"):
            if selected == "default":
                st.warning("default は削除できません。")
            else:
                prompts.pop(selected, None)
                store["prompts"] = prompts
                if store.get("active") == selected:
                    store["active"] = "default" if "default" in prompts else next(iter(prompts.keys()))
                st.session_state["prompt_store"] = store
                save_store(store)
                st.success(f"削除しました: {selected}")
                st.rerun()
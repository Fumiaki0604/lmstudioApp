import json
import time
import base64
from datetime import datetime
from pathlib import Path
from typing import Optional

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
SETTINGS_FILE = STORE_DIR / "settings.json"
SPEAKERS_FILE = Path(__file__).parent / "speakers_all.json"
TTS_QUEST_API = "https://api.tts.quest/v3/voicevox/synthesis"


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
# Settings (API keys etc.)
# =============================
def _default_settings():
    return {"tts_api_key": ""}


def load_settings() -> dict:
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return _default_settings()


def save_settings(settings: dict) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def get_tts_api_key() -> str:
    settings = st.session_state.get("app_settings", {})
    return settings.get("tts_api_key", "")


# =============================
# VOICEVOX (TTS Quest API)
# =============================
@st.cache_data(ttl=60)
def load_speakers() -> list:
    """speakers_all.json から話者一覧を読み込む（60秒でキャッシュ更新）"""
    if SPEAKERS_FILE.exists():
        return json.loads(SPEAKERS_FILE.read_text(encoding="utf-8"))
    return []


def get_speaker_data() -> dict:
    """話者データを構造化して返す
    Returns: {
        キャラ名: {
            "personality": str or None,
            "calls_profile": {"first_person": str, "second_person": str} or None,
            "styles": {スタイル名: speaker_id, ...}
        }, ...
    }
    """
    speakers = load_speakers()
    data = {}
    for sp in speakers:
        name = sp.get("name", "")
        if not name:
            continue
        profile = sp.get("dormitory_profile", {}) or {}
        personality = profile.get("personality")
        calls = sp.get("calls_profile", {}) or {}
        first_person = calls.get("first_person")
        second_person = calls.get("second_person")
        calls_info = None
        if first_person or second_person:
            calls_info = {"first_person": first_person, "second_person": second_person}

        styles = {}
        for style in sp.get("styles", []):
            if style.get("type") == "talk":
                style_name = style.get("name", "ノーマル")
                speaker_id = style.get("id")
                styles[style_name] = speaker_id

        if styles:
            data[name] = {
                "personality": personality,
                "calls_profile": calls_info,
                "styles": styles,
            }
    return data


def split_text_for_tts(text: str, max_len: int = 200) -> list:
    """テキストを句読点で分割し、max_len以下のチャンクに"""
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""
    # 句読点で分割（優先度: 。 → ！ → ？ → 、 → 改行）
    delimiters = ["。", "！", "？", "!", "?", "、", "\n"]

    i = 0
    while i < len(text):
        char = text[i]
        current += char

        # 区切り文字を見つけたら、そこで区切る
        if char in delimiters and len(current) >= 30:
            if len(current) <= max_len:
                chunks.append(current.strip())
                current = ""
        # max_lenを超えそうなら強制分割
        elif len(current) >= max_len:
            # 最後の区切り文字を探す
            last_delim = -1
            for d in delimiters:
                pos = current.rfind(d)
                if pos > last_delim:
                    last_delim = pos
            if last_delim > 30:
                chunks.append(current[:last_delim + 1].strip())
                current = current[last_delim + 1:]
            else:
                chunks.append(current.strip())
                current = ""
        i += 1

    if current.strip():
        chunks.append(current.strip())

    return [c for c in chunks if c]


def synthesize_voice(text: str, speaker_id: int, api_key: str = "", timeout: int = 30) -> tuple:
    """TTS Quest API で音声合成し、(mp3データ, エラーメッセージ)を返す（1チャンク分）"""
    try:
        params = {"text": text, "speaker": speaker_id}
        if api_key:
            params["key"] = api_key
        r = requests.get(
            TTS_QUEST_API,
            params=params,
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()

        if not data.get("success"):
            return None, f"API returned success=false: {data}"

        # mp3Base64があれば即座に返す（APIキー使用時）
        if "mp3Base64" in data:
            return base64.b64decode(data["mp3Base64"]), None

        # 非同期生成の場合: audioStatusUrlで完了を待つ
        status_url = data.get("audioStatusUrl")
        mp3_url = data.get("mp3DownloadUrl")

        if status_url and mp3_url:
            # 最大20秒待機（1秒間隔でポーリング）
            for i in range(20):
                status_r = requests.get(status_url, timeout=10)
                status_data = status_r.json()
                if status_data.get("isAudioReady"):
                    mp3_r = requests.get(mp3_url, timeout=timeout)
                    mp3_r.raise_for_status()
                    return mp3_r.content, None
                if status_data.get("isAudioError"):
                    return None, f"Audio generation error: {status_data}"
                time.sleep(1)
            return None, f"Timeout after 20s polling (last status: {status_data})"
        return None, "No audioStatusUrl or mp3DownloadUrl in response"
    except Exception as e:
        return None, f"Exception: {e}"


def synthesize_voice_full(text: str, speaker_id: int, api_key: str = "", timeout: int = 30, max_retries: int = 2) -> tuple:
    """長文テキストを分割して音声合成し、連結したmp3データを返す"""
    chunks = split_text_for_tts(text, max_len=200)
    if not chunks:
        return None, "No text to synthesize"

    audio_parts = []
    for i, chunk in enumerate(chunks):
        # チャンク間に待機を入れてAPI負荷を軽減
        if i > 0:
            time.sleep(0.5)

        # リトライ付きで音声生成
        audio_data = None
        last_error = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                time.sleep(1.0)  # リトライ前に待機
            audio_data, error = synthesize_voice(chunk, speaker_id, api_key, timeout)
            if audio_data:
                break
            last_error = error

        if not audio_data:
            return None, f"Chunk {i+1}/{len(chunks)} failed after {max_retries+1} attempts: {last_error}"
        audio_parts.append(audio_data)

    if not audio_parts:
        return None, "No audio generated"

    # MP3は単純に連結可能（フレーム単位なので）
    return b"".join(audio_parts), None


# =============================
# LM Studio helpers
# =============================
EMBEDDING_PREFIXES = ("text-embedding-", "embedding-", "nomic-embed-")


def is_chat_model(model_id: str) -> bool:
    """エンベディング専用モデルを除外する"""
    lower = model_id.lower()
    return not any(lower.startswith(p) for p in EMBEDDING_PREFIXES)


def lmstudio_models(base_url: str, timeout: int = 3):
    r = requests.get(base_url.rstrip("/") + "/models", timeout=timeout)
    r.raise_for_status()
    all_models = [m["id"] for m in r.json().get("data", [])]
    return [m for m in all_models if is_chat_model(m)]


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
if "app_settings" not in st.session_state:
    st.session_state["app_settings"] = load_settings()

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
    st.caption("※編集は⚙️設定タブで行います。")

    st.divider()
    st.header("🔊 音声読み上げ")
    tts_enabled = st.checkbox("返答を読み上げる", value=False)
    speaker_data = get_speaker_data()
    if speaker_data:
        char_names = list(speaker_data.keys())
        # デフォルトは「ずんだもん」
        default_char_idx = next((i for i, n in enumerate(char_names) if "ずんだもん" in n), 0)
        selected_char = st.selectbox("キャラクター", char_names, index=default_char_idx)

        char_info = speaker_data[selected_char]
        style_names = list(char_info["styles"].keys())
        # デフォルトは「ノーマル」
        default_style_idx = next((i for i, s in enumerate(style_names) if s == "ノーマル"), 0)
        selected_style = st.selectbox("スタイル", style_names, index=default_style_idx)

        speaker_id = char_info["styles"][selected_style]
        speaker_personality = char_info["personality"]
        speaker_calls_profile = char_info["calls_profile"]

        # キャラ連動プロンプト
        char_link_enabled = st.checkbox("キャラ連動プロンプト", value=False,
            help="ONにすると話者の性格に合わせた返答になります")
        if char_link_enabled and speaker_personality:
            st.caption(f"🎭 {speaker_personality}")
        elif char_link_enabled and not speaker_personality:
            st.caption("⚠️ このキャラクターの性格情報はありません")
        # 一人称・二人称の表示
        if char_link_enabled and speaker_calls_profile:
            fp = speaker_calls_profile.get("first_person") or "?"
            sp_person = speaker_calls_profile.get("second_person") or "?"
            st.caption(f"👤 一人称: {fp} / 二人称: {sp_person}")
    else:
        st.warning("speakers_all.json が見つかりません")
        tts_enabled = False
        speaker_id = 3  # fallback
        speaker_personality = None
        speaker_calls_profile = None
        char_link_enabled = False

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

    # 最後の音声があれば再生（非表示で自動再生）
    if "last_audio" in st.session_state and st.session_state["last_audio"]:
        audio_b64 = base64.b64encode(st.session_state["last_audio"]).decode()
        st.markdown(
            f'<audio autoplay style="display:none;"><source src="data:audio/mp3;base64,{audio_b64}" type="audio/mp3"></audio>',
            unsafe_allow_html=True,
        )
        # 再生後はクリア（連続再生防止）
        st.session_state["last_audio"] = None

    # TTS エラーがあれば表示
    if "tts_error" in st.session_state and st.session_state["tts_error"]:
        st.warning(f"🔊 音声生成失敗: {st.session_state['tts_error']}")
        st.session_state["tts_error"] = None

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
        # TTS有効時は短い返答を促す
        if tts_enabled:
            system = system + "\n\n【重要】音声読み上げモードです。返答は簡潔に、3〜4文程度（150文字以内）でまとめてください。"
        # キャラ連動プロンプトが有効なら性格情報を追加
        if char_link_enabled and speaker_personality:
            system = system + f"\n\n【キャラクター設定】\nあなたは以下の性格で返答してください: {speaker_personality}"
        # 一人称・二人称が設定されていれば追加
        if char_link_enabled and speaker_calls_profile:
            first_p = speaker_calls_profile.get("first_person")
            second_p = speaker_calls_profile.get("second_person")
            if first_p or second_p:
                pronoun_text = "【話し方の設定】\n"
                if first_p:
                    pronoun_text += f"- 自分のことは「{first_p}」と呼んでください\n"
                if second_p:
                    pronoun_text += f"- 相手（ユーザー）のことは「{second_p}」と呼んでください\n"
                system = system + "\n\n" + pronoun_text.strip()
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

        # 音声読み上げ
        if tts_enabled and reply:
            with st.spinner("🔊 音声生成中…"):
                tts_key = get_tts_api_key()
                audio_data, tts_error = synthesize_voice_full(reply, speaker_id, api_key=tts_key)
                if audio_data:
                    st.session_state["last_audio"] = audio_data
                elif tts_error:
                    st.session_state["tts_error"] = tts_error

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

    st.divider()
    st.subheader("🔑 API設定")

    app_settings = st.session_state["app_settings"]
    current_key = app_settings.get("tts_api_key", "")

    tts_api_key_input = st.text_input(
        "TTS Quest APIキー",
        value=current_key,
        type="password",
        placeholder="APIキーを入力（なくても動作しますが制限あり）",
        help="https://tts.quest/ でAPIキーを取得できます"
    )

    if st.button("💾 APIキーを保存"):
        app_settings["tts_api_key"] = tts_api_key_input.strip()
        st.session_state["app_settings"] = app_settings
        save_settings(app_settings)
        st.success("APIキーを保存しました。")

    if current_key:
        st.caption("✅ APIキー設定済み")
    else:
        st.caption("⚠️ APIキー未設定（制限付きで動作）")
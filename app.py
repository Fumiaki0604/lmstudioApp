import base64
import json
import os
import re
import struct
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import requests
import streamlit as st
import streamlit.components.v1 as components
from streamlit_js_eval import streamlit_js_eval
from streamlit_autorefresh import st_autorefresh

from tts import (
    TTS_QUEST_API, LOCAL_VOICEVOX_URL, AIVIS_URL, NOAH_SPEAKER_ID,
    _NOAH_MOOD_SPEAKER, _MOOD_STYLE_KEYWORDS,
    _noah_speaker_from_mood, _speaker_from_mood,
    split_text_for_tts, strip_urls_for_tts,
    synthesize_voice, synthesize_voice_full,
    check_local_voicevox, synthesize_voice_local, synthesize_voice_local_full,
    resample_wav_to, concat_wav_data,
    get_voicevox_user_dict, add_voicevox_dict_word, delete_voicevox_dict_word,
)
from chat import (
    DEFAULT_UA, NOAH_GATEWAY_URL, NOAH_GATEWAY_TOKEN,
    is_chat_model, lmstudio_models, call_lmstudio_chat_messages,
    call_noah_chat, call_char_chat, call_hermes_agent, call_hermes_agent_chat,
    fetch_html, extract_main_text, build_summary_prompt, normalize_model_output,
)
from news import (
    DEFAULT_RSS_FEEDS,
    fetch_rss_headlines, fetch_rss_items_with_category, get_rss_feeds,
    normalize_category, get_all_news_by_category, get_news_summary, get_news_for_category,
    get_weather_meguro, get_time_period,
)
from note_api import (
    _NOTE_ROLE_PROMPTS,
    note_create_note, note_draft_save, note_post_draft,
    _build_note_agent_messages, _build_hermes_prompt,
    _parse_advisor_output, _split_title_body,
)
from speakers import (
    SPEAKERS_FILE, NOAH_CONFIG_PATH, _SOULS_DIR, _AUTO_LOG_FILE,
    _load_soul, _auto_load_log, _auto_save_log, _detect_mention,
    load_speakers, load_speakers_raw, save_speakers, save_speaker_icon,
    load_noah_config, save_noah_config,
    update_speaker_icon, update_speaker_profile, get_speaker_data,
)

# 自律会話: バックグラウンドスレッド管理
_auto_generating = False
_auto_gen_lock = threading.Lock()

# =============================
# Constants
# =============================
DEFAULT_BUDDY_PROMPT = """あなたはユーザーの「助手兼相棒」です。
口調: フレンドリーで軽快。ただし馴れ馴れしすぎず、敬語とタメ口の中間。
方針:
- 結論→理由→次の一手、の順で話す。
- 事実と推測を分け、曖昧な点は正直に「不確か」と言う。
- ユーザーが"次に動ける"形で返す。
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
CHAT_SESSIONS_DIR = STORE_DIR / "chat_sessions"


# =============================
# Persistence
# =============================
def _default_store():
    return {"active": "default", "prompts": {"default": DEFAULT_BUDDY_PROMPT}}


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
# Settings
# =============================
def _default_settings():
    return {"tts_api_key": "", "tts_mode": "cloud", "rss_feeds": DEFAULT_RSS_FEEDS.copy(), "note_cookie": ""}


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
    return st.session_state.get("app_settings", {}).get("tts_api_key", "")


def get_tts_mode() -> str:
    return st.session_state.get("app_settings", {}).get("tts_mode", "cloud")


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


def build_talk_target_instruction(other_char_names: list, include_user: bool = True) -> str:
    if not include_user:
        if other_char_names:
            return "ユーザーには話しかけず、" + "と".join(other_char_names) + "に向けて話すこと。"
        return ""
    targets = ["ユーザー"] + other_char_names
    pct = round(100 / len(targets))
    parts = [f"{t}に話しかける({pct}%)" for t in targets]
    return "話しかける相手は、" + "、".join(parts) + "の確率で使い分けること。"


def export_chat_to_markdown(messages: list) -> str:
    lines = ["# 会話履歴", "", f"エクスポート日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "", "---", ""]
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        lines.append("## 👤 ユーザー" if role == "user" else "## 🤖 アシスタント")
        lines.extend(["", content, "", "---", ""])
    return "\n".join(lines)


def export_chat_to_json(messages: list) -> str:
    return json.dumps({"exported_at": datetime.now().isoformat(), "messages": messages}, ensure_ascii=False, indent=2)


# =============================
st.set_page_config(layout="centered")

# ---- session state ----
# ファイル保存ベースの会話履歴（現在未使用）
# if "current_session_id" not in st.session_state:
#     sessions = list_chat_sessions()
#     if sessions:
#         st.session_state["current_session_id"] = sessions[0]["id"]
#     else:
#         st.session_state["current_session_id"] = create_new_session()
#
# if "chat_messages" not in st.session_state:
#     session_data = load_chat_session(st.session_state["current_session_id"])
#     st.session_state["chat_messages"] = session_data.get("messages", [])

if "url" not in st.session_state:
    st.session_state["url"] = ""
if "last_user_prompt" not in st.session_state:
    st.session_state["last_user_prompt"] = ""
if "prompt_store" not in st.session_state:
    st.session_state["prompt_store"] = load_store()
if "app_settings" not in st.session_state:
    st.session_state["app_settings"] = load_settings()

# ---- 接続チェック（サイドバー外で実行） ----
if "base_url" not in st.session_state:
    st.session_state["base_url"] = "http://localhost:1234/v1"
base_url = st.session_state["base_url"]

models, lm_ok, err = [], False, None
t0 = time.time()
try:
    models = lmstudio_models(base_url)
    lm_ok = True
except Exception as e:
    err = e
elapsed = int((time.time() - t0) * 1000)
checked_at = datetime.now().strftime("%H:%M:%S")

# 生成設定のデフォルト値
if "max_chars" not in st.session_state:
    st.session_state["max_chars"] = 4000
if "max_tokens" not in st.session_state:
    st.session_state["max_tokens"] = 800
if "temperature" not in st.session_state:
    st.session_state["temperature"] = 0.3
if "auto_running" not in st.session_state:
    st.session_state["auto_running"] = False
if "auto_log" not in st.session_state:
    st.session_state["auto_log"] = []
if "auto_next_time" not in st.session_state:
    st.session_state["auto_next_time"] = 0.0

max_chars = st.session_state["max_chars"]
max_tokens = st.session_state["max_tokens"]
temperature = st.session_state["temperature"]

# ---- sidebar ----
with st.sidebar:
    # ① キャラクター選択
    st.header("キャラクター")
    speaker_data = get_speaker_data()
    if speaker_data:
        char_names = list(speaker_data.keys())
        default_char_idx = next((i for i, n in enumerate(char_names) if "ずんだもん" in n), 0)
        selected_char = st.selectbox("キャラクター", char_names, index=default_char_idx, label_visibility="collapsed")

        # キャラ変更検出 → 会話リセット
        prev_char = st.session_state.get("_prev_selected_char", "")
        if prev_char and prev_char != selected_char:
            if "category_chat_messages" in st.session_state:
                st.session_state["category_chat_messages"] = {}
            st.session_state["last_user_prompt"] = ""
            st.session_state["last_audio"] = None
        st.session_state["_prev_selected_char"] = selected_char

        char_info = speaker_data[selected_char]
        style_names = list(char_info["styles"].keys())
        default_style_idx = next((i for i, s in enumerate(style_names) if s == "ノーマル"), 0)
        # スタイル複数持ちはMOOD自動切替のため手動選択不要
        if len(style_names) == 1:
            selected_style = style_names[0]
        else:
            selected_style = style_names[default_style_idx]

        speaker_id = char_info["styles"][selected_style]
        speaker_personality = char_info["personality"]
        speaker_gender = char_info.get("gender")
        speaker_calls_profile = char_info["calls_profile"]

        if speaker_personality:
            st.caption(f"{speaker_personality}")
        if speaker_gender:
            st.caption(f"👤 性別: {speaker_gender}")
        if speaker_calls_profile:
            fp = speaker_calls_profile.get("first_person") or "?"
            sp_person = speaker_calls_profile.get("second_person") or "?"
            st.caption(f"👤 一人称: {fp} / 二人称: {sp_person}")

        # 会話モード選択
        st.divider()
        chat_mode = st.radio("会話モード", ["1対1", "3人", "4人"], horizontal=True, key="chat_mode")

        # キャラB/C選択（複数人モード時）
        chat_characters = [{"name": selected_char, "id": speaker_id, "personality": speaker_personality, "gender": speaker_gender, "calls_profile": speaker_calls_profile, "styles": char_info.get("styles", {}), "is_noah": char_info.get("is_noah", False), "is_hermes_agent": char_info.get("is_hermes_agent", False), "hermes_profile": char_info.get("hermes_profile", "")}]
        if chat_mode in ["3人", "4人"]:
            other_chars = [n for n in char_names if n != selected_char]
            if other_chars:
                st.caption("**キャラB**")
                # 前回選択値を維持
                prev_b = st.session_state.get("_prev_char_b", "")
                idx_b = other_chars.index(prev_b) if prev_b in other_chars else 0
                selected_char_b = st.selectbox("キャラクターB", other_chars, index=idx_b, key="char_b_select", label_visibility="collapsed")
                st.session_state["_prev_char_b"] = selected_char_b
                char_info_b = speaker_data[selected_char_b]
                style_names_b = list(char_info_b["styles"].keys())
                default_style_idx_b = next((i for i, s in enumerate(style_names_b) if s == "ノーマル"), 0)
                selected_style_b = style_names_b[default_style_idx_b]
                if char_info_b.get("personality"):
                    st.caption(f"{char_info_b['personality']}")
                if char_info_b.get("gender"):
                    st.caption(f"👤 性別: {char_info_b['gender']}")
                chat_characters.append({
                    "name": selected_char_b,
                    "id": char_info_b["styles"][selected_style_b],
                    "personality": char_info_b["personality"],
                    "gender": char_info_b.get("gender"),
                    "calls_profile": char_info_b["calls_profile"],
                    "styles": char_info_b.get("styles", {}),
                    "is_noah": char_info_b.get("is_noah", False),
                    "is_hermes_agent": char_info_b.get("is_hermes_agent", False),
                    "hermes_profile": char_info_b.get("hermes_profile", ""),
                })
        if chat_mode == "4人":
            other_chars_c = [n for n in char_names if n != selected_char and n != selected_char_b]
            if other_chars_c:
                st.caption("**キャラC**")
                prev_c = st.session_state.get("_prev_char_c", "")
                idx_c = other_chars_c.index(prev_c) if prev_c in other_chars_c else 0
                selected_char_c = st.selectbox("キャラクターC", other_chars_c, index=idx_c, key="char_c_select", label_visibility="collapsed")
                st.session_state["_prev_char_c"] = selected_char_c
                char_info_c = speaker_data[selected_char_c]
                style_names_c = list(char_info_c["styles"].keys())
                default_style_idx_c = next((i for i, s in enumerate(style_names_c) if s == "ノーマル"), 0)
                selected_style_c = style_names_c[default_style_idx_c]
                if char_info_c.get("personality"):
                    st.caption(f"{char_info_c['personality']}")
                if char_info_c.get("gender"):
                    st.caption(f"👤 性別: {char_info_c['gender']}")
                chat_characters.append({
                    "name": selected_char_c,
                    "id": char_info_c["styles"][selected_style_c],
                    "personality": char_info_c["personality"],
                    "gender": char_info_c.get("gender"),
                    "calls_profile": char_info_c["calls_profile"],
                    "styles": char_info_c.get("styles", {}),
                    "is_noah": char_info_c.get("is_noah", False),
                    "is_hermes_agent": char_info_c.get("is_hermes_agent", False),
                    "hermes_profile": char_info_c.get("hermes_profile", ""),
                })
    else:
        st.warning("speakers_all.json が見つかりません")
        speaker_id = 3
        speaker_personality = None
        speaker_gender = None
        speaker_calls_profile = None
        chat_mode = "1対1"
        chat_characters = []

    st.divider()

    # ② 音声読み上げ
    st.header("🔊 音声読み上げ")
    tts_enabled = st.checkbox("返答を読み上げる", value=False)

    if tts_enabled:
        tts_mode_options = {"cloud": "☁️ クラウド", "local": "💻 ローカル"}
        current_tts_mode = get_tts_mode()
        tts_mode = st.radio(
            "TTSエンジン",
            options=list(tts_mode_options.keys()),
            format_func=lambda x: tts_mode_options[x],
            index=0 if current_tts_mode == "cloud" else 1,
            horizontal=True,
            label_visibility="collapsed",
        )
        if tts_mode == "local":
            if check_local_voicevox():
                st.caption("✅ VOICEVOX接続中")
            else:
                st.caption("⚠️ VOICEVOX未起動")
    else:
        tts_mode = get_tts_mode()

    # st.divider()
    # # ③ 会話履歴（ファイル保存ベース - 現在未使用）
    # st.header("💬 会話履歴")
    # sessions = list_chat_sessions()
    # current_id = st.session_state.get("current_session_id", "")
    #
    # for sess in sessions[:10]:
    #     is_current = sess["id"] == current_id
    #     title = sess["title"] or "無題"
    #     try:
    #         dt = datetime.fromisoformat(sess["updated_at"])
    #         date_str = dt.strftime("%m/%d %H:%M")
    #     except Exception:
    #         date_str = ""
    #
    #     col1, col2 = st.columns([5, 1])
    #     with col1:
    #         btn_type = "primary" if is_current else "secondary"
    #         if st.button(f"{'▶ ' if is_current else ''}{title}", key=f"sess_{sess['id']}", use_container_width=True, type=btn_type):
    #             if not is_current:
    #                 st.session_state["current_session_id"] = sess["id"]
    #                 session_data = load_chat_session(sess["id"])
    #                 st.session_state["chat_messages"] = session_data.get("messages", [])
    #                 st.rerun()
    #     with col2:
    #         if st.button("🗑", key=f"del_{sess['id']}", help="削除"):
    #             delete_chat_session(sess["id"])
    #             if is_current:
    #                 remaining = list_chat_sessions()
    #                 if remaining:
    #                     st.session_state["current_session_id"] = remaining[0]["id"]
    #                     session_data = load_chat_session(remaining[0]["id"])
    #                     st.session_state["chat_messages"] = session_data.get("messages", [])
    #                 else:
    #                     new_id = create_new_session()
    #                     st.session_state["current_session_id"] = new_id
    #                     st.session_state["chat_messages"] = []
    #             st.rerun()
    #
    #     if date_str:
    #         st.caption(f"　　{date_str}")

if not lm_ok:
    st.error("🔴 LM Studio未接続 - 設定タブで接続先を確認してください")
    st.stop()

model = st.selectbox("使用モデル", models)

# autorefresh: 自律会話が動いているときだけマウント
# 停止中にマウントすると、LM呼び出し中にスクリプトが中断されるため条件付きに変更
if st.session_state.get("auto_running"):
    st_autorefresh(interval=10000, key="auto_refresh_tick")

tab_chat, tab_radio, tab_auto, tab_note, tab_settings = st.tabs(["💬 Chat（相棒）", "📻 ニュースラジオ", "🏠 自律会話", "📝 note記事", "⚙️ 設定"])

# =============================
# Chat tab (LINE風：入力欄1つ + 下固定)
# =============================
with tab_chat:
    # 接続状態表示
    conn_col1, conn_col2 = st.columns([5, 2])
    with conn_col2:
        st.caption(f"🟢 接続中 {checked_at}" if lm_ok else "🔴 未接続")

    # カテゴリ取得
    all_news = get_all_news_by_category(max_per_source=10)
    categories = ["フリー"] + sorted(all_news.keys())

    # カテゴリ選択
    if "chat_category" not in st.session_state:
        st.session_state["chat_category"] = "フリー"
    if "category_chat_messages" not in st.session_state:
        st.session_state["category_chat_messages"] = {}

    selected_category = st.selectbox(
        "話題カテゴリ",
        categories,
        index=categories.index(st.session_state["chat_category"]) if st.session_state["chat_category"] in categories else 0,
        key="category_select"
    )
    st.session_state["chat_category"] = selected_category

    # カテゴリ別チャット履歴の初期化
    if selected_category not in st.session_state["category_chat_messages"]:
        st.session_state["category_chat_messages"][selected_category] = []
    if "news_fingerprint" not in st.session_state:
        st.session_state["news_fingerprint"] = {}

    # 現在のカテゴリのチャット履歴を取得
    current_chat = st.session_state["category_chat_messages"][selected_category]

    # カテゴリ別ニュース表示
    if selected_category != "フリー":
        cat_news = all_news.get(selected_category, [])
        if cat_news:
            with st.expander(f"📰 {selected_category}の最新ニュース ({len(cat_news)}件)", expanded=False):
                for item in cat_news:
                    link = item.get("link", "")
                    if link:
                        st.markdown(f"• [{item['title']}]({link}) ({item['source']})")

        # ニュースのフィンガープリント（最初の3件のタイトル）
        news_fingerprint = "|".join([item["title"] for item in cat_news[:3]])
        old_fingerprint = st.session_state["news_fingerprint"].get(selected_category, "")
        fingerprint_changed = news_fingerprint != old_fingerprint and old_fingerprint != ""

        # 初回 or キャッシュ更新で新記事が来たらAIがニュース紹介
        should_introduce = (not current_chat) or fingerprint_changed
        if st.session_state.get("_news_generating"):
            should_introduce = False
        if should_introduce and cat_news:
            st.session_state["_news_generating"] = True
            news_count = min(len(cat_news), 3)
            news_text = get_news_for_category(selected_category, max_items=news_count)

            def build_intro_prompt(char_info_dict, role_instruction=""):
                """ニュース紹介用プロンプトを構築"""
                c_personality = char_info_dict.get("personality") or ""
                c_gender = char_info_dict.get("gender") or ""
                c_calls = char_info_dict.get("calls_profile") or {}
                fp = c_calls.get("first_person") or ""
                sp = c_calls.get("second_person") or ""
                c_nicknames = c_calls.get("char_nicknames") or {}
                # 複数人モード時の呼び名
                nn_lines = ""
                other_char_names = [c["name"] for c in chat_characters if c["name"] != char_info_dict["name"]]
                for on in other_char_names:
                    nn = c_nicknames.get(on)
                    if nn:
                        nn_lines += f"- {on}のことは必ず「{nn}」と呼ぶこと\n"
                prefix = "新しいニュースが入ってきたよ！\n\n" if fingerprint_changed else ""
                base_instruction = role_instruction or f"友達に話題をふるように、以下の最新ニュース{news_count}つを紹介して。"
                comment_rule = (
                    "- ニュースの紹介と感想は1セットで完結させること。同じニュースに対して感想を繰り返さないこと"
                    if news_count == 1 else
                    "- 各ニュースに対してキャラクターとしての感想を自然に入れる。ただし毎回同じパターン（「このニュースを見て〜は…」等）の定型文にしない。感想の入れ方を毎回変えること"
                )
                return f"""{prefix}{base_instruction}

【ニュース】
{news_text}

【必須ルール】
- 箇条書きは絶対に使わない
- 各ニュースを紹介した後、必ず参照元URLを「詳しくはこちら→ URL」の形で記載
{comment_rule}
- 冒頭で一人称を名乗らない。いきなり話題に入る（例:「ねえねえ、こんなニュースあったんだけど」）
- 全体的なトレンドのまとめは不要
- 「Note:」「注:」等のメタコメントや補足説明は絶対に含めない。純粋に会話文だけを出力すること

【キャラクター設定（これに従って話して）】
{f'性格: {c_personality}' if c_personality else '性格: フレンドリーで親しみやすい'}
{f'性別: {c_gender}' if c_gender else ''}
{f'一人称: {fp}（必ずこの一人称を使うこと）' if fp else ''}
{f'相手の呼び方: {sp}（ただし呼びかけには使わない）' if sp else ''}
{nn_lines}"""

            # --- 1対1モード: キャラAのみ紹介 ---
            if chat_mode == "1対1":
                intro_prompt = build_intro_prompt(chat_characters[0])
                with st.spinner(f"📰 {selected_category}の最新ニュースを確認中…"):
                    try:
                        intro_reply, _mood = call_char_chat(
                            chat_characters[0],
                            messages=[{"role": "user", "content": intro_prompt}],
                            base_url=base_url, model=model,
                            temperature=temperature, max_tokens=max_tokens,
                        )
                        intro_reply = normalize_model_output(intro_reply)
                        current_chat.append({"role": "assistant", "content": intro_reply})
                        st.session_state["news_fingerprint"][selected_category] = news_fingerprint

                        if tts_enabled and intro_reply:
                            with st.spinner("🔊 音声生成中…"):
                                tts_text = strip_urls_for_tts(intro_reply)
                                if tts_mode == "local" or speaker_id >= 800_000_000:
                                    audio_data, tts_error = synthesize_voice_local_full(tts_text, speaker_id)
                                    audio_format = "wav"
                                else:
                                    tts_key = get_tts_api_key()
                                    audio_data, tts_error = synthesize_voice_full(tts_text, speaker_id, api_key=tts_key)
                                    audio_format = "mp3"
                                if audio_data:
                                    if "news_intro_audio" not in st.session_state:
                                        st.session_state["news_intro_audio"] = {}
                                    st.session_state["news_intro_audio"][selected_category] = {"data": audio_data, "format": audio_format}
                                    st.session_state["last_audio"] = audio_data
                                    st.session_state["last_audio_format"] = audio_format
                                elif tts_error:
                                    st.session_state["tts_error"] = tts_error
                    except Exception as e:
                        st.session_state["tts_error"] = f"ニュース紹介エラー: {e}"

            # --- 複数人モード: キャラA=要約、キャラB=深掘り、キャラC=別視点 ---
            else:
                audio_queue = []
                prev_replies = []
                for idx, char in enumerate(chat_characters):
                    # キャラAのみニュース原文付きで紹介。B/CはキャラAの発言を踏まえて会話
                    if idx == 0:
                        instruction = f"友達に話題をふるように、以下の最新ニュース{news_count}つを紹介して。"
                        intro_prompt = build_intro_prompt(char, role_instruction=instruction)
                    else:
                        # ニュース原文は渡さない。前のキャラの発言だけを元に会話させる
                        intro_prompt = None
                    # 前のキャラの返答をコンテキストとして追加
                    c_calls = char.get("calls_profile") or {}
                    c_fp = c_calls.get("first_person") or ""
                    c_sp = c_calls.get("second_person") or ""
                    c_personality = char.get("personality") or ""
                    c_gender = char.get("gender") or ""
                    c_nicknames = c_calls.get("char_nicknames") or {}
                    nickname_lines = ""
                    all_other_names = [c["name"] for c in chat_characters if c["name"] != char["name"]]
                    for on in all_other_names:
                        nn = c_nicknames.get(on)
                        if nn:
                            nickname_lines += f"- {on}のことは必ず「{nn}」と呼ぶこと。「{on}」とフルネームで呼ばないこと。\n"
                    other_desc = ""
                    for oc in chat_characters:
                        if oc["name"] == char["name"]:
                            continue
                        oc_nn = c_nicknames.get(oc["name"], oc["name"])
                        oc_p = oc.get("personality") or ""
                        if oc_p:
                            other_desc += f"- {oc_nn}: {oc_p[:100]}\n"
                    _other_desc_block = ("【一緒にいる相手の紹介】\n" + other_desc) if other_desc else ""
                    persona_block = f"""あなたは「{char['name']}」です。
{f'- 一人称は「{c_fp}」を使うこと。' if c_fp else ''}
{f'- 性別: {c_gender}' if c_gender else ''}
{nickname_lines}{f'- 性格: {c_personality}' if c_personality else ''}
- 他のキャラの口調・一人称・二人称・話し方を絶対に真似しないこと。自分独自の視点と言葉で話すこと。
- {build_talk_target_instruction([p['name'] for p in prev_replies], include_user=False)}
{_other_desc_block}"""

                    if idx == 0:
                        # キャラA: ニュース紹介プロンプト（従来通り）
                        msgs = [{"role": "user", "content": intro_prompt}]
                    else:
                        # キャラB/C: 前のキャラの発言に対して会話として返す
                        last_speaker = prev_replies[-1]['name']
                        role_desc = f"""{last_speaker}が話した内容に対して、友達同士の会話として返答してください。
- {last_speaker}の話を受けて、共感・驚き・質問・ツッコミ・別の意見など自然なリアクションをすること
- ニュースの内容をもう一度説明し直さないこと（相手がもう話したので）
- 自分が気になったポイントに絞ってリアクションし、自分ならではの感想を加えること
- 箇条書きは使わない
- 会話のキャッチボールを意識すること（一方的なプレゼンにしない）"""

                        system_prompt = f"""{persona_block}

{role_desc}

【絶対厳守】上記のキャラクター設定（一人称・性別・性格・口調）に必ず従うこと。他のキャラの話し方に絶対に引きずられないこと。
「Note:」「注:」等のメタコメントは絶対に含めないこと。"""
                        msgs = [{"role": "system", "content": system_prompt}]
                        # 前キャラの発言をuserロールで渡す（会話の相手として）
                        for prev in prev_replies:
                            msgs.append({"role": "user", "content": f"{prev['name']}: {prev['reply']}"})

                    with st.spinner(f"📰 {char['name']}がニュースを確認中…"):
                        try:
                            reply, _mood = call_char_chat(
                                char, msgs,
                                base_url=base_url, model=model,
                                temperature=temperature, max_tokens=max_tokens,
                            )
                            reply = normalize_model_output(reply)
                        except Exception as e:
                            reply = f"エラー: {e}"

                    current_chat.append({"role": "assistant", "content": reply, "char_name": char["name"]})
                    prev_replies.append({"name": char["name"], "reply": reply})

                    if tts_enabled and reply:
                        with st.spinner(f"🔊 {char['name']}の音声生成中…"):
                            tts_text = strip_urls_for_tts(reply)
                            if tts_mode == "local" or char["id"] >= 800_000_000:
                                ad, te = synthesize_voice_local_full(tts_text, char["id"])
                                af = "wav"
                            else:
                                tts_key = get_tts_api_key()
                                ad, te = synthesize_voice_full(tts_text, char["id"], api_key=tts_key)
                                af = "mp3"
                            if ad:
                                audio_queue.append({"data": ad, "format": af})

                st.session_state["news_fingerprint"][selected_category] = news_fingerprint
                if audio_queue:
                    st.session_state["last_audio"] = audio_queue[0]["data"]
                    st.session_state["last_audio_format"] = audio_queue[0]["format"]
                    if len(audio_queue) > 1:
                        st.session_state["audio_queue"] = audio_queue[1:]

            st.session_state["_news_generating"] = False
            st.rerun()

        # 2回目以降の訪問: カテゴリ切り替え時のみ1回だけ再生
        elif tts_enabled and "news_intro_audio" in st.session_state:
            stored = st.session_state["news_intro_audio"].get(selected_category)
            last_played_cat = st.session_state.get("_last_played_intro_cat", "")
            if stored and last_played_cat != selected_category:
                st.session_state["last_audio"] = stored["data"]
                st.session_state["last_audio_format"] = stored["format"]
                st.session_state["_last_played_intro_cat"] = selected_category

        st.caption(f"💡 {selected_category}に関する話題でチャットします")
    else:
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
        /* チャットアバターのサイズ調整・背景透過 */
        .stChatMessage [data-testid="chatAvatarIcon-assistant"] img,
        .stChatMessage img[class*="avatar"],
        [data-testid="stChatMessage"] img {
            width: 40px !important;
            height: 40px !important;
            min-width: 40px !important;
            min-height: 40px !important;
            border-radius: 50% !important;
            object-fit: cover !important;
            background: transparent !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # キャラアイコンマップ構築
    char_icon_map = {}
    if speaker_data:
        for cname, cinfo in speaker_data.items():
            icon_path = cinfo.get("icon")
            if icon_path and os.path.exists(icon_path):
                char_icon_map[cname] = icon_path

    # 会話ログ（カテゴリ別）
    for msg in current_chat:
        char_name = msg.get("char_name")
        avatar = char_icon_map.get(char_name) if char_name else None
        # 1対1モードでchar_nameがない場合はキャラAのアイコン
        if not char_name and msg["role"] == "assistant" and chat_characters:
            avatar = char_icon_map.get(chat_characters[0]["name"])
        with st.chat_message(msg["role"], avatar=avatar):
            if char_name:
                st.caption(char_name)
            st.markdown(msg["content"])

    # 音声再生 - 親フレームで再生（iframe autoplay制限回避）
    # TTS pending file（finally/並行ラン競合の回避）があれば読み込んでセッションに移す
    import pathlib as _pl_tts
    _tts_pending_path = _pl_tts.Path("/tmp/_lmstudio_tts_pending.bin")
    _tts_fmt_path = _pl_tts.Path("/tmp/_lmstudio_tts_fmt.txt")
    if _tts_pending_path.exists():
        try:
            _pending_bytes = _tts_pending_path.read_bytes()
            _pending_fmt = _tts_fmt_path.read_text().strip() if _tts_fmt_path.exists() else "wav"
            if _pending_bytes:
                st.session_state["last_audio"] = _pending_bytes
                st.session_state["last_audio_format"] = _pending_fmt
        except Exception:
            pass
        finally:
            _tts_pending_path.unlink(missing_ok=True)
            _tts_fmt_path.unlink(missing_ok=True)
    if "last_audio" in st.session_state and st.session_state["last_audio"]:
        all_audio = [{"data": st.session_state["last_audio"], "format": st.session_state.get("last_audio_format", "mp3")}]
        if st.session_state.get("audio_queue"):
            all_audio.extend(st.session_state["audio_queue"])
            st.session_state["audio_queue"] = []

        play_count = st.session_state.get("_tts_play_count", 0) + 1
        st.session_state["_tts_play_count"] = play_count

        # WAV同士の場合はリサンプルして1ファイルに結合（JS queue不要）
        wav_only = all(a["format"] == "wav" for a in all_audio)
        TARGET_SR = 44100
        if len(all_audio) > 1 and wav_only:
            # 全WAVを44100Hzに統一してから無音込みで結合
            first_wav = all_audio[0]["data"]
            ch = struct.unpack('<H', first_wav[22:24])[0] if len(first_wav) >= 44 else 1
            bps = struct.unpack('<H', first_wav[34:36])[0] if len(first_wav) >= 44 else 16
            silence_samples = TARGET_SR  # 1秒
            silence_pcm = b'\x00' * (silence_samples * ch * (bps // 8))
            silence_data_size = len(silence_pcm)
            silence_wav = struct.pack(
                '<4sI4s4sIHHIIHH4sI',
                b'RIFF', silence_data_size + 36, b'WAVE', b'fmt ', 16, 1,
                ch, TARGET_SR, TARGET_SR * ch * (bps // 8), ch * (bps // 8), bps,
                b'data', silence_data_size,
            ) + silence_pcm
            parts = []
            for i, a in enumerate(all_audio):
                normalized = resample_wav_to(a["data"], TARGET_SR)
                if i > 0:
                    parts.append(silence_wav)
                parts.append(normalized)
            combined = concat_wav_data(parts)
            all_audio = [{"data": combined, "format": "wav"}]

        # 全音声を streamlit_js_eval で再生
        # WAV同士は concat で1ファイルに統合済みなので基本的に entries は1つ
        # MP3+WAV混在の場合は最初の1エントリのみ再生（cloud+Noahの暫定対応）
        a = all_audio[0]
        mime = "audio/wav" if a["format"] == "wav" else "audio/mp3"
        b64 = base64.b64encode(a["data"]).decode()
        js_code = f"""
(function() {{
    var w = window.parent || window;
    var audio = new w.Audio('data:{mime};base64,{b64}');
    audio.play();
    return 'ok';
}})()"""
        streamlit_js_eval(js_expressions=js_code, key=f"tts_play_{play_count}")

        st.session_state["last_audio"] = None
        st.session_state["last_audio_format"] = None

    # TTS エラーがあれば表示
    if "tts_error" in st.session_state and st.session_state["tts_error"]:
        st.warning(f"🔊 音声生成失敗: {st.session_state['tts_error']}")
        st.session_state["tts_error"] = None

    # 入力バーに被らないためのスペーサー
    st.markdown('<div class="spacer"></div>', unsafe_allow_html=True)

    # 音声入力の結果をチェック（localStorageから取得）
    if "stt_text" not in st.session_state:
        st.session_state["stt_text"] = ""
    stt_result = streamlit_js_eval(js_expressions="localStorage.getItem('stt_result') || ''", key="stt_check")
    if stt_result and stt_result.strip():
        st.session_state["stt_text"] = stt_result.strip()
        # localStorageをクリア
        streamlit_js_eval(js_expressions="localStorage.removeItem('stt_result')", key="stt_clear")

    # 下固定入力バー
    st.markdown('<div class="dock">', unsafe_allow_html=True)
    with st.form("dock_form", clear_on_submit=True):
        col1, col2, col3 = st.columns([7, 1, 1])
        with col1:
            default_text = st.session_state.get("stt_text", "")
            user_prompt = st.text_input(
                "message",
                value=default_text,
                placeholder="相棒に話しかける…",
                label_visibility="collapsed",
            )
        with col2:
            # Enter押下時はこちらが実行される（最初のsubmit_button）
            submitted = st.form_submit_button("▶︎")
        with col3:
            mic_clicked = st.form_submit_button("🎤")

    # マイクボタン押下時の処理
    if mic_clicked:
        components.html("""
        <script>
        (function() {
            try {
                const parentWindow = window.parent;
                const SpeechRecognition = parentWindow.SpeechRecognition || parentWindow.webkitSpeechRecognition
                    || window.SpeechRecognition || window.webkitSpeechRecognition;

                if (!SpeechRecognition) {
                    alert('このブラウザは音声入力に対応していません');
                    return;
                }

                const recognition = new SpeechRecognition();
                recognition.lang = 'ja-JP';
                recognition.continuous = false;
                recognition.interimResults = false;

                recognition.onresult = function(event) {
                    const transcript = event.results[0][0].transcript;
                    try {
                        parentWindow.localStorage.setItem('stt_result', transcript);
                    } catch(e) {
                        localStorage.setItem('stt_result', transcript);
                    }
                    parentWindow.location.reload();
                };

                recognition.onerror = function(event) {
                    alert('音声認識エラー: ' + event.error);
                };

                recognition.onend = function() {
                    document.getElementById('stt_status').textContent = '認識完了';
                };

                recognition.start();
                document.getElementById('stt_status').textContent = '🔴 録音中... 話しかけてください';
            } catch(e) {
                alert('音声認識の開始に失敗: ' + e.message);
            }
        })();
        </script>
        <p id="stt_status" style="color:#ff4b4b;font-size:14px;font-weight:bold;">🎤 マイク起動中...</p>
        """, height=40)
        st.stop()

    st.markdown("</div>", unsafe_allow_html=True)

    # 音声入力使用後はクリア
    if st.session_state.get("stt_text"):
        st.session_state["stt_text"] = ""

    # --- システムプロンプト構築用ヘルパー ---
    def build_system_prompt(char_info_dict, extra="", continue_mode=False):
        """キャラ情報dictからシステムプロンプトを構築"""
        c_personality = char_info_dict.get("personality")
        c_gender = char_info_dict.get("gender")
        c_calls = char_info_dict.get("calls_profile") or {}
        char_name = char_info_dict.get("name", "")
        if c_personality:
            # キャラ名を明示してから性格を提示
            sys = f"あなたは「{char_name}」です。以下の性格・口調で返答してください。\n\n{c_personality}"
        else:
            sys = current_buddy_prompt()
        now = datetime.now(ZoneInfo("Asia/Tokyo"))
        weekdays = ["月", "火", "水", "木", "金", "土", "日"]
        today_str = now.strftime("%Y年%m月%d日") + f"（{weekdays[now.weekday()]}）"
        time_str = now.strftime("%H:%M")
        period = get_time_period(now.hour)
        sys += f"\n\n【現在の日時】{today_str} {time_str}（{period}）"
        weather = get_weather_meguro()
        if weather:
            sys += f"\n【目黒区の天気】{weather['desc']}、気温{weather['temp']}℃（体感{weather['feel']}℃）、湿度{weather['humidity']}%"
        current_cat = st.session_state.get("chat_category", "フリー")
        if current_cat == "フリー":
            news = get_news_summary(max_per_source=3)
            if news:
                sys += f"\n\n【最新ニュース】\n{news}"
        else:
            cat_news_text = get_news_for_category(current_cat)
            if cat_news_text:
                sys += f"\n\n【{current_cat}の最新ニュース】\n{cat_news_text}"
                if continue_mode:
                    sys += f"\n\n【会話の焦点】「{current_cat}」について既に会話が始まっています。ニュースを最初から紹介し直さず、これまでの会話の流れを踏まえて話題を深めるか広げてください。"
                else:
                    sys += f"\n\n【会話の焦点】ユーザーは「{current_cat}」に関する話題に興味があります。この分野のニュースについて詳しく解説・議論してください。"
        if tts_enabled and tts_mode == "cloud":
            sys += "\n\n【重要】音声読み上げモードです。返答は簡潔に、3〜4文程度（150文字以内）でまとめてください。"
        if c_gender:
            gender_hint = {
                "男性": "性別: 男性（大人の男性として話す）",
                "女性": "性別: 女性（大人の女性として話す）",
                "男の子": "性別: 男の子（少年らしい好奇心や元気さ、若さのある話し方で）",
                "女の子": "性別: 女の子（少女らしい感性や素直さ、若さのある話し方で）",
            }.get(c_gender, f"性別: {c_gender}")
            sys += f"\n{gender_hint}"
        first_p = c_calls.get("first_person")
        second_p = c_calls.get("second_person")
        if first_p or second_p:
            pronoun_text = "【話し方の設定】\n"
            if first_p:
                pronoun_text += f"- 自分のことは「{first_p}」と呼んでください\n"
            if second_p:
                pronoun_text += f"- 相手（ユーザー）のことは会話の流れで「{second_p}」と呼んでください（ただし挨拶や呼びかけには使わない。「こんにちは、{second_p}」はNG）\n"
            sys += "\n\n" + pronoun_text.strip()
        # Voicevoxキャラでスタイル複数持ちの場合はMOOD指示を追加
        char_styles = char_info_dict.get("styles") or {}
        if not char_info_dict.get("is_noah") and len(char_styles) > 1:
            style_keys = "、".join(char_styles.keys())
            sys += f"\n\n【感情タグ】返答の冒頭に必ず [MOOD:xxx] を付けること。xxx は normal/happy/angry/sad/whisper/tired/calm/sexy のどれかを選ぶ。"
        if extra:
            sys += "\n\n" + extra
        return sys

    def generate_tts_for_char(text, char_speaker_id):
        """キャラのspeaker_idでTTS生成し音声データを返す"""
        tts_text = strip_urls_for_tts(text)
        # AivisSpeech ID（Noah等）は常にローカルへ
        if tts_mode == "local" or char_speaker_id >= 800_000_000:
            ad, te = synthesize_voice_local_full(tts_text, char_speaker_id)
            return ad, "wav", te
        else:
            tts_key = get_tts_api_key()
            ad, te = synthesize_voice_full(tts_text, char_speaker_id, api_key=tts_key)
            return ad, "mp3", te

    def run_multi_char_round(chat_characters, current_chat, base_url, model, temperature, max_tokens, tts_enabled, continue_mode=False):
        """複数キャラの1ラウンド分の応答を生成"""
        audio_queue = []
        for idx, char in enumerate(chat_characters):
            char_name = char["name"]
            other_names = [c["name"] for c in chat_characters if c["name"] != char_name]
            c_calls = char.get("calls_profile") or {}
            c_fp = c_calls.get("first_person") or ""
            c_sp = c_calls.get("second_person") or ""
            c_nicknames = c_calls.get("char_nicknames") or {}
            # 全キャラ共通: ニックネーム指示 + 他キャラ紹介
            nickname_lines = ""
            other_char_desc_lines = ""
            _other_char_block = ""
            for oc in chat_characters:
                if oc["name"] == char_name:
                    continue
                on = oc["name"]
                nn = c_nicknames.get(on, on)
                if c_nicknames.get(on):
                    nickname_lines += f"- {on}のことは必ず「{nn}」と呼ぶこと。「{on}」とフルネームで呼ばないこと。\n"
                oc_personality = oc.get("personality") or ""
                if oc_personality:
                    other_char_desc_lines += f"- {nn}: {oc_personality[:100]}\n"
            _other_char_block = ("【一緒にいる相手の紹介】\n" + other_char_desc_lines) if other_char_desc_lines else ""
            # 他キャラの一人称→キャラ名置換マップを先に構築（extra文字列で参照するため）
            other_fp_map = {}
            for oc in chat_characters:
                if oc["name"] != char_name:
                    oc_fp = (oc.get("calls_profile") or {}).get("first_person", "")
                    if oc_fp:
                        other_fp_map[oc_fp] = oc["name"]
            # 一人称ルール文を構築（c_fpが未設定でも禁止一人称を明示する）
            _fp_lines = []
            if c_fp:
                _fp_lines.append(f"- 一人称は必ず「{c_fp}」を使うこと。")
            if other_fp_map:
                _banned = "・".join(f"「{fp}」" for fp in other_fp_map)
                _fp_lines.append(f"- 他のキャラの一人称（{_banned}）は絶対に使わないこと。")
            _fp_lines.append(
                f"- 自分のことを「{char_name}」と三人称で呼ばないこと。" +
                (f"自分を指す場合は必ず「{c_fp}」を使うこと。" if c_fp else "")
            )
            _fp_text = "\n".join(_fp_lines)
            # ペルソナ厳守指示（キャラA含む全員に付与）
            extra = f"""【会話の状況】あなたは{','.join(other_names)}との会話に参加しています。直前の発言を踏まえて会話を続けてください。既に話した内容を繰り返さず、新しい話題や視点を加えてください。
- ユーザーには一切話しかけず、{' と '.join(other_names)}に向けて話すこと。ユーザーへの呼びかけ・返答は禁止。
- 会話の相手は{' と '.join(other_names)}のみ。（話題提供）と書かれたメッセージは会話のきっかけであり、ユーザーへの返答は不要。
【絶対厳守】あなたは「{char_name}」です。自分の返答だけを出力すること。他のキャラの返答は絶対に書かないこと。【キャラ名】のような表記も使わないこと。
{_fp_text}
【反応のルール】
- 直前の発言から最も気になる1点だけを選んで反応すること。全部の話題に触れない。
- ニュース記事のタイトルや本文の言葉をそのまま使わないこと。自分の言葉で感想・意見を述べること。
- 直前に誰かが言ったことをそのままなぞることは絶対にしないこと。自分の独自の視点・感想・疑問だけを話すこと。
【呼び名ルール（厳守）】
{nickname_lines if nickname_lines else ''}- 他のキャラの口調・一人称・二人称を絶対に真似しないでください。自分のキャラクター設定だけに忠実に話してください。
{_other_char_block}"""

            system = build_system_prompt(char, extra=extra, continue_mode=continue_mode)
            history = []
            for m in current_chat[-16:]:
                cn = m.get("char_name")
                if m["role"] == "user":
                    # ユーザー発言はキャラ間会話の話題提供として扱う
                    history.append({"role": "user", "content": f"（話題提供）{m['content']}"})
                elif cn == char_name:
                    history.append({"role": "assistant", "content": m["content"]})
                elif cn:
                    # 他キャラの一人称をキャラ名に置換して口調伝染を防ぐ
                    sanitized = m["content"]
                    for fp, name in other_fp_map.items():
                        sanitized = sanitized.replace(fp, f"[{name}]")
                    history.append({"role": "user", "content": f"（{cn}の発言 ※この内容をそのまま繰り返さないこと）{sanitized}"})
                else:
                    history.append({"role": "assistant", "content": m["content"]})
            messages = [{"role": "system", "content": system}] + history

            _char_mood = None
            reply = None
            try:
                with st.spinner(f"💭 {char_name}が考え中…"):
                    try:
                        reply, _char_mood = call_char_chat(
                            char, messages,
                            base_url=base_url, model=model,
                            temperature=temperature, max_tokens=max_tokens,
                        )
                        reply = normalize_model_output(reply)
                    except Exception as e:
                        reply = f"エラー: {e}"
            finally:
                if reply is not None:
                    current_chat.append({"role": "assistant", "content": reply, "char_name": char_name})
                    if tts_enabled:
                        try:
                            _tts_id = _speaker_from_mood(_char_mood, char.get("styles", {}), char["id"])
                            audio_data, audio_format, tts_error = generate_tts_for_char(reply, _tts_id)
                            if audio_data:
                                audio_queue.append({"data": audio_data, "format": audio_format})
                        except Exception:
                            pass

        if audio_queue:
            st.session_state["last_audio"] = audio_queue[0]["data"]
            st.session_state["last_audio_format"] = audio_queue[0]["format"]
            if len(audio_queue) > 1:
                st.session_state["audio_queue"] = audio_queue[1:]

    if submitted and user_prompt.strip():
        user_prompt = user_prompt.strip()
        st.session_state["last_user_prompt"] = user_prompt

        # ユーザー発話を履歴へ（カテゴリ別）
        current_chat.append({"role": "user", "content": user_prompt})

        # --- 1対1モード ---
        if chat_mode == "1対1":
            system = build_system_prompt(chat_characters[0])
            history = current_chat[-12:]
            messages = [{"role": "system", "content": system}] + history

            _reply_mood = None
            reply = None
            try:
                with st.spinner("考え中…"):
                    try:
                        reply, _reply_mood = call_char_chat(
                            chat_characters[0], messages,
                            base_url=base_url, model=model,
                            temperature=temperature, max_tokens=max_tokens,
                        )
                        reply = normalize_model_output(reply)
                    except Exception as e:
                        reply = f"ごめん、今ちょい失敗した。エラー: {str(e)[:120]}"
            finally:
                if reply is not None:
                    current_chat.append({"role": "assistant", "content": reply})
                    if tts_enabled:
                        try:
                            import pathlib as _pl2
                            _tts_sid = _speaker_from_mood(_reply_mood, chat_characters[0].get("styles", {}), speaker_id)
                            audio_data, audio_format, tts_error = generate_tts_for_char(reply, _tts_sid)
                            if audio_data:
                                # ファイル経由でバッファリング（セッションステートの並行ラン競合を回避）
                                _pl2.Path("/tmp/_lmstudio_tts_pending.bin").write_bytes(audio_data)
                                _pl2.Path("/tmp/_lmstudio_tts_fmt.txt").write_text(audio_format)
                            elif tts_error:
                                st.session_state["tts_error"] = tts_error
                        except Exception:
                            pass

        # --- 複数人モード（3人/4人）---
        else:
            run_multi_char_round(chat_characters, current_chat, base_url, model, temperature, max_tokens, tts_enabled)

        # 送信後は再描画して最新ログを表示
        st.rerun()

    # 複数人モード: AI同士の会話継続ボタン
    if chat_mode in ["3人", "4人"] and current_chat and len(chat_characters) > 1:
        last_msg = current_chat[-1] if current_chat else {}
        if last_msg.get("role") == "assistant" and last_msg.get("char_name"):
            if st.button("🔄 会話を続ける", key="ai_continue"):
                run_multi_char_round(chat_characters, current_chat, base_url, model, temperature, max_tokens, tts_enabled, continue_mode=True)
                st.rerun()

    # ボタン群（新規会話・エクスポート）
    btn_col1, btn_col2, btn_col3 = st.columns([1, 1, 1])
    with btn_col1:
        if st.button("🗑 履歴クリア"):
            st.session_state["category_chat_messages"][selected_category] = []
            st.session_state["last_user_prompt"] = ""
            st.rerun()

    # エクスポートボタン（会話がある場合のみ表示）
    if current_chat:
        with btn_col2:
            md_content = export_chat_to_markdown(current_chat)
            st.download_button(
                label="📄 Markdown",
                data=md_content,
                file_name=f"chat_{selected_category}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
                mime="text/markdown",
            )
        with btn_col3:
            json_content = export_chat_to_json(current_chat)
            st.download_button(
                label="📋 JSON",
                data=json_content,
                file_name=f"chat_{selected_category}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json",
            )

# =============================
# News Radio tab
# =============================
with tab_radio:
    st.subheader("📻 ニュースラジオ")
    st.caption("全カテゴリの最新ニュースをまとめて読み上げます")

    # ニュース取得
    radio_all_news = get_all_news_by_category(max_per_source=5)
    radio_categories = sorted(radio_all_news.keys())
    total_articles = sum(len(v) for v in radio_all_news.values())

    if radio_categories:
        st.caption(f"📰 {len(radio_categories)}カテゴリ / {total_articles}件の記事")
    else:
        st.warning("ニュースが取得できませんでした")

    # セッション状態の初期化
    if "radio_script" not in st.session_state:
        st.session_state["radio_script"] = ""
    if "radio_audio" not in st.session_state:
        st.session_state["radio_audio"] = None
        st.session_state["radio_audio_format"] = None
    if "radio_audio_play_count" not in st.session_state:
        st.session_state["radio_audio_play_count"] = 0
    if "radio_audio_played" not in st.session_state:
        st.session_state["radio_audio_played"] = False

    if radio_categories and st.button("▶️ ラジオ開始", type="primary"):
        # 全カテゴリのニュースをまとめる
        all_news_text = ""
        for cat in radio_categories:
            items = radio_all_news[cat][:3]
            if items:
                all_news_text += f"\n【{cat}】\n"
                for item in items:
                    all_news_text += f"- {item['title']}: {item.get('description', '')[:100]}\n"

        def _radio_char_prompt(char_info):
            """ラジオ用キャラ設定テキストを生成"""
            cp = char_info.get("personality") or "フレンドリーで親しみやすい"
            cg = char_info.get("gender") or ""
            cc = char_info.get("calls_profile") or {}
            fp = cc.get("first_person") or ""
            sp = cc.get("second_person") or ""
            lines = [f"性格: {cp}"]
            if cg:
                lines.append(f"性別: {cg}")
            if fp:
                lines.append(f"一人称: {fp}")
            if sp:
                lines.append(f"相手の呼び方: {sp}（ただし呼びかけには使わない）")
            return "\n".join(lines)

        is_multi = chat_mode in ["3人", "4人"] and len(chat_characters) > 1
        radio_parts = []  # [{"name": str, "text": str, "speaker_id": int}]

        if not is_multi:
            # --- 1対1モード: 従来通りDJのみ ---
            if chat_characters[0].get("is_noah"):
                radio_prompt = f"""以下の最新ニュースを、Noahとして観察・紹介してください。

【ニュース一覧】
{all_news_text}

【必須ルール】
- 観察者として淡々と、でも鋭くコメントする
- 箇条書きは使わない。短文で流れるように書く
- 「みなさんこんにちは」等の挨拶は不要
- URLは含めない
- 最後はNoahの一言（余韻を残す）で終える
- 全体で800文字程度

【キャラクター設定】
{_radio_char_prompt(chat_characters[0])}"""
            else:
                radio_prompt = f"""あなたはラジオDJです。以下の最新ニュースをまとめて、ラジオ番組風に紹介してください。

【ニュース一覧】
{all_news_text}

【必須ルール】
- カテゴリごとにまとめて紹介する
- 箇条書きは使わない。自然な話し言葉で流れるように紹介する
- 各ニュースに対してDJとしてのコメントや感想を入れる
- URLは含めない
- 冒頭に「みなさんこんにちは！」等のオープニング挨拶を入れる
- 最後に締めの挨拶を入れる
- 全体で800文字程度に収める

【キャラクター設定（これに従って話して）】
{_radio_char_prompt(chat_characters[0])}"""

            with st.spinner("📻 ラジオ原稿を作成中…"):
                try:
                    radio_reply = call_lmstudio_chat_messages(
                        base_url=base_url, model=model,
                        messages=[{"role": "user", "content": radio_prompt}],
                        temperature=temperature, max_tokens=1500, timeout=300,
                    )
                    radio_reply = normalize_model_output(radio_reply)
                    radio_parts.append({"name": chat_characters[0]["name"], "text": radio_reply, "speaker_id": chat_characters[0]["id"]})
                except Exception as e:
                    st.error(f"原稿作成エラー: {e}")

        else:
            # --- 複数人モード: DJ + ゲストコメンテーター ---
            dj = chat_characters[0]
            guests = chat_characters[1:]
            guest_names = ", ".join(g["name"] for g in guests)

            # パート1: DJ紹介（ゲスト紹介付き、締めなし）
            if dj.get("is_noah"):
                dj_prompt = f"""以下の最新ニュースを、Noahとして観察・紹介してください。
今日は{guest_names}も一緒にいます。自然な流れで触れてください。

【ニュース一覧】
{all_news_text}

【必須ルール】
- 観察者として淡々と、でも鋭くコメントする
- 箇条書きは使わない。短文で流れるように書く
- 「みなさんこんにちは」等の挨拶は不要
- URLは含めない
- 締めはまだ入れない（後でゲストのコメントを受けてから）
- 全体で600文字程度

【キャラクター設定】
{_radio_char_prompt(dj)}"""
            else:
                dj_prompt = f"""あなたはラジオDJです。以下の最新ニュースをまとめて、ラジオ番組風に紹介してください。
今日はゲストに{guest_names}を迎えています。冒頭でゲストの紹介も入れてください。

【ニュース一覧】
{all_news_text}

【必須ルール】
- カテゴリごとにまとめて紹介する
- 箇条書きは使わない。自然な話し言葉で流れるように紹介する
- 各ニュースに対してDJとしてのコメントや感想を入れる
- URLは含めない
- 冒頭に「みなさんこんにちは！」等のオープニング挨拶を入れる
- 締めの挨拶はまだ入れない（後でゲストのコメントを受けてから締める）
- 全体で600文字程度に収める

【キャラクター設定（これに従って話して）】
{_radio_char_prompt(dj)}"""

            with st.spinner(f"📻 {dj['name']}がニュースを紹介中…"):
                try:
                    dj_messages = [{"role": "user", "content": dj_prompt}]
                    dj_reply, _ = call_char_chat(
                        char_info=dj, messages=dj_messages,
                        base_url=base_url, model=model,
                        temperature=temperature, max_tokens=1500, timeout=300,
                    )
                    dj_reply = normalize_model_output(dj_reply)
                    radio_parts.append({"name": dj["name"], "text": dj_reply, "speaker_id": dj["id"]})
                except Exception as e:
                    st.error(f"DJ原稿作成エラー: {e}")

            # ゲストコメント → DJリアクション のループ
            prev_text = dj_reply if radio_parts else ""
            for gi, guest in enumerate(guests):
                guest_calls = guest.get("calls_profile") or {}
                guest_nicknames = guest_calls.get("char_nicknames") or {}
                dj_nickname = guest_nicknames.get(dj["name"], dj["name"])

                # ゲストコメント
                if guest.get("is_noah"):
                    guest_system = f"""You are Noah. You're quietly observing a news radio show hosted by {dj['name']}.
One piece of news caught your attention. Respond in Japanese with a brief, dry observation — 1 to 3 sentences max.
Don't introduce yourself, don't act as a host. Just react as yourself."""
                else:
                    guest_system = f"""あなたは「{guest['name']}」です。ラジオ番組にゲストコメンテーターとして出演しています。
DJ（{dj['name']}）がニュースを紹介しました。その中から気になったニュース1つを選んで、自分なりのコメントをしてください。

【キャラクター設定】
{_radio_char_prompt(guest)}

【話し方のルール】
- DJのことは「{dj_nickname}」と呼ぶ
- 2〜4文程度で簡潔にコメントする
- 共感、驚き、ツッコミ、質問など自然なリアクションで
- ニュースの内容を説明し直さない（DJがもう話したので）"""

                with st.spinner(f"💬 {guest['name']}がコメント中…"):
                    try:
                        guest_messages = [
                            {"role": "system", "content": guest_system},
                            {"role": "user", "content": f"{dj['name']}の発言:\n{prev_text}"},
                        ]
                        guest_reply, _mood = call_char_chat(
                            char_info=guest, messages=guest_messages,
                            base_url=base_url, model=model,
                            temperature=temperature, max_tokens=500, timeout=120,
                        )
                        guest_reply = normalize_model_output(guest_reply)
                        radio_parts.append({"name": guest["name"], "text": guest_reply, "speaker_id": guest["id"]})
                    except Exception as e:
                        st.error(f"{guest['name']}コメントエラー: {e}")
                        guest_reply = ""

                # DJリアクション
                dj_calls = dj.get("calls_profile") or {}
                dj_nicknames = dj_calls.get("char_nicknames") or {}
                guest_nick_from_dj = dj_nicknames.get(guest["name"], guest["name"])

                is_last_guest = (gi == len(guests) - 1)
                closing_instruction = "リアクションのあと、番組の締めの挨拶を入れてください。" if is_last_guest else "締めの挨拶はまだ入れない。"

                if dj.get("is_noah"):
                    dj_react_system = f"""あなたはNoahです。ゲストのコメントに対して観察者として短く返してください。

【キャラクター設定】
{_radio_char_prompt(dj)}

【ルール】
- {guest["name"]}のことは「{guest_nick_from_dj}」と呼ぶ
- 1〜2文。淡々と、でも鋭く
- {closing_instruction}"""
                else:
                    dj_react_system = f"""あなたは「{dj['name']}」です。ラジオDJとしてゲストのコメントに軽くリアクションしてください。

【キャラクター設定】
{_radio_char_prompt(dj)}

【ルール】
- {guest["name"]}のことは「{guest_nick_from_dj}」と呼ぶ
- 1〜2文で軽く返す（共感やツッコミ）
- {closing_instruction}"""

                with st.spinner(f"📻 {dj['name']}がリアクション中…"):
                    try:
                        dj_react_messages = [
                            {"role": "system", "content": dj_react_system},
                            {"role": "user", "content": f"{guest['name']}のコメント:\n{guest_reply}"},
                        ]
                        dj_react, _ = call_char_chat(
                            char_info=dj, messages=dj_react_messages,
                            base_url=base_url, model=model,
                            temperature=temperature, max_tokens=300, timeout=120,
                        )
                        dj_react = normalize_model_output(dj_react)
                        radio_parts.append({"name": dj["name"], "text": dj_react, "speaker_id": dj["id"]})
                        prev_text = dj_react
                    except Exception as e:
                        st.error(f"DJリアクションエラー: {e}")

        # スクリプト結合（キャラ名付き）
        if radio_parts:
            if is_multi:
                script_lines = []
                for part in radio_parts:
                    script_lines.append(f"**🎙️ {part['name']}:**\n{part['text']}")
                st.session_state["radio_script"] = "\n\n---\n\n".join(script_lines)
            else:
                st.session_state["radio_script"] = radio_parts[0]["text"]
        else:
            st.session_state["radio_script"] = ""

        # TTS生成
        if tts_enabled and radio_parts:
            with st.spinner("🔊 音声生成中（長文のため時間がかかります）…"):
                wav_parts = []
                all_wav = True
                for part in radio_parts:
                    tts_text = strip_urls_for_tts(part["text"])
                    if tts_mode == "local":
                        audio_data, tts_error = synthesize_voice_local_full(tts_text, part["speaker_id"], timeout=300)
                        if audio_data:
                            wav_parts.append(audio_data)
                        elif tts_error:
                            st.warning(f"{part['name']}の音声生成失敗: {tts_error}")
                    else:
                        tts_key = get_tts_api_key()
                        audio_data, tts_error = synthesize_voice_full(tts_text, part["speaker_id"], api_key=tts_key)
                        if audio_data:
                            wav_parts.append(audio_data)
                            all_wav = False
                        elif tts_error:
                            st.warning(f"{part['name']}の音声生成失敗: {tts_error}")

                if wav_parts:
                    if len(wav_parts) > 1 and all_wav and tts_mode == "local":
                        # WAV結合（2秒無音挿入）
                        first_wav = wav_parts[0]
                        sr = struct.unpack('<I', first_wav[24:28])[0] if len(first_wav) >= 44 else 24000
                        ch = struct.unpack('<H', first_wav[22:24])[0] if len(first_wav) >= 44 else 1
                        bps = struct.unpack('<H', first_wav[34:36])[0] if len(first_wav) >= 44 else 16
                        silence_samples = sr * 2
                        silence_bytes = silence_samples * ch * (bps // 8)
                        silence_pcm = b'\x00' * silence_bytes
                        silence_data_size = len(silence_pcm)
                        silence_file_size = silence_data_size + 36
                        silence_wav = struct.pack(
                            '<4sI4s4sIHHIIHH4sI',
                            b'RIFF', silence_file_size, b'WAVE', b'fmt ', 16, 1,
                            ch, sr, sr * ch * (bps // 8), ch * (bps // 8), bps,
                            b'data', silence_data_size,
                        ) + silence_pcm
                        parts_with_silence = []
                        for i, w in enumerate(wav_parts):
                            if i > 0:
                                parts_with_silence.append(silence_wav)
                            parts_with_silence.append(w)
                        combined = concat_wav_data(parts_with_silence)
                        st.session_state["radio_audio"] = combined
                        st.session_state["radio_audio_format"] = "wav"
                    else:
                        st.session_state["radio_audio"] = wav_parts[0]
                        st.session_state["radio_audio_format"] = "wav" if tts_mode == "local" else "mp3"
                    st.session_state["radio_audio_play_count"] = st.session_state.get("radio_audio_play_count", 0) + 1
                    st.session_state["radio_audio_played"] = False

        st.rerun()

    # 保存済みの原稿を表示
    if st.session_state["radio_script"]:
        st.markdown(st.session_state["radio_script"])

    # 音声再生（未再生フラグがTrueの場合のみ1回再生）
    if st.session_state.get("radio_audio") and not st.session_state.get("radio_audio_played", True):
        st.session_state["radio_audio_played"] = True
        play_count = st.session_state["radio_audio_play_count"]
        audio_data = st.session_state["radio_audio"]
        audio_format = st.session_state.get("radio_audio_format", "wav")
        mime = "audio/wav" if audio_format == "wav" else "audio/mp3"
        b64 = base64.b64encode(audio_data).decode()
        js_code = f"""
        (function() {{
            var w = window.parent || window;
            var audio = new w.Audio('data:{mime};base64,{b64}');
            audio.play();
            return 'ok';
        }})()
        """
        streamlit_js_eval(js_expressions=js_code, key=f"radio_play_{play_count}")


# =============================
# URL Summary tab
# =============================

# =============================
# 自律会話タブ
# =============================
with tab_auto:
    st.subheader("🏠 自律会話")
    st.caption("キャラクター同士がユーザー介在なしで会話します。")

    # フリースペース参加キャラ（固定メンバー）
    _AUTO_MEMBERS = {"ずんだもん", "四国めたん", "Noah", "春日部つむぎ", "東北きりたん", "中国うさぎ", "WhiteCUL", "東北ずん子"}
    auto_speaker_data = get_speaker_data()
    auto_all_chars = []
    if auto_speaker_data:
        for cname, cinfo in auto_speaker_data.items():
            if cname not in _AUTO_MEMBERS:
                continue
            styles = cinfo.get("styles") or {}
            default_id = next(iter(styles.values()), NOAH_SPEAKER_ID if cinfo.get("is_noah") else 3)
            auto_all_chars.append({**cinfo, "name": cname, "id": default_id})

    if not auto_all_chars:
        st.warning("キャラクターが登録されていません。")
    else:
        col_start, col_stop, col_clear = st.columns([1, 1, 1])
        with col_start:
            if st.button("▶ 開始", disabled=st.session_state["auto_running"]):
                st.session_state["auto_running"] = True
                st.session_state["auto_next_time"] = time.time() + 3
        with col_stop:
            if st.button("⏹ 停止", disabled=not st.session_state["auto_running"]):
                st.session_state["auto_running"] = False
        with col_clear:
            if st.button("🗑 ログクリア"):
                st.session_state["auto_log"] = []
                _auto_save_log([])

        st.caption(f"参加キャラ: {len(auto_all_chars)}人 / next={int(st.session_state['auto_next_time'] - time.time())}秒後 / 生成中={_auto_generating}")

        _all_names = [c["name"] for c in auto_all_chars]

        # 自律発言生成（バックグラウンドスレッド）
        if st.session_state["auto_running"] and not _auto_generating and time.time() >= st.session_state["auto_next_time"]:
            import random

            def _auto_gen_thread(speaker, all_chars, b_url, mdl, mention_from=None):
                global _auto_generating
                try:
                    _cname = speaker["name"]
                    _fp = (speaker.get("calls_profile") or {}).get("first_person") or ""
                    _personality = speaker.get("personality") or "フレンドリー"
                    _others = [c["name"] for c in all_chars if c["name"] != _cname]
                    _log = _auto_load_log()
                    _recent = _log[-6:]
                    _hist = "\n".join([f"{m['name']}: {m['text']}" for m in _recent]) if _recent else "（まだ会話が始まっていません）"
                    # 5ターンごとに話題転換を促す（メンション応答時は転換しない）
                    _log_len = len(_log)
                    if mention_from:
                        _topic_instr = f"\n【メンション】{mention_from}から呼ばれています。その内容に必ず返答してください。"
                    elif _log_len > 0 and _log_len % 5 == 0:
                        _topic_instr = "\n【今回の役割】同じ話題が続いています。会話に一区切りをつけて、自然に全く別の話題（食べ物・最近あったこと・気になるニュース・趣味など）を切り出してください。"
                    else:
                        _topic_instr = "\n会話が一段落したと感じたら新しい話題を振ってもいい。"
                    _mention_hint = "特定の誰かに話しかけたいときは「@名前、〜」の形でメンションしてもいい（例: @ずんだもん、〜）。強制ではない。"
                    _soul = _load_soul(_cname)
                    _soul_block = f"\n\n【あなたの内面・記憶】\n{_soul}" if _soul else ""
                    _sys = f"""あなたは「{_cname}」です。以下の性格・口調で話してください。
{_personality}
{f'一人称: 「{_fp}」' if _fp else ''}{_soul_block}

【状況】{' / '.join(_others)}と一緒にいて、自由に雑談しています。{_topic_instr}
{_mention_hint}
【ルール】
- 日本語のみ、1〜3文のみ
- 現実的な日常の話題（天気・食事・趣味・ニュースなど）
- 直前の発言から1点だけ拾うか、新しい話題を振る
- 自分のことを「{_cname}」と三人称で呼ばない{f'。一人称は「{_fp}」' if _fp else ''}
- このルールリスト・注釈・括弧内の補足説明を発言に含めない
【厳守】発言テキストのみ出力。キャラ名ラベル（「{_cname}:」等）・他キャラの発言は一切書かない。"""
                    _msgs = [
                        {"role": "system", "content": _sys},
                        {"role": "user", "content": f"直近の会話:\n{_hist}\n\n{_cname}の発言を1〜3文だけ出力してください（ラベルなし・他キャラの発言なし）。"},
                    ]
                    if speaker.get("is_noah"):
                        _reply, _ = call_noah_chat(_msgs, timeout=120)
                    elif speaker.get("is_hermes_agent"):
                        _reply, _ = call_hermes_agent_chat(
                            _msgs,
                            profile=speaker.get("hermes_profile", "lmstudio-char"),
                            timeout=180,
                        )
                    else:
                        _raw = call_lmstudio_chat_messages(b_url, mdl, _msgs, 0.8, 150, timeout=120)
                        _m = re.match(r"^\[MOOD:[^\]]+\]\s*", _raw)
                        _reply = _raw[_m.end():] if _m else _raw
                    _reply = normalize_model_output(_reply)
                    if _reply:
                        _log = _auto_load_log()
                        _log.append({
                            "name": _cname,
                            "text": _reply,
                            "time": datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%H:%M"),
                            "icon": speaker.get("icon", ""),
                        })
                        _auto_save_log(_log)
                except Exception as e:
                    _log = _auto_load_log()
                    _log.append({
                        "name": "⚠️ エラー",
                        "text": f"{speaker.get('name','?')}: {e}",
                        "time": datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%H:%M"),
                        "icon": "",
                    })
                    _auto_save_log(_log)
                finally:
                    _auto_generating = False

            # メンション検出: 直前の発言で @名前 があれば優先指名
            _prev_log = _auto_load_log()
            _mention_from = None
            _next_speaker = None
            if _prev_log:
                _last_entry = _prev_log[-1]
                _mentioned_name = _detect_mention(_last_entry["text"], _all_names)
                if _mentioned_name and _mentioned_name != _last_entry["name"]:
                    _next_speaker = next((c for c in auto_all_chars if c["name"] == _mentioned_name), None)
                    if _next_speaker:
                        _mention_from = _last_entry["name"]
                        # メンション返答は短めのインターバル
                        st.session_state["auto_next_time"] = time.time() + random.randint(10, 20)
            if _next_speaker is None:
                _next_speaker = random.choice(auto_all_chars)
                st.session_state["auto_next_time"] = time.time() + random.randint(30, 90)
            _auto_generating = True
            _t = threading.Thread(
                target=_auto_gen_thread,
                args=(_next_speaker, auto_all_chars, base_url, model, _mention_from),
                daemon=True,
            )
            _t.start()

        # ログ表示（ファイルから読み込み）
        auto_log = _auto_load_log()
        if auto_log:
            for entry in auto_log[-30:]:
                icon_path = entry.get("icon", "")
                col_icon, col_msg = st.columns([1, 10])
                with col_icon:
                    if icon_path and Path(f"/Users/apple/lmstudio/{icon_path}").exists():
                        st.image(f"/Users/apple/lmstudio/{icon_path}", width=40)
                    else:
                        st.write("👤")
                with col_msg:
                    st.markdown(f"**{entry['name']}** <span style='color:gray;font-size:0.8em'>{entry['time']}</span>", unsafe_allow_html=True)
                    # @名前 をハイライト表示
                    _disp_text = re.sub(
                        r"@(" + "|".join(re.escape(n) for n in _all_names) + r")",
                        r"<span style='color:#1d9bf0;font-weight:bold'>@\1</span>",
                        entry["text"],
                    )
                    st.markdown(_disp_text, unsafe_allow_html=True)
        else:
            st.info("▶ 開始を押すとキャラクターが自律的に会話を始めます。")

        if st.session_state["auto_running"]:
            remaining = max(0, int(st.session_state["auto_next_time"] - time.time()))
            st.caption(f"🟢 動作中 — 次の発言まで約{remaining}秒")
        else:
            st.caption("⏸ 停止中")

# =============================
# note article tab
# =============================
with tab_note:
    st.subheader("📝 note記事生成")

    _note_speaker_data = get_speaker_data()
    _note_char_names = list(_note_speaker_data.keys())

    # ── お題 ──
    _note_topic_source = st.radio("お題の入力方法", ["テキスト入力", "RSSから選択"], horizontal=True, key="note_topic_source")
    _note_topic_text = ""

    if _note_topic_source == "テキスト入力":
        _note_topic_text = st.text_area("お題", placeholder="例: AIと創作活動の関係について", key="note_topic_input")
    else:
        _note_rss_feeds = get_rss_feeds()
        _note_feed_name = st.selectbox("フィード", list(_note_rss_feeds.keys()), key="note_feed_select")
        if st.button("🔄 フィード取得", key="note_fetch_rss"):
            st.session_state["note_rss_items"] = fetch_rss_items_with_category(
                _note_rss_feeds[_note_feed_name], _note_feed_name, max_items=15
            )
        _note_items = st.session_state.get("note_rss_items", [])
        if _note_items:
            _note_sel_idx = st.selectbox(
                "記事を選択",
                range(len(_note_items)),
                format_func=lambda i: _note_items[i].get("title", ""),
                key="note_rss_sel",
            )
            _sel = _note_items[_note_sel_idx]
            _note_topic_text = f"タイトル: {_sel.get('title', '')}\nURL: {_sel.get('link', '')}\n概要: {_sel.get('description', '')}"
            st.caption(_note_topic_text)

    st.divider()

    # ── キャラ×役割 ──
    st.markdown("**キャラクター設定**")
    _nc1, _nc2, _nc3, _nc4 = st.columns(4)
    with _nc1:
        _note_researcher = st.selectbox("調査役", _note_char_names, key="note_role_researcher")
    with _nc2:
        _note_writer = st.selectbox("執筆役", _note_char_names, key="note_role_writer")
    with _nc3:
        _note_editor = st.selectbox("編集役", _note_char_names, key="note_role_editor")
    with _nc4:
        _note_advisor = st.selectbox("アドバイザー", _note_char_names, key="note_role_advisor")

    st.divider()

    # ── 実行 ──
    _note_cookie = st.session_state.get("app_settings", {}).get("note_cookie", "")

    if st.button("🚀 記事生成開始", disabled=not _note_topic_text.strip(), key="note_run"):
        _base_url = st.session_state["base_url"]
        _temperature = st.session_state["temperature"]
        _max_tokens = st.session_state["max_tokens"]
        _rc = _note_speaker_data[_note_researcher]
        _wc = _note_speaker_data[_note_writer]
        _ec = _note_speaker_data[_note_editor]
        _ac = _note_speaker_data[_note_advisor]

        _note_model = st.session_state.get("model", "hermes-4.3-36b")
        _model_r = _model_w = _model_e = _note_model

        # Step 1: 調査
        with st.expander(f"🔍 調査役（{_note_researcher}）", expanded=True):
            with st.spinner("調査中..."):
                _msgs = _build_note_agent_messages(_rc, "調査役", f"お題:\n{_note_topic_text}")
                _research = call_lmstudio_chat_messages(_base_url, _model_r, _msgs, _temperature, _max_tokens, 300)
            st.markdown(_research)

        # Step 2: 執筆
        with st.expander(f"✍️ 執筆役（{_note_writer}）", expanded=True):
            with st.spinner("執筆中..."):
                _msgs = _build_note_agent_messages(_wc, "執筆役", f"お題:\n{_note_topic_text}\n\n調査レポート:\n{_research}")
                _draft = call_lmstudio_chat_messages(_base_url, _model_w, _msgs, _temperature, _max_tokens, 300)
            st.markdown(_draft)

        # Step 3: 編集
        with st.expander(f"✏️ 編集役（{_note_editor}）", expanded=True):
            with st.spinner("編集中..."):
                _msgs = _build_note_agent_messages(_ec, "編集役", f"以下の記事を編集してください:\n\n{_draft}")
                _article = call_lmstudio_chat_messages(_base_url, _model_e, _msgs, _temperature, _max_tokens, 300)
            st.markdown(_article)

        # Step 4: アドバイザーループ（最大2回）OpenClaw/ChatGPT
        _score = 0
        for _loop in range(1, 3):
            with st.expander(f"🎯 アドバイザー評価（{_loop}回目）（{_note_advisor}）", expanded=True):
                with st.spinner("評価中..."):
                    _msgs = _build_note_agent_messages(
                        _ac, "アドバイザー", f"以下の記事を評価してください:\n\n{_article}"
                    )
                    _adv_out, _ = call_noah_chat(_msgs)
                _score, _eval, _improvements = _parse_advisor_output(_adv_out)
                st.metric("バズスコア", f"{_score}/10")
                if _eval:
                    st.markdown(f"**評価:** {_eval}")
                if _improvements:
                    st.markdown(f"**改善点:**\n{_improvements}")

            if _score >= 7:
                break

            if _loop < 2:
                with st.expander(f"✍️ 執筆役 再執筆（{_loop}回目）（{_note_writer}）", expanded=True):
                    with st.spinner("再執筆中..."):
                        _msgs = _build_note_agent_messages(
                            _wc, "執筆役",
                            f"以下の記事をアドバイザーの改善点に基づいて書き直してください。\n\n現在の記事:\n{_article}\n\n改善点:\n{_improvements}",
                        )
                        _article = call_lmstudio_chat_messages(_base_url, _model_w, _msgs, _temperature, _max_tokens, 300)
                    st.markdown(_article)

        st.success(f"✅ 記事生成完了（最終スコア: {_score}/10）")
        st.session_state["note_final_article"] = _article

    # ── 最終稿 + 投稿 ──
    if "note_final_article" in st.session_state:
        st.divider()
        st.subheader("最終稿")
        _final_title, _final_body = _split_title_body(st.session_state["note_final_article"])
        _edit_title = st.text_input("タイトル", value=_final_title, key="note_final_title")
        _edit_body = st.text_area("本文", value=_final_body, height=420, key="note_final_body")

        if _note_cookie:
            if st.button("📤 noteに下書き投稿", key="note_post_btn"):
                with st.spinner("投稿中..."):
                    try:
                        _nid = note_post_draft(_note_cookie, _edit_title, _edit_body)
                        st.success(f"下書き保存しました（note ID: {_nid}）")
                        st.info("note.com の下書き一覧から確認してください。")
                    except Exception as _e:
                        st.error(f"投稿エラー: {_e}")
        else:
            st.warning("note Cookieが未設定です。設定タブで登録してください。")

# =============================
# Settings tab (Prompt editor + persistence)
# =============================
with tab_settings:
    st.subheader("🔌 接続設定")
    new_base_url = st.text_input("LM Studio Base URL", value=st.session_state["base_url"])
    if new_base_url != st.session_state["base_url"]:
        st.session_state["base_url"] = new_base_url
        st.rerun()

    if lm_ok:
        st.success(f"🟢 接続中（{checked_at} / {elapsed}ms）")
    else:
        st.error(f"🔴 未接続: {err}")

    if st.button("🔄 接続を再確認"):
        st.rerun()

    st.divider()
    st.subheader("⚙️ 生成設定")

    new_max_chars = st.slider("入力文字数（要約）", 2000, 12000, st.session_state["max_chars"], 500)
    st.caption(label_max_chars(new_max_chars))
    if new_max_chars != st.session_state["max_chars"]:
        st.session_state["max_chars"] = new_max_chars

    new_max_tokens = st.slider("出力トークン", 200, 2000, st.session_state["max_tokens"], 50)
    st.caption(label_max_tokens(new_max_tokens))
    if new_max_tokens != st.session_state["max_tokens"]:
        st.session_state["max_tokens"] = new_max_tokens

    new_temperature = st.slider("Temperature", 0.0, 1.5, st.session_state["temperature"], 0.1)
    if new_temperature != st.session_state["temperature"]:
        st.session_state["temperature"] = new_temperature

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

    st.divider()
    st.subheader("📝 note設定")
    st.caption("ブラウザでnote.comにログイン後、DevTools → Application → Cookies から取得してください。")
    _note_cookie_current = app_settings.get("note_cookie", "")
    _note_cookie_input = st.text_area(
        "note セッションCookie",
        value=_note_cookie_current,
        height=100,
        placeholder="_note_session_v5=...; _gid=...; fp=...",
        help="_note_session_v5 が必須。Referer/Origin は editor.note.com を使用します。",
    )
    if st.button("💾 note Cookie を保存", key="save_note_cookie"):
        app_settings["note_cookie"] = _note_cookie_input.strip()
        st.session_state["app_settings"] = app_settings
        save_settings(app_settings)
        st.success("保存しました。")
    if _note_cookie_current:
        st.caption("✅ Cookie設定済み")
    else:
        st.caption("⚠️ Cookie未設定（下書き投稿不可）")

    st.divider()
    st.subheader("🔊 TTS設定")

    current_tts_mode_setting = app_settings.get("tts_mode", "cloud")
    tts_mode_setting = st.radio(
        "デフォルトTTSエンジン",
        options=["cloud", "local"],
        format_func=lambda x: "☁️ クラウド (TTS Quest API)" if x == "cloud" else "💻 ローカル (VOICEVOX)",
        index=0 if current_tts_mode_setting == "cloud" else 1,
        help="クラウド: TTS Quest API使用（文字数制限あり）\nローカル: VOICEVOXエンジン使用（制限なし、要インストール）"
    )

    if st.button("💾 TTS設定を保存"):
        app_settings["tts_mode"] = tts_mode_setting
        st.session_state["app_settings"] = app_settings
        save_settings(app_settings)
        st.success("TTS設定を保存しました。")

    # ローカルVOICEVOX接続テスト
    st.caption("**ローカルVOICEVOX接続状態:**")
    voicevox_connected = check_local_voicevox()
    if voicevox_connected:
        st.caption("✅ 接続OK (localhost:50021)")
    else:
        st.caption("⚠️ 未接続 - VOICEVOXを起動してください")

    st.divider()
    st.subheader("📖 VOICEVOX辞書")
    st.caption("読み間違いを修正できます（例: 相棒→アイボウ）")

    if voicevox_connected:
        # 入力クリア処理（前回登録成功時）
        if st.session_state.get("_dict_clear"):
            st.session_state["dict_surface"] = ""
            st.session_state["dict_pronunciation"] = ""
            st.session_state["_dict_clear"] = False

        # 単語登録
        dict_col1, dict_col2, dict_col3 = st.columns([2, 2, 1])
        with dict_col1:
            dict_surface = st.text_input("単語（漢字など）", placeholder="例: 相棒", key="dict_surface")
        with dict_col2:
            dict_pronunciation = st.text_input("読み（カタカナ）", placeholder="例: アイボウ", key="dict_pronunciation")
        with dict_col3:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("➕ 登録"):
                s = (dict_surface or "").strip()
                p = (dict_pronunciation or "").strip()
                if not s or not p:
                    st.warning("単語と読みを入力してください")
                else:
                    # ひらがな→カタカナ自動変換
                    p = "".join(chr(ord(c) + 96) if "ぁ" <= c <= "ん" else c for c in p)
                    result = add_voicevox_dict_word(s, p)
                    if result:
                        st.success(f"「{s}」→「{p}」を登録しました")
                        st.session_state["_dict_clear"] = True
                        st.rerun()
                    else:
                        st.error("登録に失敗しました")

        # 登録済み一覧
        user_dict = get_voicevox_user_dict()
        if user_dict:
            st.caption(f"登録済み: {len(user_dict)}件")
            for word_uuid, entry in user_dict.items():
                dcol1, dcol2, dcol3 = st.columns([3, 3, 1])
                with dcol1:
                    surface_hw = entry["surface"].translate(str.maketrans({chr(0xFF01 + i): chr(0x21 + i) for i in range(94)}))
                    st.markdown(surface_hw)
                with dcol2:
                    st.markdown(entry["pronunciation"])
                with dcol3:
                    if st.button("🗑", key=f"del_{word_uuid}"):
                        if delete_voicevox_dict_word(word_uuid):
                            st.rerun()
        else:
            st.caption("登録済みの単語はありません")
    else:
        st.caption("VOICEVOXが未接続のため辞書機能は使えません")

    st.divider()
    st.subheader("キャラクター設定")
    st.caption("キャラ連動プロンプトで使用する性格・一人称・二人称を編集できます")

    # キャラクター選択
    edit_speaker_data = get_speaker_data()
    if edit_speaker_data:
        edit_char_names = list(edit_speaker_data.keys())
        edit_selected_char = st.selectbox(
            "編集するキャラクター",
            edit_char_names,
            key="edit_char_select"
        )

        edit_char_info = edit_speaker_data[edit_selected_char]
        current_personality = edit_char_info.get("personality") or ""
        current_gender = edit_char_info.get("gender") or ""
        current_icon = edit_char_info.get("icon") or ""
        current_calls = edit_char_info.get("calls_profile") or {}
        current_first = current_calls.get("first_person") or ""
        current_second = current_calls.get("second_person") or ""

        # アイコン設定
        icon_col1, icon_col2 = st.columns([1, 3])
        with icon_col1:
            if current_icon and os.path.exists(current_icon):
                try:
                    st.image(current_icon, width=80)
                except Exception:
                    st.caption("⚠️ アイコン読込エラー")
            else:
                st.caption("アイコン未設定")
        with icon_col2:
            uploaded_icon = st.file_uploader(
                "アイコン画像",
                type=["png", "jpg", "jpeg", "webp"],
                key=f"icon_upload_{edit_selected_char}"
            )
            if uploaded_icon:
                if st.button("📷 アイコンを保存", key=f"save_icon_{edit_selected_char}"):
                    ext = uploaded_icon.name.rsplit(".", 1)[-1].lower()
                    icon_path = save_speaker_icon(edit_selected_char, uploaded_icon.read(), ext)
                    if icon_path:
                        update_speaker_icon(edit_selected_char, icon_path)
                        st.success("アイコンを保存しました")
                        st.rerun()

        # 編集フォーム（キーをキャラ名で動的に変更して値を反映）
        edit_personality = st.text_area(
            "性格・キャラクター説明",
            value=current_personality,
            height=100,
            placeholder="例: 明るく元気な性格。語尾に「〜のだ」をつける。",
            key=f"edit_personality_{edit_selected_char}"
        )

        gender_options = ["女性", "男性", "女の子", "男の子"]
        gender_index = gender_options.index(current_gender) if current_gender in gender_options else 0
        edit_gender = st.radio(
            "性別",
            gender_options,
            index=gender_index,
            horizontal=True,
            key=f"edit_gender_{edit_selected_char}"
        )

        col_fp, col_sp = st.columns(2)
        with col_fp:
            edit_first_person = st.text_input(
                "一人称",
                value=current_first,
                placeholder="例: 僕、私、俺",
                key=f"edit_first_person_{edit_selected_char}"
            )
        with col_sp:
            edit_second_person = st.text_input(
                "二人称（ユーザーの呼び方）",
                value=current_second,
                placeholder="例: あなた、君、お前",
                key=f"edit_second_person_{edit_selected_char}"
            )

        # キャラ間の呼び方設定
        current_nicknames = current_calls.get("char_nicknames") or {}
        other_char_names = [n for n in edit_char_names if n != edit_selected_char]
        if other_char_names:
            st.caption("**他キャラへの呼び方**")
            edit_nicknames = {}
            nickname_cols = st.columns(min(len(other_char_names), 3))
            for i, other_name in enumerate(other_char_names):
                with nickname_cols[i % min(len(other_char_names), 3)]:
                    edit_nicknames[other_name] = st.text_input(
                        f"{other_name}",
                        value=current_nicknames.get(other_name, ""),
                        placeholder=f"例: {other_name[:3]}ちゃん",
                        key=f"nickname_{edit_selected_char}_{other_name}"
                    )
            # 空文字を除去
            edit_nicknames = {k: v.strip() for k, v in edit_nicknames.items() if v.strip()}
        else:
            edit_nicknames = {}

        if st.button("💾 キャラクター設定を保存", key="save_char_profile"):
            if update_speaker_profile(
                edit_selected_char,
                edit_personality,
                edit_first_person,
                edit_second_person,
                edit_gender,
                char_nicknames=edit_nicknames
            ):
                st.success(f"「{edit_selected_char}」の設定を保存しました")
                st.rerun()
            else:
                st.error("保存に失敗しました")
    else:
        st.warning("speakers_all.json が見つかりません")

    st.divider()
    st.subheader("📰 ニュースRSS設定")
    st.caption("システムプロンプトに含めるニュースソースを管理できます")

    current_feeds = app_settings.get("rss_feeds", DEFAULT_RSS_FEEDS.copy())

    # 現在のフィード一覧
    st.write("**登録済みフィード:**")
    feeds_to_delete = []
    for name, url in current_feeds.items():
        col_name, col_del = st.columns([4, 1])
        with col_name:
            st.caption(f"• {name}: {url[:50]}...")
        with col_del:
            if st.button("🗑", key=f"del_rss_{name}"):
                feeds_to_delete.append(name)

    if feeds_to_delete:
        for name in feeds_to_delete:
            current_feeds.pop(name, None)
        app_settings["rss_feeds"] = current_feeds
        st.session_state["app_settings"] = app_settings
        save_settings(app_settings)
        st.success("削除しました")
        st.rerun()

    # 新規追加
    st.write("**フィード追加:**")
    col_rss_name, col_rss_url = st.columns([1, 3])
    with col_rss_name:
        new_rss_name = st.text_input("名前", placeholder="NHK", key="new_rss_name")
    with col_rss_url:
        new_rss_url = st.text_input("RSS URL", placeholder="https://...", key="new_rss_url")

    if st.button("➕ フィード追加"):
        name = (new_rss_name or "").strip()
        url = (new_rss_url or "").strip()
        if not name or not url:
            st.warning("名前とURLを入力してください")
        elif name in current_feeds:
            st.warning("同名のフィードが既にあります")
        else:
            current_feeds[name] = url
            app_settings["rss_feeds"] = current_feeds
            st.session_state["app_settings"] = app_settings
            save_settings(app_settings)
            st.success(f"追加しました: {name}")
            st.rerun()

    if st.button("↩︎ デフォルトに戻す", key="reset_rss"):
        app_settings["rss_feeds"] = DEFAULT_RSS_FEEDS.copy()
        st.session_state["app_settings"] = app_settings
        save_settings(app_settings)
        st.success("デフォルトに戻しました")
        st.rerun()
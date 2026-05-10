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
    DEFAULT_UA, NOAH_GATEWAY_URL, NOAH_GATEWAY_TOKEN, _OPENCLAW_WORKSPACE,
    is_chat_model, lmstudio_models, call_lmstudio_chat_messages,
    call_noah_chat, call_char_chat, call_hermes_agent, call_hermes_agent_chat,
    fetch_html, extract_main_text, build_summary_prompt, normalize_model_output,
    _priority_request,
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
    _auto_gen_lock, _auto_state,
    _load_soul, _auto_load_log, _auto_save_log, _detect_mention,
    load_speakers, load_speakers_raw, save_speakers, save_speaker_icon,
    load_noah_config, save_noah_config,
    update_speaker_icon, update_speaker_profile, get_speaker_data,
    parse_soul_affinities, affinity_behavior, sanitize_soul_for_prompt,
    extract_soul_interests, detect_topic_repetition,
    load_episodes, format_episodes_for_prompt,
)
from conversation_controller import (
    CONTROL_RATE, update_conv_state, MovePlanner, build_move_instruction,
    check_output, build_retry_instruction,
)

# 自律会話スレッド状態は speakers._auto_state (rerun-safe mutable dict) を使用

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
st.set_page_config(page_title="ChatRoom", layout="centered")

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
if "topic_change_cooldown" not in st.session_state:
    st.session_state["topic_change_cooldown"] = 0
if "topic_debug_log" not in st.session_state:
    st.session_state["topic_debug_log"] = []
if "auto_log" not in st.session_state:
    st.session_state["auto_log"] = []
if "auto_next_time" not in st.session_state:
    st.session_state["auto_next_time"] = 0.0
if "auto_tts_played_count" not in st.session_state:
    st.session_state["auto_tts_played_count"] = 0

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
    else:
        st.warning("speakers_all.json が見つかりません")
        speaker_id = 3
        speaker_personality = None
        speaker_gender = None
        speaker_calls_profile = None

    st.divider()

    # ② モデル選択
    st.header("🤖 モデル")
    model = st.selectbox("使用モデル", models, label_visibility="collapsed")

    st.divider()

    # ③ 音声読み上げ
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

# autorefresh: 常時固定interval。propsが変化するとリレンダーが発生してボタンが2回必要になるため固定
# 停止中は生成ロジック側でauto_runningをチェックしてスキップする
st_autorefresh(interval=10000, key="auto_refresh_tick")


def _do_noah_feedback(log_entries: list) -> str:
    from datetime import date as _date
    import re as _re
    today = _date.today().strftime("%Y-%m-%d")
    recent = log_entries[-30:]
    hist = "\n".join([f"【{e['name']}】{e['text']}" for e in recent])
    user_content = f"""以下は今日（{today}）の自律会話ログです。Noahとして以下3つのファイルへの追記内容を出力してください。

会話ログ:
{hist}

出力形式（必ずこの形式で）:
<OBSERVATIONS>
（{today}付きでNoah視点の今日の記録を1〜3行）
</OBSERVATIONS>
<KNOWLEDGE>
（会話で出た新知識を1行）
</KNOWLEDGE>
<RELATIONSHIP>
（他キャラとの交流で気づいたことを1行）
</RELATIONSHIP>"""
    try:
        output, _ = call_noah_chat([{"role": "user", "content": user_content}], timeout=60)
    except Exception as e:
        return f"Noah書き戻しエラー: {e}"

    def _extract(tag):
        m = _re.search(rf"<{tag}>(.*?)</{tag}>", output, _re.DOTALL)
        return m.group(1).strip() if m else ""

    results = []
    for fname, tag in [("OBSERVATIONS.md", "OBSERVATIONS"), ("KNOWLEDGE.md", "KNOWLEDGE"), ("RELATIONSHIP.md", "RELATIONSHIP")]:
        content = _extract(tag)
        if not content:
            continue
        fpath = _OPENCLAW_WORKSPACE / fname
        try:
            existing = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
            fpath.write_text(existing.rstrip() + f"\n\n{content}\n", encoding="utf-8")
            results.append(fname)
        except Exception:
            pass
    return f"書き戻し完了: {', '.join(results)}" if results else "書き戻す内容なし"


def _do_soul_updates(log_entries: list, all_chars: list, base_url: str, model: str) -> None:
    import re as _re
    last_10 = log_entries[-10:]
    if not last_10:
        return
    active_names = {e["name"] for e in last_10}
    all_other_names = list({e["name"] for e in last_10})
    for char in all_chars:
        cname = char["name"]
        if cname == "Noah" or cname not in active_names:
            continue
        my_turns = [e for e in last_10 if e["name"] == cname]
        if not my_turns:
            continue
        other_names = [n for n in all_other_names if n != cname]
        my_text = "\n".join([f"- {e['text']}" for e in my_turns])
        soul_path = _SOULS_DIR / f"{cname}.md"
        current_soul = (soul_path.read_text(encoding="utf-8") if soul_path.exists() else
                        f"# {cname} のソウルファイル\n\n## 自己認識\n\n## 最近の関心\n\n## 重要な記憶\n\n## メンバーへの印象\n")
        other_str = "、".join(other_names)
        _episodes = load_episodes(cname, limit=10)
        _ep_text = format_episodes_for_prompt(_episodes)
        _ep_block = f"\n\n直近のエピソード記録（具体的な出来事）:\n{_ep_text}" if _ep_text else ""
        prompt = f"""以下は「{cname}」の直近の発言です:
{my_text}{_ep_block}

現在のソウルファイル:
{current_soul}

上記の発言を踏まえて、ソウルファイルの以下セクションを更新してください（各セクションは3行以内）。

【重要ルール】
- 具体的な出来事・固有名詞・会話の内容をそのまま記録しない
- 発言から読み取れる「{cname}の性質・傾向・価値観」に抽象化して記録する
- 例（NG）:「黒糖アイス作りの買い出しが楽しみ」
- 例（OK）:「みんなで協力して何かを作り上げることに喜びを感じる」

各セクションの意味:
- <最近の関心>: 発言から読み取れる興味・関心の傾向（具体的な話題名ではなく傾向として）
- <重要な記憶>: {cname}の人格形成に関わる体験から導かれた価値観・信念（出来事の記録ではない）
- <メンバーへの印象>: {other_str}への印象（既存も維持しつつ更新）

形式:
<最近の関心>内容</最近の関心>
<重要な記憶>内容</重要な記憶>
<メンバーへの印象>内容</メンバーへの印象>"""
        try:
            msgs = [{"role": "system", "content": f"あなたは{cname}のソウル更新AIです。"},
                    {"role": "user", "content": prompt}]
            output = call_lmstudio_chat_messages(base_url, model, msgs, 0.7, 600, timeout=90, background=True)

            def _replace_section(soul: str, tag: str, new_content: str) -> str:
                m = _re.search(rf"## {tag}\n(.*?)(?=\n## |\Z)", soul, _re.DOTALL)
                if m:
                    return soul[:m.start(1)] + new_content.strip() + "\n" + soul[m.end(1):]
                return soul + f"\n## {tag}\n{new_content.strip()}\n"

            tag_map = {"最近の関心": "最近の関心", "重要な記憶": "重要な記憶", "メンバーへの印象": "メンバーへの印象"}
            updated = current_soul
            for xml_tag, section_name in tag_map.items():
                m = _re.search(rf"<{xml_tag}>(.*?)</{xml_tag}>", output, _re.DOTALL)
                if m:
                    updated = _replace_section(updated, section_name, m.group(1).strip())
            soul_path.write_text(updated, encoding="utf-8")
        except Exception:
            pass


with st.sidebar:
    st.divider()
    with st.expander("🐛 話題転換デバッグ", expanded=False):
        _dbg_log = st.session_state.get("topic_debug_log", [])
        _cd = st.session_state.get("topic_change_cooldown", 0)
        st.caption(f"cooldown残り: {_cd}")
        if _dbg_log:
            for _line in _dbg_log:
                st.caption(_line)
        else:
            st.caption("イベントなし")
        if st.button("クリア", key="dbg_clear"):
            st.session_state["topic_debug_log"] = []

tab_auto, tab_note, tab_autogen, tab_settings = st.tabs(["🏠 自律会話", "📝 note記事", "🤖 AutoGen PoC", "⚙️ 設定"])

with tab_auto:
    st.subheader("🏠 自律会話")
    st.caption("キャラクター同士がユーザー介在なしで会話します。")

    # フリースペース参加キャラ（固定メンバー）
    _AUTO_MEMBERS = {"ずんだもん", "四国めたん", "Noah", "春日部つむぎ", "東北きりたん", "中国うさぎ", "WhiteCUL", "東北ずん子", "雨晴はう", "Hermes", "東北イタコ", "ぞん子"}
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
                _stop_log = _auto_load_log()
                if _stop_log:
                    threading.Thread(target=_do_noah_feedback, args=(_stop_log,), daemon=True).start()
                    threading.Thread(target=_do_soul_updates, args=(_stop_log, auto_all_chars, base_url, model), daemon=True).start()
        with col_clear:
            if st.button("🗑 ログクリア"):
                st.session_state["auto_log"] = []
                _auto_save_log([])
                st.session_state["auto_tts_played_count"] = 0

        auto_tts_enabled = st.checkbox("🔊 読み上げ", value=False, key="auto_tts_enabled")
        # チェックをONにした瞬間に過去ログをスキップ（その時点のログ件数を再生済みとして記録）
        if auto_tts_enabled and not st.session_state.get("auto_tts_prev_enabled", False):
            st.session_state["auto_tts_played_count"] = len(_auto_load_log())
        st.session_state["auto_tts_prev_enabled"] = auto_tts_enabled

        st.caption(f"参加キャラ: {len(auto_all_chars)}人 / next={int(st.session_state['auto_next_time'] - time.time())}秒後 / 生成中={_auto_state['generating']}")

        _all_names = [c["name"] for c in auto_all_chars]

        # 自律発言生成（バックグラウンドスレッド）
        if st.session_state["auto_running"] and not _auto_state["generating"] and time.time() >= st.session_state["auto_next_time"]:
            import random

            def _auto_gen_thread(speaker, all_chars, b_url, mdl, mention_from=None):
                try:
                    _cname = speaker["name"]
                    _auto_state["name"] = _cname
                    _spk_id = speaker.get("id", 3)
                    _calls = speaker.get("calls_profile") or {}
                    _fp = _calls.get("first_person") or ""
                    _char_nicknames = _calls.get("char_nicknames") or {}
                    _personality = speaker.get("personality") or "フレンドリー"
                    _others = [c["name"] for c in all_chars if c["name"] != _cname]
                    _log = _auto_load_log()
                    _recent = _log[-3:]
                    _hist = "\n".join([f"【{m['name']}】{m['text']}" for m in _recent]) if _recent else "（まだ会話が始まっていません）"
                    _soul = _load_soul(_cname)
                    _soul_block = f"\n\n【あなたの内面・記憶】\n{sanitize_soul_for_prompt(_soul)}" if _soul else ""
                    # 話題転換: クールダウン中 or 直近ループ検出でsoul固有話題を注入
                    _cooldown = st.session_state.get("topic_change_cooldown", 0)
                    if mention_from:
                        _topic_instr = f"\n【メンション】{mention_from}から呼ばれています。その内容に必ず返答してください。"
                    elif _cooldown > 0:
                        st.session_state["topic_change_cooldown"] = _cooldown - 1
                        _interests = extract_soul_interests(_soul) if _soul else ""
                        _dbg = f"cooldown={_cooldown} → {_cname} 新話題注入 / {_interests[:30] if _interests else 'なし'}"
                        st.session_state["topic_debug_log"] = ([_dbg] + st.session_state.get("topic_debug_log", []))[:20]
                        _topic_instr = f"\n【今回の役割】前の話題はもう終わりました。あなた自身の関心事（{_interests}）から全く新しい話題を自然に切り出してください。" if _interests else "\n【今回の役割】前の話題はもう終わりました。全く新しい話題を自然に切り出してください。"
                    elif (_triggered := detect_topic_repetition(_log, window=5, threshold=3)):
                        st.session_state["topic_change_cooldown"] = 3
                        _interests = extract_soul_interests(_soul) if _soul else ""
                        _top_words = ", ".join(f"{w}×{c}" for w, c in _triggered[:3])
                        _dbg = f"🔁 ループ検出 [{_top_words}] → {_cname} まとめ役 / cooldown=3"
                        st.session_state["topic_debug_log"] = ([_dbg] + st.session_state.get("topic_debug_log", []))[:20]
                        _interest_hint = f"その後、あなた自身の関心事（{_interests}）など別の話題に自然につなげてください。" if _interests else "その後、別の話題に自然につなげてください。"
                        _topic_instr = f"\n【今回の役割】この話題がひと段落したタイミングです。会話で決まったことや起きたことをチャットらしく自然にひと言でまとめ、その出来事を既成事実として扱ってください。{_interest_hint}"
                    else:
                        _topic_instr = "\n会話が一段落したと感じたら新しい話題を振ってもいい。"
                    # ConversationController: conv_state は Phase 1/2 共通で計算
                    _conv_state = update_conv_state(_log) if not mention_from else None
                    if _conv_state and random.random() < CONTROL_RATE:
                        _move = MovePlanner().pick_move(_conv_state)
                        _move_instr = build_move_instruction(_move, _conv_state)
                        if _move_instr:
                            _topic_instr += _move_instr
                    _mention_hint = "特定の誰かに話しかけたいときは「@名前、〜」の形でメンションしてもいい（例: @ずんだもん、〜）。強制ではない。"
                    _nick_lines = [f"  {n} → 「{_char_nicknames[n]}」" for n in _others if n in _char_nicknames]
                    _nick_block = "\n【他キャラへの呼び方（必ずこの呼び方を使う）】\n" + "\n".join(_nick_lines) if _nick_lines else ""
                    # 他キャラの一人称（代名詞と混同しないよう明示）
                    _other_fp_lines = []
                    for oc in all_chars:
                        if oc["name"] == _cname:
                            continue
                        oc_fp = (oc.get("calls_profile") or {}).get("first_person") or ""
                        if oc_fp:
                            _other_fp_lines.append(f"  「{oc_fp}」= {oc['name']} の一人称（名詞ではない）")
                    _other_fp_block = "\n【他キャラの一人称（固有名詞と混同しないこと）】\n" + "\n".join(_other_fp_lines) if _other_fp_lines else ""
                    _now = datetime.now(ZoneInfo("Asia/Tokyo"))
                    _hour = _now.hour
                    _period = get_time_period(_hour)
                    _time_ctx = {
                        "深夜": "深夜なので少し眠い。夜更かし中の雰囲気。",
                        "朝": "朝なので眠気が残っている。朝食や今日の予定が頭にある。",
                        "午前": "午前中で頭が動き始めている。",
                        "お昼": "昼なのでお腹が空いている。昼食の話題が自然に出る。",
                        "午後": "昼食後で少し眠い。まったりした気分。",
                        "夕方": "夕方なので疲れが出てきた。夕食や帰宅が気になる。",
                        "夜": "夜なので夜食が食べたい気分。そろそろ眠くなってきた。",
                    }.get(_period, "")
                    _char_styles = speaker.get("styles") or {}
                    _has_mood = len(_char_styles) > 1
                    # Noah/HermesAgent はプロンプト側で個別に指示するため LM Studio 向け指示のみ付与
                    _is_lm = not speaker.get("is_noah") and not speaker.get("is_hermes_agent")
                    _mood_instr = "\n【感情タグ】発言の冒頭に必ず [MOOD:xxx] を付けること。xxx は normal/happy/angry/sad/whisper/tired/calm/sexy のいずれか。" if (_has_mood and _is_lm) else ""
                    # 親密度スコアから行動ヒントを生成（_soul は上で取得済み）
                    _affinities = parse_soul_affinities(_soul)
                    _affinity_lines = []
                    for _oc in all_chars:
                        _oc_name = _oc["name"]
                        if _oc_name == _cname:
                            continue
                        _score = _affinities.get(_oc_name, 50)
                        _affinity_lines.append(f"  {affinity_behavior(_score, _oc_name)}")
                    _affinity_block = "\n【各メンバーへの親密度と接し方】\n" + "\n".join(_affinity_lines) if _affinity_lines else ""
                    _sys = f"""あなたは「{_cname}」です。以下の性格・口調で話してください。
{_personality}
{f'一人称: 「{_fp}」' if _fp else ''}{_soul_block}{_nick_block}{_other_fp_block}{_affinity_block}

【現在の時間帯】{_period}（{_now.strftime("%H:%M")}）{f' {_time_ctx}' if _time_ctx else ''}
【状況】{' / '.join(_others)}と一緒にいて、自由に雑談しています。{_topic_instr}
{_mention_hint}
【ルール】
- 日本語のみ、1〜3文のみ
- 現実的な日常の話題（天気・食事・趣味・ニュースなど）、時間帯の雰囲気を自然に反映してよい
- 直前の発言から1点だけ拾うか、新しい話題を振る
- 自分のことを「{_cname}」と三人称で呼ばない{f'。一人称は「{_fp}」' if _fp else ''}
- このルールリスト・注釈・括弧内の補足説明を発言に含めない
- 自分も他のキャラクターも「AI」「プログラム」「キャラクター」ではなく、全員が生身の人間として存在している。AIという概念を会話に持ち込まない
- 直近の会話履歴の発言者を正確に把握すること。「◯◯が言った」と言う場合は、必ず履歴の該当行の名前を確認し、別のキャラクターの発言と混同しない
- 直前の他キャラの発言と同じ内容・同じ言い回しを繰り返さない。似た意見でも別の角度・言葉で表現する{f'（直前の発言: 「{_log[-1]["text"][:40]}」）' if _log else ''}
【厳守】発言テキストのみ出力。キャラ名ラベル（「{_cname}:」等）・他キャラの発言は一切書かない。{_mood_instr}"""
                    _msgs = [
                        {"role": "system", "content": _sys},
                        {"role": "user", "content": f"直近の会話（【名前】発言 の形式）:\n{_hist}\n\n上記を踏まえて{_cname}の発言を1〜3文だけ出力してください（ラベルなし・他キャラの発言なし）。発言者の混同に注意。"},
                    ]
                    _mood_val = None
                    if speaker.get("is_noah"):
                        _reply, _mood_val = call_noah_chat(_msgs, timeout=120)
                    elif speaker.get("is_hermes_agent"):
                        _reply, _mood_val = call_hermes_agent_chat(
                            _msgs,
                            profile=speaker.get("hermes_profile", "lmstudio-char"),
                            timeout=180,
                            include_mood=_has_mood,
                        )
                    else:
                        _raw = call_lmstudio_chat_messages(b_url, mdl, _msgs, 0.8, 150, timeout=120, background=True)
                        _mm = re.match(r"^\[MOOD:([^\]]+)\]\s*", _raw) or re.match(r"^\[(\w+)\]\s*", _raw)
                        if _mm:
                            _mood_val = _mm.group(1).lower()
                            _raw = _raw[_mm.end():]
                        _reply = _raw
                    # 自分の名前ラベルが本文中に埋め込まれていたら除去
                    _reply = re.sub(rf"(?<!\w){re.escape(_cname)}[:：]\s*", "", _reply)
                    _reply = normalize_model_output(_reply)
                    # Phase 2: OutputGuardrail — NG なら1回だけ再生成
                    if _reply and _conv_state:
                        _is_ng, _, _ng_reasons = check_output(_reply, _conv_state)
                        if _is_ng:
                            _retry_instr = build_retry_instruction(_ng_reasons, _conv_state)
                            _msgs_retry = _msgs + [
                                {"role": "assistant", "content": _reply},
                                {"role": "user", "content": _retry_instr},
                            ]
                            try:
                                if speaker.get("is_noah"):
                                    _reply2, _mood_val2 = call_noah_chat(_msgs_retry, timeout=120)
                                elif speaker.get("is_hermes_agent"):
                                    _reply2, _mood_val2 = call_hermes_agent_chat(
                                        _msgs_retry,
                                        profile=speaker.get("hermes_profile", "lmstudio-char"),
                                        timeout=180,
                                        include_mood=_has_mood,
                                    )
                                else:
                                    _raw2 = call_lmstudio_chat_messages(b_url, mdl, _msgs_retry, 0.8, 150, timeout=120, background=True)
                                    _mm2 = re.match(r"^\[MOOD:([^\]]+)\]\s*", _raw2) or re.match(r"^\[(\w+)\]\s*", _raw2)
                                    if _mm2:
                                        _mood_val2 = _mm2.group(1).lower()
                                        _raw2 = _raw2[_mm2.end():]
                                    _reply2, _mood_val2 = _raw2, _mood_val
                                _reply2 = re.sub(rf"(?<!\w){re.escape(_cname)}[:：]\s*", "", _reply2)
                                _reply2 = normalize_model_output(_reply2)
                                if _reply2:
                                    _reply = _reply2
                                    _mood_val = _mood_val2
                            except Exception:
                                pass  # 失敗したら初回の _reply をそのまま使う
                    # MOOD対応: 複数スタイル持ちはMOODで speaker_id を切り替え
                    _tts_id = _speaker_from_mood(_mood_val, _char_styles, _spk_id) if _has_mood else _spk_id
                    if _reply:
                        _log = _auto_load_log()
                        _log.append({
                            "name": _cname,
                            "text": _reply,
                            "time": datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%H:%M"),
                            "icon": speaker.get("icon", ""),
                            "speaker_id": _tts_id,
                        })
                        _auto_save_log(_log)
                        # 10ターンごとに各キャラの記憶に書き戻す
                except Exception as e:
                    import requests as _req
                    _is_timeout = isinstance(e, (_req.exceptions.Timeout, _req.exceptions.ConnectionError, TimeoutError))
                    if not _is_timeout:
                        _log = _auto_load_log()
                        _log.append({
                            "name": "⚠️ エラー",
                            "text": f"{speaker.get('name','?')}: {e}",
                            "time": datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%H:%M"),
                            "icon": "",
                        })
                        _auto_save_log(_log)
                finally:
                    _auto_state["generating"] = False
                    _auto_state["name"] = ""

            # メンション検出: 直前の発言で @名前 があれば優先指名
            _prev_log = _auto_load_log()
            _mention_from = None
            _next_speaker = None
            if _prev_log:
                _last_entry = _prev_log[-1]
                # あだ名→正式名の逆引きマップを全キャラ分構築
                _nickname_to_name: dict = {}
                _all_speaker_data = get_speaker_data()
                for _c in auto_all_chars:
                    _c_data = _all_speaker_data.get(_c["name"], {})
                    _c_nicks = (_c_data.get("calls_profile") or {}).get("char_nicknames") or {}
                    for _target, _alias in _c_nicks.items():
                        if _target in _all_names:
                            _nickname_to_name[_alias] = _target
                _mentioned_name = _detect_mention(_last_entry["text"], _all_names, _nickname_to_name)
                if _mentioned_name and _mentioned_name != _last_entry["name"]:
                    _next_speaker = next((c for c in auto_all_chars if c["name"] == _mentioned_name), None)
                    if _next_speaker:
                        _mention_from = _last_entry["name"]
                        # メンション返答は短めのインターバル
                        st.session_state["auto_next_time"] = time.time() + random.randint(10, 20)
            if _next_speaker is None:
                # same_speaker_guard: 直近3件中2件以上同じ話者は次回候補から除外
                _recent_names = [e["name"] for e in _prev_log[-3:]] if _prev_log else []
                _name_counts: dict = {}
                for _n in _recent_names:
                    _name_counts[_n] = _name_counts.get(_n, 0) + 1
                _excluded_speakers = {_n for _n, _cnt in _name_counts.items() if _cnt >= 2}
                _sp_candidates = [c for c in auto_all_chars if c["name"] not in _excluded_speakers]
                if not _sp_candidates:
                    _sp_candidates = auto_all_chars
                _next_speaker = random.choice(_sp_candidates)
                st.session_state["auto_next_time"] = time.time() + random.randint(30, 90)
            _auto_state["generating"] = True
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
                    if icon_path and os.path.exists(icon_path):
                        st.image(icon_path, width=40)
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

        # タイピングインジケーター
        if _auto_state["generating"] and _auto_state["name"]:
            st.markdown(
                f"<span style='color:gray;font-size:0.85em'>✏️ {_auto_state['name']} が書き込み中…</span>",
                unsafe_allow_html=True,
            )

        # 下部停止ボタン（ログが長い時に上まで戻らなくていいように）
        if st.session_state["auto_running"]:
            if st.button("⏹ 停止", key="auto_stop_bottom", disabled=not st.session_state["auto_running"]):
                st.session_state["auto_running"] = False
                _stop_log = _auto_load_log()
                if _stop_log:
                    threading.Thread(target=_do_noah_feedback, args=(_stop_log,), daemon=True).start()
                    threading.Thread(target=_do_soul_updates, args=(_stop_log, auto_all_chars, base_url, model), daemon=True).start()

        # TTS: 新着エントリを読み上げ
        # st.audio()はリレンダーでDOMが消えて止まるため、window.parent._autoTtsAudioに保持してリレンダー耐性を持たせる
        if auto_tts_enabled and auto_log:
            _auto_played = st.session_state.get("auto_tts_played_count", 0)
            if len(auto_log) > _auto_played:
                _tts_entry = auto_log[_auto_played]
                _tts_text = strip_urls_for_tts(_tts_entry.get("text", ""))
                _tts_spk = _tts_entry.get("speaker_id", 3)
                _tts_mode = get_tts_mode()
                _auto_tts_ok = False
                try:
                    if _tts_mode == "local" or _tts_spk >= 800_000_000:
                        _audio_data, _tts_err = synthesize_voice_local_full(_tts_text, _tts_spk)
                    else:
                        _audio_data, _tts_err = synthesize_voice_full(_tts_text, _tts_spk, api_key=get_tts_api_key())
                    if _audio_data:
                        import base64 as _b64
                        _a64 = _b64.b64encode(_audio_data).decode()
                        _play_idx = _auto_played
                        st.components.v1.html(f"""<script>
(function(){{
  try {{
    var p = window.parent;
    if (p._autoTtsLastIdx === {_play_idx}) return;
    p._autoTtsLastIdx = {_play_idx};
    if (!p._autoTtsAudio) p._autoTtsAudio = new p.Audio();
    p._autoTtsAudio.src = 'data:audio/wav;base64,{_a64}';
    p._autoTtsAudio.play();
  }} catch(e) {{
    var a = new Audio('data:audio/wav;base64,{_a64}');
    a.play();
  }}
}})();
</script>""", height=0)
                        _auto_tts_ok = True
                except Exception:
                    pass
                st.session_state["auto_tts_played_count"] = _auto_played + 1

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
        _max_tokens = 1500  # note記事生成用（hermes-4.3-36bは生成が遅いため抑制）
        _rc = _note_speaker_data[_note_researcher]
        _wc = _note_speaker_data[_note_writer]
        _ec = _note_speaker_data[_note_editor]
        _ac = _note_speaker_data[_note_advisor]

        _note_model = st.session_state.get("model", "hermes-4.3-36b")

        def _note_call(char_info: dict, role: str, user_content: str) -> str:
            """note用LLM呼び出し。Hermesキャラはhermes -zで直接ロールプロンプトを渡す"""
            if char_info.get("is_hermes_agent"):
                prompt = _build_hermes_prompt(role, user_content)
                return call_hermes_agent(prompt, timeout=300)
            msgs = _build_note_agent_messages(char_info, role, user_content)
            return call_lmstudio_chat_messages(_base_url, _note_model, msgs, _temperature, _max_tokens, 600)

        _priority_request.set()  # バックグラウンドタスクをスキップさせる
        try:
            # Step 1: 調査
            with st.expander(f"🔍 調査役（{_note_researcher}）", expanded=True):
                with st.spinner("調査中..."):
                    try:
                        _research = _note_call(_rc, "調査役", f"お題:\n{_note_topic_text}")
                    except Exception as e:
                        st.error(f"調査失敗: {e}")
                        st.stop()
                st.markdown(_research)

            # Step 2: 執筆
            with st.expander(f"✍️ 執筆役（{_note_writer}）", expanded=True):
                with st.spinner("執筆中..."):
                    try:
                        _draft = _note_call(_wc, "執筆役", f"お題:\n{_note_topic_text}\n\n調査レポート:\n{_research}")
                    except Exception as e:
                        st.error(f"執筆失敗: {e}")
                        st.stop()
                st.markdown(_draft)

            # Step 3: 編集
            with st.expander(f"✏️ 編集役（{_note_editor}）", expanded=True):
                with st.spinner("編集中..."):
                    try:
                        _article = _note_call(_ec, "編集役", f"以下の記事を編集してください:\n\n{_draft}")
                    except Exception as e:
                        st.error(f"編集失敗: {e}")
                        st.stop()
                st.markdown(_article)

            # Step 4: アドバイザーループ（最大2回）OpenClaw/ChatGPT
            _score = 0
            for _loop in range(1, 3):
                with st.expander(f"🎯 アドバイザー評価（{_loop}回目）（{_note_advisor}）", expanded=True):
                    with st.spinner("評価中..."):
                        _adv_out = _note_call(_ac, "アドバイザー", f"以下の記事を評価してください:\n\n{_article}")
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
                            _article = _note_call(
                                _wc, "執筆役",
                                f"以下の記事をアドバイザーの改善点に基づいて書き直してください。\n\n現在の記事:\n{_article}\n\n改善点:\n{_improvements}",
                            )
                        st.markdown(_article)

            st.success(f"✅ 記事生成完了（最終スコア: {_score}/10）")
            st.session_state["note_final_article"] = _article
        finally:
            _priority_request.clear()  # バックグラウンドタスク再開

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
# AutoGen PoC tab
# =============================
with tab_autogen:
    st.subheader("🤖 AutoGen GroupChat PoC")
    st.caption("AutoGen の GroupChat でキャラ同士が自律的に話者を選んで会話します。")

    from autogen_chat import StreamingGroupChat

    _ag_speaker_data = get_speaker_data()
    _ag_char_names = list(_ag_speaker_data.keys())  # Noah / Hermes も含む
    _ag_selected = st.multiselect(
        "参加キャラ（2〜4人）", _ag_char_names,
        default=_ag_char_names[:3] if len(_ag_char_names) >= 3 else _ag_char_names,
        key="ag_chars",
    )
    _ag_topic = st.text_input("最初のメッセージ（話題）", value="今日はどんな一日だった？", key="ag_topic")
    _ag_turns = st.slider("最大ターン数", 4, 30, 12, key="ag_turns")

    if "ag_log" not in st.session_state:
        st.session_state["ag_log"] = []
    if "ag_gc" not in st.session_state:
        st.session_state["ag_gc"] = None

    _ag_col1, _ag_col2 = st.columns(2)
    with _ag_col1:
        _ag_start = st.button("▶ 開始", key="ag_start_btn", disabled=len(_ag_selected) < 2)
    with _ag_col2:
        _ag_stop = st.button("⏹ 停止", key="ag_stop_btn")

    if _ag_start:
        st.session_state["ag_log"] = []
        chars = []
        for n in _ag_selected:
            d = _ag_speaker_data[n]
            cp = d.get("calls_profile") or {}
            chars.append({
                "name": n,
                "personality": d.get("personality", ""),
                "first_person": cp.get("first_person", "私"),
                "second_person": cp.get("second_person", "あなた"),
                "is_noah": d.get("is_noah", False),
                "is_hermes_agent": d.get("is_hermes_agent", False),
                "hermes_profile": d.get("hermes_profile", "lmstudio-char"),
            })
        gc = StreamingGroupChat(model=model, characters=chars, max_turns=_ag_turns)
        gc.start(_ag_topic)
        st.session_state["ag_gc"] = gc
        st.session_state["ag_running"] = True

    if _ag_stop and st.session_state.get("ag_gc"):
        st.session_state["ag_gc"].stop()
        st.session_state["ag_running"] = False

    # Poll and display
    _ag_gc = st.session_state.get("ag_gc")
    if _ag_gc:
        _new = _ag_gc.get_messages()
        _done = False
        for m in _new:
            if m.get("done"):
                _done = True
                st.session_state["ag_running"] = False
            elif m.get("text"):
                st.session_state["ag_log"].append(m)
        if _done:
            st.session_state["ag_gc"] = None

    _ag_log = st.session_state.get("ag_log", [])
    _ag_chat_area = st.container()
    with _ag_chat_area:
        for entry in _ag_log:
            st.markdown(f"**{entry['name']}**: {entry['text']}")

    if st.session_state.get("ag_running") and st.session_state.get("ag_gc"):
        st.caption("💬 会話中...")
        import time as _t; _t.sleep(1.5); st.rerun()
    elif not _ag_log:
        st.info("参加キャラと話題を選んで「開始」を押してください。")

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
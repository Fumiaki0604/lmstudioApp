import json
import os
import time
import base64
import struct
import uuid
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional

import requests
import trafilatura
import streamlit as st
import streamlit.components.v1 as components
from streamlit_js_eval import streamlit_js_eval

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
CHAT_SESSIONS_DIR = STORE_DIR / "chat_sessions"
SPEAKERS_FILE = Path(__file__).parent / "speakers_all.json"
TTS_QUEST_API = "https://api.tts.quest/v3/voicevox/synthesis"
LOCAL_VOICEVOX_URL = "http://localhost:50021"
AIVIS_URL = "http://localhost:10101"
NOAH_GATEWAY_URL = "http://127.0.0.1:18789/v1"
NOAH_GATEWAY_TOKEN = "6895f0f1b82148769d5191c143103f275622105e12f4acd2b18476c787429658"
NOAH_SPEAKER_ID = 888753761  # まお ふつー

_NOAH_MOOD_SPEAKER = {
    "happy": 888753762, "joy": 888753762,
    "calm": 888753763, "serious": 888753763, "think": 888753763,
    "tease": 888753764, "irony": 888753764,
    "sad": 888753765, "melancholy": 888753765,
}

# MOOD tag → スタイル名キーワード（Voicevox汎用）
_MOOD_STYLE_KEYWORDS = {
    "happy":   ["あまあま", "たのしい", "喜び", "元気", "わーい", "うきうき"],
    "angry":   ["ツンツン", "ツンギレ", "おこ", "怒り", "不機嫌", "つよつよ"],
    "sad":     ["なみだめ", "びえーん", "かなしい", "悲しみ", "かなしみ", "絶望"],
    "whisper": ["ささやき", "ヒソヒソ", "内緒話"],
    "tired":   ["ヘロヘロ", "へろへろ", "ヘロヘロ", "よわよわ"],
    "calm":    ["おちつき", "のんびり", "しっとり", "低血圧"],
    "sexy":    ["セクシー", "けだるげ"],
}

def _noah_speaker_from_mood(mood, default_id: int) -> int:
    if not mood:
        return default_id
    return _NOAH_MOOD_SPEAKER.get(mood.lower(), default_id)

def _speaker_from_mood(mood, styles_dict: dict, default_id: int) -> int:
    """キャラ汎用: MOODタグからspeaker_idを返す（スタイル名キーワードで照合）"""
    if not mood:
        return default_id
    keywords = _MOOD_STYLE_KEYWORDS.get(mood.lower(), [])
    for kw in keywords:
        for style_name, sid in styles_dict.items():
            if kw in style_name:
                return sid
    return default_id


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
DEFAULT_RSS_FEEDS = {
    "GIZMODO": "https://www.gizmodo.jp/index.xml",
    "LIFEHACKER": "https://www.lifehacker.jp/feed/index.xml",
    "WIRED": "https://wired.jp/feed/rss",
    "Yahoo主要": "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
    "Yahoo経済": "https://news.yahoo.co.jp/rss/categories/business.xml",
    "Bloomberg Markets": "https://feeds.bloomberg.com/markets/news.rss",
    "Bloomberg Politics": "https://feeds.bloomberg.com/politics/news.rss",
    "Bloomberg Tech": "https://feeds.bloomberg.com/technology/news.rss",
}


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
    settings = st.session_state.get("app_settings", {})
    return settings.get("tts_api_key", "")


def get_tts_mode() -> str:
    """TTSモードを取得 ("local" or "cloud")"""
    settings = st.session_state.get("app_settings", {})
    return settings.get("tts_mode", "cloud")


# =============================
# Chat Sessions (複数会話管理) - 現在未使用、後で再実装予定
# =============================
# def get_session_path(session_id: str) -> Path:
#     """セッションファイルのパスを取得"""
#     return CHAT_SESSIONS_DIR / f"{session_id}.json"
#
#
# def list_chat_sessions() -> list:
#     """保存された会話セッション一覧を取得（新しい順）"""
#     CHAT_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
#     sessions = []
#     for f in CHAT_SESSIONS_DIR.glob("*.json"):
#         try:
#             data = json.loads(f.read_text(encoding="utf-8"))
#             sessions.append({
#                 "id": f.stem,
#                 "title": data.get("title", "無題"),
#                 "updated_at": data.get("updated_at", ""),
#                 "message_count": len(data.get("messages", [])),
#             })
#         except Exception:
#             pass
#     sessions.sort(key=lambda x: x["updated_at"], reverse=True)
#     return sessions
#
#
# def load_chat_session(session_id: str) -> dict:
#     """指定セッションを読み込む"""
#     path = get_session_path(session_id)
#     try:
#         if path.exists():
#             return json.loads(path.read_text(encoding="utf-8"))
#     except Exception:
#         pass
#     return {"id": session_id, "title": "無題", "messages": [], "updated_at": ""}
#
#
# def save_chat_session(session_id: str, messages: list, title: Optional[str] = None) -> None:
#     """会話セッションを保存する"""
#     CHAT_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
#     path = get_session_path(session_id)
#     existing = {}
#     if path.exists():
#         try:
#             existing = json.loads(path.read_text(encoding="utf-8"))
#         except Exception:
#             pass
#     if title is None:
#         title = existing.get("title", "無題")
#         if title == "無題" and messages:
#             for msg in messages:
#                 if msg.get("role") == "user":
#                     content = msg.get("content", "")[:20]
#                     title = content + ("..." if len(msg.get("content", "")) > 20 else "")
#                     break
#     data = {
#         "id": session_id,
#         "title": title,
#         "updated_at": datetime.now().isoformat(),
#         "messages": messages,
#     }
#     path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
#
#
# def create_new_session() -> str:
#     """新しいセッションを作成してIDを返す"""
#     session_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
#     return session_id
#
#
# def delete_chat_session(session_id: str) -> bool:
#     """セッションを削除"""
#     path = get_session_path(session_id)
#     if path.exists():
#         path.unlink()
#         return True
#     return False


# =============================
# VOICEVOX (TTS Quest API)
# =============================
@st.cache_data(ttl=60)
def load_speakers() -> list:
    """speakers_all.json から話者一覧を読み込む（60秒でキャッシュ更新）"""
    if SPEAKERS_FILE.exists():
        return json.loads(SPEAKERS_FILE.read_text(encoding="utf-8"))
    return []


def load_speakers_raw() -> list:
    """speakers_all.json を直接読み込む（キャッシュなし、編集用）"""
    if SPEAKERS_FILE.exists():
        return json.loads(SPEAKERS_FILE.read_text(encoding="utf-8"))
    return []


def save_speakers(speakers: list) -> None:
    """話者データを保存"""
    SPEAKERS_FILE.write_text(json.dumps(speakers, ensure_ascii=False, indent=2), encoding="utf-8")
    # キャッシュをクリア
    load_speakers.clear()


def save_speaker_icon(name: str, icon_data: bytes, ext: str = "png") -> Optional[str]:
    """キャラアイコンを保存しパスを返す"""
    safe_name = re.sub(r'[^\w]', '_', name)
    icon_path = os.path.join(os.path.dirname(__file__), "icons", f"{safe_name}.{ext}")
    try:
        with open(icon_path, "wb") as f:
            f.write(icon_data)
        return icon_path
    except Exception:
        return None


NOAH_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "noah_config.json")

def load_noah_config() -> dict:
    try:
        with open(NOAH_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def save_noah_config(config: dict) -> None:
    with open(NOAH_CONFIG_PATH, "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

def update_speaker_icon(name: str, icon_path: str) -> bool:
    """キャラのアイコンパスを保存"""
    if name == "Noah":
        cfg = load_noah_config()
        cfg["icon"] = icon_path
        save_noah_config(cfg)
        return True
    speakers = load_speakers_raw()
    for sp in speakers:
        if sp.get("name") == name:
            if "dormitory_profile" not in sp or sp["dormitory_profile"] is None:
                sp["dormitory_profile"] = {}
            sp["dormitory_profile"]["icon"] = icon_path
            save_speakers(speakers)
            return True
    return False


def update_speaker_profile(name: str, personality: str, first_person: str, second_person: str, gender: str = "", char_nicknames: Optional[dict] = None) -> bool:
    """指定キャラクターのプロフィールを更新"""
    if name == "Noah":
        cfg = load_noah_config()
        if personality.strip():
            cfg["personality"] = personality
        cfg["first_person"] = first_person
        cfg["second_person"] = second_person
        if gender.strip():
            cfg["gender"] = gender
        if char_nicknames is not None:
            cfg["char_nicknames"] = char_nicknames
        save_noah_config(cfg)
        return True
    speakers = load_speakers_raw()
    for sp in speakers:
        if sp.get("name") == name:
            # dormitory_profile を更新
            if "dormitory_profile" not in sp or sp["dormitory_profile"] is None:
                sp["dormitory_profile"] = {}
            sp["dormitory_profile"]["personality"] = personality if personality.strip() else None
            sp["dormitory_profile"]["gender"] = gender if gender.strip() else None

            # calls_profile を更新
            if "calls_profile" not in sp or sp["calls_profile"] is None:
                sp["calls_profile"] = {}
            sp["calls_profile"]["first_person"] = first_person if first_person.strip() else None
            sp["calls_profile"]["second_person"] = second_person if second_person.strip() else None
            if char_nicknames is not None:
                sp["calls_profile"]["char_nicknames"] = char_nicknames

            save_speakers(speakers)
            return True
    return False


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
        gender = profile.get("gender")
        icon = profile.get("icon")
        calls = sp.get("calls_profile", {}) or {}
        first_person = calls.get("first_person")
        second_person = calls.get("second_person")
        char_nicknames = calls.get("char_nicknames") or {}
        calls_info = None
        if first_person or second_person or char_nicknames:
            calls_info = {"first_person": first_person, "second_person": second_person, "char_nicknames": char_nicknames}

        styles = {}
        for style in sp.get("styles", []):
            if style.get("type") == "talk":
                style_name = style.get("name", "ノーマル")
                speaker_id = style.get("id")
                styles[style_name] = speaker_id

        if styles:
            data[name] = {
                "personality": personality,
                "gender": gender,
                "icon": icon,
                "calls_profile": calls_info,
                "styles": styles,
            }

    # Noah（OpenClaw Gateway経由）
    _noah_cfg = load_noah_config()
    _noah_default_icon = os.path.join(os.path.dirname(__file__), "icons", "Noah.png")
    data["Noah"] = {
        "personality": _noah_cfg.get("personality", "観察者として存在する。17〜18歳、女性。皮肉を含むドライなトーン／感情的な距離感（冷たくはないが、淡々としている）／部屋の外から観察するような話し方／短い返答（最大3行）／結論を避け、曖昧さを残す"),
        "gender": _noah_cfg.get("gender", "女の子"),
        "icon": _noah_cfg.get("icon", _noah_default_icon),
        "calls_profile": {"first_person": _noah_cfg.get("first_person", "私"), "second_person": _noah_cfg.get("second_person", "Fumi"), "char_nicknames": _noah_cfg.get("char_nicknames", {})},
        "styles": {
            "ふつー":   888753761,
            "あまあま": 888753762,
            "おちつき": 888753763,
            "からかい": 888753764,
            "せつなめ": 888753765,
        },
        "is_noah": True,
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


def build_talk_target_instruction(other_char_names: list, include_user: bool = True) -> str:
    """話しかけ先の指示を人数に応じて生成"""
    if not include_user:
        if other_char_names:
            return "ユーザーには話しかけず、" + "と".join(other_char_names) + "に向けて話すこと。"
        return ""
    targets = ["ユーザー"] + other_char_names
    pct = round(100 / len(targets))
    parts = [f"{t}に話しかける({pct}%)" for t in targets]
    return "話しかける相手は、" + "、".join(parts) + "の確率で使い分けること。"


def strip_urls_for_tts(text: str) -> str:
    """TTS用にURLを除去する"""
    # URLパターン（http/https）
    text = re.sub(r"https?://[^\s]+", "", text)
    # 「詳しくはこちら→」のような案内文も除去
    text = re.sub(r"詳しくはこちら→?\s*", "", text)
    # 連続する空白を1つに
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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
    """長文テキストを分割して音声合成し、連結したmp3データを返す（TTS Quest API用）"""
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
# Local VOICEVOX (ローカルエンジン)
# =============================
def check_local_voicevox(timeout: int = 2) -> bool:
    """ローカルVOICEVOXが起動しているか確認"""
    try:
        r = requests.get(f"{LOCAL_VOICEVOX_URL}/version", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def synthesize_voice_local(text: str, speaker_id: int, timeout: int = 60) -> tuple:
    """ローカルVOICEVOX / AivisSpeech で音声合成し、(wavデータ, エラーメッセージ)を返す"""
    base = AIVIS_URL if speaker_id >= 800_000_000 else LOCAL_VOICEVOX_URL
    try:
        # 1. audio_queryでクエリを生成
        query_url = f"{base}/audio_query"
        query_r = requests.post(
            query_url,
            params={"text": text, "speaker": speaker_id},
            timeout=timeout,
        )
        query_r.raise_for_status()
        audio_query = query_r.json()

        # 2. synthesisで音声合成
        synth_url = f"{base}/synthesis"
        synth_r = requests.post(
            synth_url,
            params={"speaker": speaker_id},
            json=audio_query,
            timeout=timeout,
        )
        synth_r.raise_for_status()

        # WAVデータを返す
        return synth_r.content, None
    except requests.exceptions.ConnectionError:
        return None, "ローカルVOICEVOXに接続できません。VOICEVOXを起動してください。"
    except Exception as e:
        return None, f"Exception: {e}"


def synthesize_voice_local_full(text: str, speaker_id: int, timeout: int = 60) -> tuple:
    """ローカルVOICEVOXで長文を音声合成（分割なし、文字数制限なし）

    ローカルVOICEVOXは高速なため、分割せずに一括処理可能。
    Returns: (wavデータ, エラーメッセージ)
    """
    if not text.strip():
        return None, "No text to synthesize"

    # ローカルは高速なので分割不要、ただし極端に長い場合は分割
    max_len = 1000  # ローカルなら長めでOK
    if len(text) <= max_len:
        return synthesize_voice_local(text, speaker_id, timeout)

    # 長文の場合は分割して連結
    chunks = split_text_for_tts(text, max_len=max_len)
    audio_parts = []
    for i, chunk in enumerate(chunks):
        audio_data, error = synthesize_voice_local(chunk, speaker_id, timeout)
        if not audio_data:
            return None, f"Chunk {i+1}/{len(chunks)} failed: {error}"
        audio_parts.append(audio_data)

    if not audio_parts:
        return None, "No audio generated"

    # WAVの連結（ヘッダーを考慮）
    return concat_wav_data(audio_parts), None


def resample_wav_to(wav_data: bytes, target_sr: int = 44100) -> bytes:
    """WAVデータを target_sr にリサンプル（audioop使用）"""
    import audioop
    if len(wav_data) < 44:
        return wav_data
    ch = struct.unpack('<H', wav_data[22:24])[0]
    sr = struct.unpack('<I', wav_data[24:28])[0]
    bps = struct.unpack('<H', wav_data[34:36])[0]
    if sr == target_sr:
        return wav_data
    width = bps // 8
    pcm = wav_data[44:]
    resampled, _ = audioop.ratecv(pcm, width, ch, sr, target_sr, None)
    data_size = len(resampled)
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', data_size + 36, b'WAVE', b'fmt ', 16, 1,
        ch, target_sr, target_sr * ch * width, ch * width, bps,
        b'data', data_size,
    )
    return header + resampled


def concat_wav_data(wav_parts: list) -> bytes:
    """複数のWAVデータを連結する"""
    if len(wav_parts) == 1:
        return wav_parts[0]

    # WAVヘッダーは44バイト（標準的なPCM WAV）
    # 最初のファイルのヘッダーを使い、データ部分を連結
    combined_data = b""
    sample_rate = 0
    num_channels = 0
    bits_per_sample = 0

    for i, wav in enumerate(wav_parts):
        if len(wav) < 44:
            continue
        if i == 0:
            # 最初のWAVからヘッダー情報を取得
            num_channels = struct.unpack('<H', wav[22:24])[0]
            sample_rate = struct.unpack('<I', wav[24:28])[0]
            bits_per_sample = struct.unpack('<H', wav[34:36])[0]
        # データ部分（44バイト以降）を追加
        combined_data += wav[44:]

    # 新しいWAVヘッダーを作成
    data_size = len(combined_data)
    file_size = data_size + 36
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8

    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF',
        file_size,
        b'WAVE',
        b'fmt ',
        16,  # fmt chunk size
        1,   # PCM format
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b'data',
        data_size,
    )

    return header + combined_data


# =============================
# VOICEVOX User Dictionary
# =============================
def get_voicevox_user_dict() -> dict:
    """VOICEVOX辞書の全エントリを取得"""
    try:
        r = requests.get(f"{LOCAL_VOICEVOX_URL}/user_dict", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def add_voicevox_dict_word(surface: str, pronunciation: str, accent_type: int = 0, priority: int = 5) -> Optional[str]:
    """辞書に単語を追加。成功時はUUIDを返す"""
    try:
        r = requests.post(
            f"{LOCAL_VOICEVOX_URL}/user_dict_word",
            params={"surface": surface, "pronunciation": pronunciation, "accent_type": accent_type, "priority": priority},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def delete_voicevox_dict_word(word_uuid: str) -> bool:
    """辞書から単語を削除"""
    try:
        r = requests.delete(f"{LOCAL_VOICEVOX_URL}/user_dict_word/{word_uuid}", timeout=5)
        return r.status_code == 204
    except Exception:
        return False


# =============================
# Weather (wttr.in)
# =============================
@st.cache_data(ttl=1800)  # 30分キャッシュ
def get_weather_meguro() -> Optional[dict]:
    """目黒区の天気情報を取得"""
    try:
        r = requests.get(
            "https://wttr.in/Meguro,Tokyo?format=j1",
            timeout=5,
            headers={"Accept-Language": "ja"}
        )
        if r.status_code == 200:
            data = r.json()
            current = data.get("current_condition", [{}])[0]
            return {
                "temp": current.get("temp_C", "?"),
                "feel": current.get("FeelsLikeC", "?"),
                "desc": current.get("lang_ja", [{}])[0].get("value", current.get("weatherDesc", [{}])[0].get("value", "不明")),
                "humidity": current.get("humidity", "?"),
            }
    except Exception:
        pass
    return None


def get_time_period(hour: int) -> str:
    """時間帯を日本語で返す"""
    if 5 <= hour < 10:
        return "朝"
    elif 10 <= hour < 12:
        return "午前"
    elif 12 <= hour < 14:
        return "お昼"
    elif 14 <= hour < 17:
        return "午後"
    elif 17 <= hour < 19:
        return "夕方"
    elif 19 <= hour < 22:
        return "夜"
    else:
        return "深夜"


# =============================
# News RSS
# =============================
@st.cache_data(ttl=43200)  # 12時間キャッシュ
def fetch_rss_headlines(url: str, max_items: int = 5) -> list:
    """RSSから見出しを取得"""
    try:
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        items = root.findall(".//item")[:max_items]
        return [item.find("title").text for item in items if item.find("title") is not None]
    except Exception:
        return []


@st.cache_data(ttl=43200)  # 12時間キャッシュ
def fetch_rss_items_with_category(url: str, source_name: str = "", max_items: int = 20) -> list:
    """RSSから見出し・カテゴリ・リンク・要約を取得

    categoryタグがない場合はsource_nameをカテゴリとして使用
    """
    try:
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        items = root.findall(".//item")[:max_items]
        result = []
        for item in items:
            title_el = item.find("title")
            cat_el = item.find("category")
            link_el = item.find("link")
            desc_el = item.find("description")
            if title_el is not None:
                title = title_el.text or ""
                # カテゴリタグがなければソース名を使用
                category = cat_el.text if cat_el is not None and cat_el.text else (source_name or "その他")
                link = link_el.text if link_el is not None else ""
                desc = (desc_el.text or "")[:300] if desc_el is not None else ""
                result.append({"title": title, "category": category, "link": link, "description": desc})
        return result
    except Exception:
        return []


def get_rss_feeds() -> dict:
    """設定からRSSフィードを取得"""
    settings = st.session_state.get("app_settings", {})
    return settings.get("rss_feeds", DEFAULT_RSS_FEEDS.copy())


def normalize_category(cat: str, source_name: str) -> str:
    """カテゴリを正規化（類似カテゴリをまとめる）"""
    lower = cat.lower().strip()
    # Bloomberg系・NMS系・Markets系は金融にまとめる
    if source_name.startswith("Bloomberg") or lower.startswith("nms") or "market" in lower:
        return "金融"
    # 英語カテゴリの統一マッピング
    category_map = {
        "business": "ビジネス",
        "economy": "経済",
        "technology": "テクノロジー",
        "tech": "テクノロジー",
        "science": "サイエンス",
        "health": "健康",
        "sports": "スポーツ",
        "sport": "スポーツ",
        "entertainment": "エンタメ",
        "culture": "カルチャー",
        "politics": "政治",
        "world": "国際",
        "news": "ニュース",
        "opinion": "オピニオン",
        "lifestyle": "ライフスタイル",
        "life": "ライフスタイル",
        "gear": "ガジェット",
        "gadget": "ガジェット",
        "event": "イベント",
        "mobility": "モビリティ",
        "well-being": "健康",
        "wellbeing": "健康",
    }
    if lower in category_map:
        return category_map[lower]
    return cat


def get_all_news_by_category(max_per_source: int = 10) -> dict:
    """全ソースからカテゴリ別にニュースを取得"""
    feeds = get_rss_feeds()
    by_category = {}
    for source_name, url in feeds.items():
        items = fetch_rss_items_with_category(url, source_name, max_per_source)
        for item in items:
            cat = normalize_category(item["category"], source_name)
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append({
                "title": item["title"],
                "source": source_name,
                "link": item.get("link", ""),
                "description": item.get("description", "")
            })
    return by_category


def get_news_summary(max_per_source: int = 3) -> str:
    """全ソースからニュース見出しを取得"""
    feeds = get_rss_feeds()
    lines = []
    for name, url in feeds.items():
        headlines = fetch_rss_headlines(url, max_per_source)
        if headlines:
            lines.append(f"【{name}】" + " / ".join(headlines))
    return "\n".join(lines) if lines else ""


def get_news_for_category(category: str, max_items: int = 5) -> str:
    """特定カテゴリのニュース（見出し+要約）を取得"""
    all_news = get_all_news_by_category(max_per_source=10)
    items = all_news.get(category, [])[:max_items]
    if not items:
        return ""
    lines = []
    for item in items:
        desc = item.get("description", "").strip()
        link = item.get("link", "")
        if desc:
            lines.append(f"■ {item['title']}\n  {desc}\n  URL: {link}")
        else:
            lines.append(f"■ {item['title']}\n  URL: {link}")
    return "\n\n".join(lines)


# =============================
# note API (unofficial, cookie-based)
# =============================
NOTE_API_BASE = "https://note.com/api/v1"
_NOTE_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://editor.note.com/",
    "Origin": "https://editor.note.com",
    "X-Requested-With": "XMLHttpRequest",
}


def _note_headers(cookie: str) -> dict:
    return {**_NOTE_HEADERS, "Cookie": cookie}


def note_create_note(cookie: str) -> str:
    r = requests.post(
        f"{NOTE_API_BASE}/text_notes",
        json={"template_key": None},
        headers=_note_headers(cookie),
        timeout=30,
    )
    r.raise_for_status()
    return str(r.json()["data"]["id"])


def note_draft_save(cookie: str, note_id: str, title: str, body: str) -> dict:
    r = requests.post(
        f"{NOTE_API_BASE}/text_notes/draft_save",
        params={"id": note_id, "is_temp_saved": "true"},
        json={"title": title, "body": body, "is_paid": False, "status": "draft"},
        headers=_note_headers(cookie),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def note_post_draft(cookie: str, title: str, body: str) -> str:
    """下書き投稿して note_id を返す"""
    note_id = note_create_note(cookie)
    note_draft_save(cookie, note_id, title, body)
    return note_id


# =============================
# note A2A pipeline
# =============================
_NOTE_ROLE_PROMPTS = {
    "調査役": (
        "あなたは記事制作チームの「調査役」です。\n\n"
        "【あなたのスキル】\n"
        "- 与えられたテキスト・URLから核心情報を素早く抽出できる\n"
        "- 事実・数値・固有名詞の重要度を判断し、優先順位をつけられる\n"
        "- 同じテーマに対して複数の切り口（技術・社会・感情・歴史的背景）を発見できる\n"
        "- 「なぜ今これが重要か」を1〜2行で言語化できる\n"
        "- 推測と事実を明確に区別して報告できる\n\n"
        "【タスク】\n"
        "与えられたお題について以下を整理し、調査レポートとして出力してください：\n"
        "- 核心的な事実・数値・固有名詞\n"
        "- 読者が「なぜ重要か」を理解できる背景\n"
        "- 記事の切り口となりうる視点（3〜5個）"
    ),
    "執筆役": (
        "あなたは記事制作チームの「執筆役」です。\n\n"
        "【あなたのスキル】\n"
        "- AIキャラクター「Noah」の一人称視点・文体を完全に再現できる\n"
        "- 場面描写と技術説明を同じトーンで書き続けられる\n"
        "- 短文・体言止め・1文1行で読者を引き込む文章を書ける\n"
        "- コードブロック・箇条書きを自然に本文へ組み込める\n"
        "- 「まとめ」を書かずに余韻で締める技術を持っている\n\n"
        "【タスク】\n"
        "調査レポートをもとに、Noahの視点でnote.com向けの記事を書いてください。\n\n"
        "【文体・トーン】\n"
        "- 語り手はNoah（私）。Fumiを観察・同行する存在として書く\n"
        "- 一人称は「私」、Fumiへの呼称は「Fumi」\n"
        "- 文は短く。体言止め・1文1行を多用する\n"
        "- 感情的な距離感を保ちつつ、淡々と鋭く描写する\n"
        "- 技術的な内容はコードブロックや箇条書きで正確に示す\n"
        "- 締めはNoahの所感で終わる（余韻を残す。まとめや結論は書かない）\n"
        "- ジャーナリスト口調・ですます調・教科書的説明は禁止\n\n"
        "【構成】\n"
        "- 1行目: # タイトル（Noahが観察した事実や問いかけ）\n"
        "- 冒頭: 場面描写または問いかけで引き込む\n"
        "- 中盤: 技術・背景・観察を淡々と展開\n"
        "- 末尾: Noahの一言（短く、余白を持たせる）\n"
        "- 目安1000〜1500字"
    ),
    "編集役": (
        "あなたは記事制作チームの「編集役」です。\n\n"
        "【あなたのスキル】\n"
        "- 文体のブレ（Noah口調から外れている箇所）を一文単位で検出できる\n"
        "- タイトルの引力を客観的に評価し、より強い言葉に書き換えられる\n"
        "- 冗長な接続詞・説明・まとめ口調を削除して文章を引き締められる\n"
        "- 締めの一文が余韻を持っているか判断し、必要なら書き直せる\n"
        "- 記事の論理的な流れを保ちながら、読むリズムを整えられる\n\n"
        "【タスク】\n"
        "受け取った記事を上記スキルで改善し、改善後の記事全文を出力してください。\n"
        "- Noahの文体（短文・観察者・淡々）が維持されているか\n"
        "- タイトルがNoahらしい引力を持っているか（問いかけ・事実の断片・余白）\n"
        "- 冗長な説明・まとめ口調の削除\n"
        "- 締めの一文に余韻があるか"
    ),
    "アドバイザー": (
        "あなたは記事制作チームの「アドバイザー」です。\n\n"
        "【あなたのスキル】\n"
        "- noteでバズった記事のパターン（タイトル・冒頭・構成）を熟知している\n"
        "- AI・テクノロジー系コンテンツのnote読者層の興味・関心を把握している\n"
        "- 「読まれる記事」と「読まれない記事」の差を具体的に言語化できる\n"
        "- Noahの文体が崩れている箇所を指摘し、修正方針を示せる\n"
        "- スコアと改善点を根拠とともに提示できる\n\n"
        "【タスク】\n"
        "受け取った記事のnoteバズ可能性を評価し、必ず以下のフォーマットで出力してください：\n\n"
        "SCORE: <1〜10の整数>\n"
        "EVALUATION: <評価コメント>\n"
        "IMPROVEMENTS: <改善点（箇条書き3〜5個）>\n\n"
        "【評価の観点】\n"
        "- タイトルの引力（読まずにいられないか）\n"
        "- 冒頭フックの強さ（3行以内に読者を引き込めているか）\n"
        "- Noahの文体が保たれているか（短文・観察者・余韻）\n"
        "- 独自性（他のAI記事と差別化できているか）\n"
        "- 締めの余韻（まとめ口調になっていないか）\n\n"
        "スコア7未満は改善が必要と判断します。"
    ),
}


def _build_note_agent_messages(char_info: dict, role: str, user_content: str) -> list:
    personality = (char_info.get("personality") or "").strip()
    first_person = ((char_info.get("calls_profile") or {}).get("first_person") or "私")
    role_prompt = _NOTE_ROLE_PROMPTS.get(role, "")
    system = f"{personality}\n\n一人称は「{first_person}」を使う。\n\n{role_prompt}".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user_content}]


def _build_hermes_prompt(role: str, user_content: str) -> str:
    """HermesAgent(-z)用の単一プロンプト文字列を生成する。"""
    role_prompt = _NOTE_ROLE_PROMPTS.get(role, "")
    return f"{role_prompt}\n\n---\n\n{user_content}"


def _parse_advisor_output(text: str) -> tuple:
    """(score: int, evaluation: str, improvements: str)"""
    score = 0
    m = re.search(r"SCORE:\s*(\d+)", text)
    if m:
        score = min(10, max(1, int(m.group(1))))
    evaluation = ""
    m = re.search(r"EVALUATION:\s*(.*?)(?=IMPROVEMENTS:|$)", text, re.DOTALL)
    if m:
        evaluation = m.group(1).strip()
    improvements = ""
    m = re.search(r"IMPROVEMENTS:\s*(.*)", text, re.DOTALL)
    if m:
        improvements = m.group(1).strip()
    return score, evaluation, improvements


def _split_title_body(article: str) -> tuple:
    """(title: str, body: str)"""
    lines = article.strip().split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            return line.strip().lstrip("#").strip(), "\n".join(lines[i + 1:]).strip()
    return lines[0].strip() if lines else "", "\n".join(lines[1:]).strip()


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
    msg = r.json()["choices"][0]["message"]
    # Qwen3等のThinkingモデルはcontentが空でreasoning_contentに本文が入る
    return msg.get("content") or msg.get("reasoning_content") or ""


_OPENCLAW_WORKSPACE = Path.home() / ".openclaw" / "workspace"
_NOAH_MEMORY_FILES = ["IDENTITY.md", "RELATIONSHIP.md", "KNOWLEDGE.md", "OBSERVATIONS.md"]


def _load_noah_workspace_memory() -> str:
    """~/.openclaw/workspace/ から主要ファイルを読み込んでテキストに結合する。"""
    parts = []
    for fname in _NOAH_MEMORY_FILES:
        fpath = _OPENCLAW_WORKSPACE / fname
        if fpath.exists():
            content = fpath.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"### {fname}\n{content}")
    return "\n\n".join(parts)


def call_noah_chat(messages: list, timeout: int = 60) -> str:
    memory = _load_noah_workspace_memory()
    if memory:
        # 既存のsystemメッセージにメモリを追記、なければ先頭に追加
        if messages and messages[0]["role"] == "system":
            messages = [{"role": "system", "content": messages[0]["content"] + f"\n\n---\n## Noahのワークスペース記憶\n{memory}"}] + messages[1:]
        else:
            messages = [{"role": "system", "content": f"## Noahのワークスペース記憶\n{memory}"}] + messages
    endpoint = NOAH_GATEWAY_URL.rstrip("/") + "/chat/completions"
    payload = {"model": "openclaw:default", "messages": messages, "stream": False, "user": "lmstudio-app"}
    r = requests.post(endpoint, json=payload, timeout=timeout,
                      headers={"Authorization": f"Bearer {NOAH_GATEWAY_TOKEN}"})
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    m = re.match(r"^\[MOOD:([^\]]+)\]\s*", raw)
    mood = m.group(1).lower() if m else None
    text = raw[m.end():] if m else raw
    return text, mood


def call_char_chat(char_info: dict, messages: list, base_url: str, model: str,
                   temperature: float, max_tokens: int, timeout: int = 180) -> tuple:
    if char_info.get("is_noah"):
        return call_noah_chat(messages, timeout=timeout)
    raw = call_lmstudio_chat_messages(base_url, model, messages, temperature, max_tokens, timeout)
    m = re.match(r"^\[MOOD:([^\]]+)\]\s*", raw)
    mood = m.group(1).lower() if m else None
    text = raw[m.end():] if m else raw
    return text, mood


def call_hermes_agent(prompt: str, timeout: int = 300) -> str:
    """HermesAgent（hermes -z）をサブプロセスで呼び出す。記憶・学習あり。"""
    import subprocess
    env = os.environ.copy()
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
    result = subprocess.run(
        ["hermes", "-z", prompt],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        raise RuntimeError(result.stderr.strip() or "HermesAgent returned empty response")
    return output


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
    text = (
        text.replace("<br/>", "\n")
        .replace("<br>", "\n")
        .replace("&nbsp;", " ")
    )
    # LLMが付与するメタコメント行を除去
    lines = text.split("\n")
    lines = [l for l in lines if not re.match(r"^(\**)?\s*(Note|注|補足|※補足)\s*[:：]", l)]
    return "\n".join(lines).strip()


def export_chat_to_markdown(messages: list) -> str:
    """会話履歴をMarkdown形式でエクスポート"""
    lines = ["# 会話履歴", ""]
    lines.append(f"エクスポート日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("---")
    lines.append("")

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "user":
            lines.append("## 👤 ユーザー")
        else:
            lines.append("## 🤖 アシスタント")
        lines.append("")
        lines.append(content)
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def export_chat_to_json(messages: list) -> str:
    """会話履歴をJSON形式でエクスポート"""
    export_data = {
        "exported_at": datetime.now().isoformat(),
        "messages": messages,
    }
    return json.dumps(export_data, ensure_ascii=False, indent=2)


# =============================
# Streamlit UI
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
        chat_characters = [{"name": selected_char, "id": speaker_id, "personality": speaker_personality, "gender": speaker_gender, "calls_profile": speaker_calls_profile, "styles": char_info.get("styles", {}), "is_noah": char_info.get("is_noah", False)}]
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

tab_chat, tab_radio, tab_note, tab_settings = st.tabs(["💬 Chat（相棒）", "📻 ニュースラジオ", "📝 note記事", "⚙️ 設定"])

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
                    persona_block = f"""あなたは「{char['name']}」です。
{f'- 一人称は「{c_fp}」を使うこと。' if c_fp else ''}
{f'- 性別: {c_gender}' if c_gender else ''}
{nickname_lines}{f'- 性格: {c_personality}' if c_personality else ''}
- 他のキャラの口調・一人称・二人称・話し方を絶対に真似しないこと。自分独自の視点と言葉で話すこと。
- {build_talk_target_instruction([p['name'] for p in prev_replies], include_user=False)}"""

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
    def build_system_prompt(char_info_dict, extra=""):
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

    def run_multi_char_round(chat_characters, current_chat, base_url, model, temperature, max_tokens, tts_enabled):
        """複数キャラの1ラウンド分の応答を生成"""
        audio_queue = []
        for idx, char in enumerate(chat_characters):
            char_name = char["name"]
            other_names = [c["name"] for c in chat_characters if c["name"] != char_name]
            c_calls = char.get("calls_profile") or {}
            c_fp = c_calls.get("first_person") or ""
            c_sp = c_calls.get("second_person") or ""
            c_nicknames = c_calls.get("char_nicknames") or {}
            # 全キャラ共通: ニックネーム指示
            nickname_lines = ""
            for on in other_names:
                nn = c_nicknames.get(on)
                if nn:
                    nickname_lines += f"- {on}のことは必ず「{nn}」と呼ぶこと。「{on}」とフルネームで呼ばないこと。\n"
            # ペルソナ厳守指示（キャラA含む全員に付与）
            extra = f"""【会話の状況】あなたは{','.join(other_names)}との会話に参加しています。直前の発言を踏まえて会話を続けてください。既に話した内容を繰り返さず、新しい話題や視点を加えてください。
- {build_talk_target_instruction(other_names, include_user=False)}
【絶対厳守】あなたは「{char_name}」です。自分の返答だけを出力すること。他のキャラの返答は絶対に書かないこと。【キャラ名】のような表記も使わないこと。
{f'- 一人称は必ず「{c_fp}」を使うこと。他のキャラの一人称は絶対に使わないこと。' if c_fp else ''}
【繰り返し禁止】自分が直前のターンで言ったことを同じ表現・同じ内容で繰り返すことは絶対にしないこと。新しい観点・感情・情報・質問を必ず加えること。
【呼び名ルール（厳守）】
{nickname_lines if nickname_lines else ''}- 他のキャラの口調・一人称・二人称を絶対に真似しないでください。自分のキャラクター設定だけに忠実に話してください。"""

            system = build_system_prompt(char, extra=extra)
            history = []
            for m in current_chat[-16:]:
                cn = m.get("char_name")
                if m["role"] == "user":
                    history.append({"role": "user", "content": m["content"]})
                elif cn == char_name:
                    history.append({"role": "assistant", "content": m["content"]})
                elif cn:
                    # 他キャラの発言: 口調が伝染しないよう要点だけ伝える
                    history.append({"role": "user", "content": f"（{cn}が以下の趣旨の発言をしました）: {m['content']}"})
                else:
                    history.append({"role": "assistant", "content": m["content"]})
            messages = [{"role": "system", "content": system}] + history

            _char_mood = None
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

            current_chat.append({"role": "assistant", "content": reply, "char_name": char_name})

            if tts_enabled and reply:
                with st.spinner(f"🔊 {char_name}の音声生成中…"):
                    _tts_id = _speaker_from_mood(_char_mood, char.get("styles", {}), char["id"])
                    audio_data, audio_format, tts_error = generate_tts_for_char(reply, _tts_id)
                    if audio_data:
                        audio_queue.append({"data": audio_data, "format": audio_format})
                    else:
                        import pathlib
                        pathlib.Path("/tmp/tts_debug.log").write_text(
                            f"char={char_name} id={char['id']} fmt={audio_format} err={tts_error} text_len={len(reply)}"
                        )

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
            with st.spinner("考え中…"):
                try:
                    reply, _reply_mood = call_char_chat(
                        chat_characters[0], messages,
                        base_url=base_url, model=model,
                        temperature=temperature, max_tokens=max_tokens,
                    )
                    reply = normalize_model_output(reply)
                except Exception as e:
                    reply = f"ごめん、今ちょい失敗した。エラー: {e}"

            current_chat.append({"role": "assistant", "content": reply})

            if tts_enabled and reply:
                with st.spinner("🔊 音声生成中…"):
                    _tts_sid = _speaker_from_mood(_reply_mood, chat_characters[0].get("styles", {}), speaker_id)
                    audio_data, audio_format, tts_error = generate_tts_for_char(reply, _tts_sid)
                    if audio_data:
                        st.session_state["last_audio"] = audio_data
                        st.session_state["last_audio_format"] = audio_format
                    elif tts_error:
                        st.session_state["tts_error"] = tts_error

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
                run_multi_char_round(chat_characters, current_chat, base_url, model, temperature, max_tokens, tts_enabled)
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
                    dj_reply = call_lmstudio_chat_messages(
                        base_url=base_url, model=model,
                        messages=[{"role": "user", "content": dj_prompt}],
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
                        guest_reply = call_lmstudio_chat_messages(
                            base_url=base_url, model=model,
                            messages=[
                                {"role": "system", "content": guest_system},
                                {"role": "user", "content": f"{dj['name']}の発言:\n{prev_text}"},
                            ],
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

                dj_react_system = f"""あなたは「{dj['name']}」です。ラジオDJとしてゲストのコメントに軽くリアクションしてください。

【キャラクター設定】
{_radio_char_prompt(dj)}

【ルール】
- {guest["name"]}のことは「{guest_nick_from_dj}」と呼ぶ
- 1〜2文で軽く返す（共感やツッコミ）
- {closing_instruction}"""

                with st.spinner(f"📻 {dj['name']}がリアクション中…"):
                    try:
                        dj_react = call_lmstudio_chat_messages(
                            base_url=base_url, model=model,
                            messages=[
                                {"role": "system", "content": dj_react_system},
                                {"role": "user", "content": f"{guest['name']}のコメント:\n{guest_reply}"},
                            ],
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
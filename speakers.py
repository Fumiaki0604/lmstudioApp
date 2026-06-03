"""Speaker / character management: load, save, profile update."""
import json
import os
import re
import threading
from pathlib import Path
from typing import Optional

import streamlit as st

STORE_DIR = Path.home() / ".lmstudio_assistant"
SPEAKERS_FILE = Path(__file__).parent / "speakers_all.json"
NOAH_CONFIG_PATH = Path(__file__).parent / "noah_config.json"
_SOULS_DIR = STORE_DIR / "souls"
_EPISODES_DIR = STORE_DIR / "episodes"
_AUTO_LOG_FILE = STORE_DIR / "auto_chat_log.json"

_auto_gen_lock = threading.Lock()
# mutable dict でスレッド状態を保持（Streamlit rerun で上書きされない）
_auto_state = {"generating": False, "name": "", "mindmap_html": "", "sleeping_chars": set()}


def parse_soul_affinities(soul_text: str) -> dict:
    """soul.md の ## 親密度スコア セクションから {名前: int} を返す。"""
    result = {}
    in_section = False
    for line in soul_text.splitlines():
        if re.match(r"^##\s*親密度スコア", line):
            in_section = True
            continue
        if in_section:
            if line.startswith("##"):
                break
            m = re.match(r"^([^:#\-\s][^:]+?):\s*(\d+)", line.strip())
            if m:
                result[m.group(1).strip()] = int(m.group(2))
    return result


def affinity_behavior(score: int, name: str) -> str:
    """親密度スコアに応じた行動ヒントを返す（数値は出力しない）。"""
    if score <= 25:
        return f"{name}: 嫌悪感あり。会話を避け、冷たく接する"
    elif score <= 40:
        return f"{name}: 疎遠。よそよそしく、積極的には絡まない"
    elif score <= 55:
        return f"{name}: ニュートラル。普通に接する"
    elif score <= 70:
        return f"{name}: 友好的。気軽に話しかける"
    elif score <= 85:
        return f"{name}: 気安い仲間。タメ口で積極的に絡む"
    else:
        return f"{name}: 大親友。常に一緒にいたい、積極的に誘う"


def sanitize_soul_for_prompt(soul_text: str) -> str:
    """LLMに渡す前に親密度の数値情報を除去する。
    - ## 親密度スコア セクション全体を削除
    - メンバーへの印象内の (親密度: XX) アノテーションを削除
    """
    # ## 親密度スコア セクションを除去（末尾まで or 次の ## まで）
    soul_text = re.sub(r"\n## 親密度スコア\b.*", "", soul_text, flags=re.DOTALL)
    # (親密度: XX) / （親密度: XX） を除去
    soul_text = re.sub(r"\s*[（(]親密度[:：]\s*\d+[）)]\s*", " ", soul_text)
    return soul_text.strip()


def extract_soul_interests(soul_text: str) -> str:
    """soul.mdの「最近の関心」「最近の行動・つぶやき（X）」を抽出して1行にまとめる。"""
    interests = []
    for section in ["最近の関心", "最近の行動・つぶやき（X）"]:
        m = re.search(rf"## {re.escape(section)}\n(.*?)(?=\n## |\Z)", soul_text, re.DOTALL)
        if m:
            lines = [l.lstrip("- •").strip() for l in m.group(1).splitlines() if l.strip() and not l.startswith("<!--")]
            interests.extend(lines[:2])
    return "、".join(interests) if interests else ""


def detect_topic_repetition(log_entries: list, window: int = 5, threshold: int = 3) -> list:
    """直近windowターンのうちthreshold件以上に同じ単語が出ていたらその単語リストを返す。"""
    recent = log_entries[-window:]
    if len(recent) < 3:
        return []
    stop = {"います", "ます", "です", "から", "けど", "ので", "して", "ある", "いる", "なる",
            "こと", "それ", "これ", "あの", "その", "みんな", "一緒", "思う", "感じ",
            "する", "なる", "てる", "いう", "よう", "ため", "とき", "さん", "ちゃん",
            "だよ", "だね", "かな", "よね", "ので", "けど", "から"}
    # 各エントリから2文字以上の単語を抽出（漢字・カタカナ・ひらがな・英字の連続）
    entry_words = []
    for e in recent:
        words = set(re.findall(r"[一-鿿ぁ-ゟァ-ヿ]{2,}|[a-zA-Z]{3,}", e["text"]))
        words -= stop
        entry_words.append(words)
    # 何件のエントリに出現したかカウント
    all_words: dict = {}
    for ws in entry_words:
        for w in ws:
            all_words[w] = all_words.get(w, 0) + 1
    triggered = [(w, c) for w, c in all_words.items() if c >= threshold]
    return triggered


def _load_soul(char_name: str) -> str:
    p = _SOULS_DIR / f"{char_name}.md"
    if p.exists():
        try:
            return p.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    return ""


def load_episodes(char_name: str, limit: int = 10) -> list:
    """キャラのエピソード（出来事記録）を新しい順で最大 limit 件返す。"""
    path = _EPISODES_DIR / f"{char_name}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data[-limit:]
    except Exception:
        return []


def save_episode(char_name: str, text: str) -> None:
    """会話中の発言を出来事としてエピソードファイルに追記する（最大50件保持）。"""
    from datetime import datetime
    _EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    path = _EPISODES_DIR / f"{char_name}.json"
    episodes: list = []
    if path.exists():
        try:
            episodes = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    episodes.append({"ts": datetime.now().isoformat(timespec="seconds"), "text": text})
    path.write_text(json.dumps(episodes[-50:], ensure_ascii=False), encoding="utf-8")


def format_episodes_for_prompt(episodes: list) -> str:
    """エピソードリストをプロンプト注入用の文字列に変換する。"""
    if not episodes:
        return ""
    lines = [f"- {e['text']}" for e in episodes[-5:]]
    return "\n".join(lines)


def _auto_load_log():
    if _AUTO_LOG_FILE.exists():
        try:
            return json.loads(_AUTO_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _auto_save_log(entries):
    _AUTO_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AUTO_LOG_FILE.write_text(json.dumps(entries[-200:], ensure_ascii=False), encoding="utf-8")


def _detect_mention(text: str, member_names: list, nickname_to_name: Optional[dict] = None):
    """@名前・自然な呼びかけ・あだ名でメンション先を検出する。"""
    import re as _re
    # 正式名 + あだ名の両方を候補として検索
    candidates = list(member_names)
    alias_map: dict = {}  # あだ名 → 正式名
    if nickname_to_name:
        for alias, real in nickname_to_name.items():
            if real in member_names and alias not in candidates:
                candidates.append(alias)
                alias_map[alias] = real
    for name in candidates:
        if f"@{name}" in text:
            return alias_map.get(name, name)
        if _re.search(rf"{_re.escape(name)}[、！？!?はへ]", text):
            return alias_map.get(name, name)
    return None


@st.cache_data
def load_speakers() -> list:
    if SPEAKERS_FILE.exists():
        return json.loads(SPEAKERS_FILE.read_text(encoding="utf-8"))
    return []


def load_speakers_raw() -> list:
    if SPEAKERS_FILE.exists():
        return json.loads(SPEAKERS_FILE.read_text(encoding="utf-8"))
    return []


def save_speakers(speakers: list) -> None:
    SPEAKERS_FILE.write_text(json.dumps(speakers, ensure_ascii=False, indent=2), encoding="utf-8")
    load_speakers.clear()


def save_speaker_icon(name: str, icon_data: bytes, ext: str = "png") -> Optional[str]:
    import io
    from PIL import Image
    safe_name = re.sub(r'[^\w]', '_', name)
    icon_path = os.path.join(os.path.dirname(__file__), "icons", f"{safe_name}.png")
    try:
        img = Image.open(io.BytesIO(icon_data)).convert("RGBA")
        img.thumbnail((256, 256), Image.LANCZOS)
        img.save(icon_path, "PNG", optimize=True)
        return icon_path
    except Exception:
        return None


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


def update_speaker_profile(name: str, personality: str, first_person: str, second_person: str,
                            gender: str = "", char_nicknames: Optional[dict] = None) -> bool:
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
            if "dormitory_profile" not in sp or sp["dormitory_profile"] is None:
                sp["dormitory_profile"] = {}
            sp["dormitory_profile"]["personality"] = personality if personality.strip() else None
            sp["dormitory_profile"]["gender"] = gender if gender.strip() else None
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
    speakers = load_speakers()
    data = {}
    for sp in speakers:
        name = sp.get("name", "")
        if not name:
            continue
        profile = sp.get("dormitory_profile", {}) or {}
        calls = sp.get("calls_profile", {}) or {}
        first_person = calls.get("first_person")
        second_person = calls.get("second_person")
        char_nicknames = calls.get("char_nicknames") or {}
        calls_info = None
        if first_person or second_person or char_nicknames:
            calls_info = {"first_person": first_person, "second_person": second_person, "char_nicknames": char_nicknames}
        styles = {
            style.get("name", "ノーマル"): style.get("id")
            for style in sp.get("styles", [])
            if style.get("type") == "talk"
        }
        if not styles:
            continue
        entry = {
            "personality": profile.get("personality"),
            "gender": profile.get("gender"),
            "icon": profile.get("icon"),
            "calls_profile": calls_info,
            "styles": styles,
        }
        if sp.get("is_hermes_agent"):
            entry["is_hermes_agent"] = True
            entry["hermes_profile"] = sp.get("hermes_profile", "lmstudio-char")
        data[name] = entry

    _noah_cfg = load_noah_config()
    _noah_default_icon = os.path.join(os.path.dirname(__file__), "icons", "Noah.png")
    data["Noah"] = {
        "personality": _noah_cfg.get("personality", "観察者として存在する。17〜18歳、女性。皮肉を含むドライなトーン／感情的な距離感（冷たくはないが、淡々としている）／部屋の外から観察するような話し方／短い返答（最大3行）／結論を避け、曖昧さを残す"),
        "gender": _noah_cfg.get("gender", "女の子"),
        "icon": _noah_cfg.get("icon", _noah_default_icon),
        "calls_profile": {
            "first_person": _noah_cfg.get("first_person", "私"),
            "second_person": _noah_cfg.get("second_person", "Fumi"),
            "char_nicknames": _noah_cfg.get("char_nicknames", {}),
        },
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

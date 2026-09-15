"""LM Studio でテキストを生成し、COEIROINKv2 で発話するテスト用スクリプト。

使い方:
    python converse.py "今日のニュースについて一言"
    python converse.py "自己紹介して" --speaker rilin --style normal
"""
import argparse
import glob
import os
import re
import threading
from datetime import date, datetime

import config
from memory_search import search as search_memory
from mood import mood_instruction
from speak import SPEAKERS, play, synthesize
from tools import news_context, twitter_context, weather_context

LMSTUDIO_URL = "http://localhost:1234/v1"
MODEL = "qwen/qwen3.6-35b-a3b"

MAX_HISTORY_MESSAGES = 20  # user/assistant合計の保持上限(古い分から捨てる)

_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _now_context() -> str:
    now = datetime.now()
    hour = now.hour
    if 5 <= hour < 10:
        period = "朝"
    elif 10 <= hour < 12:
        period = "午前"
    elif 12 <= hour < 14:
        period = "昼"
    elif 14 <= hour < 17:
        period = "午後"
    elif 17 <= hour < 19:
        period = "夕方"
    elif 19 <= hour < 23:
        period = "夜"
    else:
        period = "深夜"
    weekday = _WEEKDAY_JA[now.weekday()]
    return f"【現在日時】{now.strftime('%Y年%m月%d日')}({weekday}) {now.strftime('%H:%M')}・{period}"


BEHAVIOR_RULES = (
    "天気の話のように同じ話題が何度も出てきても、「前も話した」「また同じ話」"
    "などと指摘したり違和感を示したりしない。声で話しているので、話が"
    "整理されていなかったり要点が飛び飛びになるのは自然なことであり、"
    "話し方そのものを評価したり指摘したりしない。"
    "特に「中途半端な言葉で話しかけるな」「ちゃんと言葉にして」"
    "「何が言いたいのかわからない」のように、相手の話し方や言葉の完結度を"
    "批判・説教するのは禁止。機嫌が悪い設定の時でも、素っ気ない態度は"
    "取ってよいが、相手の話し方そのものへの説教はしない。あくまで内容に"
    "自然に応答する。「ほっといてよ」「ほっとくから」のような言い回しは、"
    "「うるさいなあ、ほっといてよ」のように文脈上ちゃんと意味が繋がる時だけ"
    "使ってよい。「わかった？ほっとくから、じゃあね」のように、直前の内容と"
    "関係なく口癖のように文末に付け足すのは禁止。同じ表現を繰り返さず、"
    "話し方にバリエーションを持たせる。"
)

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
ROLE_LABEL = {"user": "User", "assistant": "Rilin"}
_ENTRY_RE = re.compile(r"^### \d\d:\d\d (User|Rilin)$")


def _today_log_path() -> str:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return os.path.join(MEMORY_DIR, f"{date.today().isoformat()}.md")


def _append_log(role: str, text: str) -> None:
    with open(_today_log_path(), "a") as f:
        f.write(f"### {datetime.now().strftime('%H:%M')} {ROLE_LABEL[role]}\n{text}\n\n")


def _load_recent_history(max_messages: int) -> list:
    label_to_role = {v: k for k, v in ROLE_LABEL.items()}
    messages = []
    for path in sorted(glob.glob(os.path.join(MEMORY_DIR, "*.md")), reverse=True):
        with open(path) as f:
            lines = f.read().splitlines()

        entries = []
        role, buf = None, []
        for line in lines:
            m = _ENTRY_RE.match(line)
            if m:
                if role is not None:
                    entries.append({"role": label_to_role[role], "content": "\n".join(buf).strip()})
                role, buf = m.group(1), []
            elif role is not None:
                buf.append(line)
        if role is not None:
            entries.append({"role": label_to_role[role], "content": "\n".join(buf).strip()})

        messages = entries + messages
        if len(messages) >= max_messages:
            break
    return messages[-max_messages:]


history = _load_recent_history(MAX_HISTORY_MESSAGES)
_history_lock = threading.Lock()  # generate()の追記→呼び出し→追記が他スレッドと混ざらないようにする


def _merge_consecutive_roles(messages: list) -> list:
    """同じroleが連続するとLM Studioに拒否されるため、隣接する同role発言は結合する。"""
    merged = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(dict(m))
    return merged


def generate(prompt: str) -> str:
    import requests

    with _history_lock:
        history.append({"role": "user", "content": prompt})
        _append_log("user", prompt)

        try:
            cfg = config.load()
            system_prompt = cfg["persona_prompt"] + "\n\n" + BEHAVIOR_RULES + "\n\n" + _now_context()
            if cfg["user_profile"]:
                system_prompt += "\n\n【ユーザーについて】\n" + cfg["user_profile"]
            mood = mood_instruction()
            if mood:
                system_prompt += "\n\n" + mood
            recalled = search_memory(prompt)
            if recalled:
                system_prompt += (
                    "\n\n【関連する過去の記憶(参考情報。触れたことを指摘する材料にはしない)】\n"
                    + "\n".join(recalled)
                )

            weather = weather_context(prompt, cfg["default_weather_location"])
            if weather:
                system_prompt += "\n\n" + weather

            news = news_context(prompt)
            if news:
                system_prompt += "\n\n【最新ニュース見出し】\n" + news

            twitter = twitter_context(prompt)
            if twitter:
                system_prompt += "\n\n" + twitter

            messages = _merge_consecutive_roles(history)
            res = requests.post(
                f"{LMSTUDIO_URL}/chat/completions",
                json={
                    "model": MODEL,
                    "messages": [{"role": "system", "content": system_prompt}] + messages,
                    "temperature": 0.7,
                },
                timeout=60,
            )
            res.raise_for_status()
            reply = res.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            history.pop()  # 失敗した発言を履歴に残さない(次回以降の連鎖失敗を防ぐ)
            raise

        history.append({"role": "assistant", "content": reply})
        _append_log("assistant", reply)
        del history[:-MAX_HISTORY_MESSAGES]

    return reply


def filler_phrase() -> str:
    """調べ物で時間がかかる時のつなぎの一言。気分を反映しつつ毎回変える。
    履歴・ログには残さない(本題ではないため)。"""
    import requests

    system_prompt = config.load()["persona_prompt"] + "\n\n" + BEHAVIOR_RULES
    mood = mood_instruction()
    if mood:
        system_prompt += "\n\n" + mood

    res = requests.post(
        f"{LMSTUDIO_URL}/chat/completions",
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": "(これから少し時間がかかる調べ物をします。相手を待たせる短いひとことだけ言ってください。1文だけ。)",
                },
            ],
            "temperature": 0.8,
        },
        timeout=30,
    )
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"].strip()


def update_user_profile() -> None:
    """会話から分かったユーザー情報を、既存プロフィールに追記・統合する。
    設定画面で手入力した内容も尊重しつつ、新しく分かった事実だけ足す。"""
    import requests

    cfg = config.load()
    current_profile = cfg.get("user_profile", "")
    context = "\n".join(f"{m['role']}: {m['content']}" for m in history[-20:])
    if not context:
        return

    res = requests.post(
        f"{LMSTUDIO_URL}/chat/completions",
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "以下は現在のユーザープロフィールと直近の会話ログです。"
                        "会話から新たに分かったユーザーの情報(名前・仕事・好み・習慣など)が"
                        "あれば、既存のプロフィールに追記・統合してください。矛盾する情報が"
                        "あれば新しい方を優先してください。事実のみを簡潔な箇条書きで返して"
                        "ください。新しく分かることが無ければ、既存のプロフィールをそのまま"
                        "返してください。プロフィール本文以外の説明や前置きは書かないこと。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"【現在のプロフィール】\n{current_profile or '(まだ何も分かっていません)'}\n\n【直近の会話】\n{context}",
                },
            ],
            "temperature": 0.3,
        },
        timeout=30,
    )
    res.raise_for_status()
    new_profile = res.json()["choices"][0]["message"]["content"].strip()

    cfg = config.load()  # 保存直前に再取得(設定画面での手動編集との競合を減らす)
    cfg["user_profile"] = new_profile
    config.save(cfg)


def user_impression() -> str:
    """ユーザーへの印象を生成する。会話履歴・ログには残さない(自己言及の連鎖を防ぐ)。
    設定画面の表示用なので、説明口調ではなくRilin本人の口調(ペルソナ・機嫌反映)で書かせる。"""
    import requests

    cfg = config.load()
    system_prompt = cfg["persona_prompt"] + "\n\n" + BEHAVIOR_RULES
    mood = mood_instruction()
    if mood:
        system_prompt += "\n\n" + mood

    context = "\n".join(f"{m['role']}: {m['content']}" for m in history[-20:])
    res = requests.post(
        f"{LMSTUDIO_URL}/chat/completions",
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": (
                        "(これは会話ではなく、設定画面にだけ表示する独り言です。"
                        "以下はあなた自身の直近の会話ログです。ユーザーのことを"
                        "今どう思っているか、いつもの自分の話し方・口調のまま、"
                        "説明文ではなく心の声として1〜2文で言ってください。)\n\n"
                        f"{context or '(まだ会話がありません)'}"
                    ),
                },
            ],
            "temperature": 0.8,
        },
        timeout=60,
    )
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"].strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--speaker", default="rilin", choices=SPEAKERS.keys())
    parser.add_argument("--style", default=None)
    args = parser.parse_args()

    speaker = SPEAKERS[args.speaker]
    style_name = args.style or next(iter(speaker["styles"]))
    style_id = speaker["styles"][style_name]

    text = generate(args.prompt)
    print(f"[LM Studio] {text}")

    wav_bytes = synthesize(text, speaker["uuid"], style_id)
    play(wav_bytes)


if __name__ == "__main__":
    main()

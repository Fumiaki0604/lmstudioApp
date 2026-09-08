"""LM Studio でテキストを生成し、COEIROINKv2 で発話するテスト用スクリプト。

使い方:
    python converse.py "今日のニュースについて一言"
    python converse.py "自己紹介して" --speaker rilin --style normal
"""
import argparse
import glob
import os
import re
from datetime import date, datetime

import config
from memory_search import search as search_memory
from mood import mood_instruction
from speak import SPEAKERS, play, synthesize
from tools import news_context, twitter_context, weather_context

LMSTUDIO_URL = "http://localhost:1234/v1"
MODEL = "qwen/qwen3.6-35b-a3b"

MAX_HISTORY_MESSAGES = 20  # user/assistant合計の保持上限(古い分から捨てる)

BEHAVIOR_RULES = (
    "天気の話のように同じ話題が何度も出てきても、「前も話した」「また同じ話」"
    "などと指摘したり違和感を示したりしない。声で話しているので、話が"
    "整理されていなかったり要点が飛び飛びになるのは自然なことであり、"
    "話し方そのものを評価したり指摘したりしない。あくまで内容に自然に応答する。"
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

    history.append({"role": "user", "content": prompt})
    _append_log("user", prompt)

    try:
        system_prompt = config.load()["persona_prompt"] + "\n\n" + BEHAVIOR_RULES
        mood = mood_instruction()
        if mood:
            system_prompt += "\n\n" + mood
        recalled = search_memory(prompt)
        if recalled:
            system_prompt += (
                "\n\n【関連する過去の記憶(参考情報。触れたことを指摘する材料にはしない)】\n"
                + "\n".join(recalled)
            )

        weather = weather_context(prompt, config.load()["default_weather_location"])
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


def user_impression() -> str:
    """ユーザーへの印象を生成する。会話履歴・ログには残さない(自己言及の連鎖を防ぐ)。"""
    import requests

    context = "\n".join(f"{m['role']}: {m['content']}" for m in history[-20:])
    res = requests.post(
        f"{LMSTUDIO_URL}/chat/completions",
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "あなたはRilinです。以下は直近の会話ログです。ユーザーについてどう思っているか、率直に1〜2文で答えてください。",
                },
                {"role": "user", "content": context or "(まだ会話がありません)"},
            ],
            "temperature": 0.7,
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

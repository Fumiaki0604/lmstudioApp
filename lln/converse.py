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

from speak import SPEAKERS, play, synthesize

LMSTUDIO_URL = "http://localhost:1234/v1"
MODEL = "qwen/qwen3.6-35b-a3b"

SYSTEM_PROMPT = (
    "あなたは音声で話しかけてくるフレンドリーなアシスタントです。"
    "短く自然な話し言葉で、1〜2文で答えてください。"
)

MAX_HISTORY_MESSAGES = 20  # user/assistant合計の保持上限(古い分から捨てる)

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


def generate(prompt: str) -> str:
    import requests

    history.append({"role": "user", "content": prompt})
    _append_log("user", prompt)

    res = requests.post(
        f"{LMSTUDIO_URL}/chat/completions",
        json={
            "model": MODEL,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + history,
            "temperature": 0.7,
        },
        timeout=60,
    )
    res.raise_for_status()
    reply = res.json()["choices"][0]["message"]["content"].strip()

    history.append({"role": "assistant", "content": reply})
    _append_log("assistant", reply)
    del history[:-MAX_HISTORY_MESSAGES]

    return reply


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

"""LM Studio でテキストを生成し、COEIROINKv2 で発話するテスト用スクリプト。

使い方:
    python converse.py "今日のニュースについて一言"
    python converse.py "自己紹介して" --speaker rilin --style normal
"""
import argparse

from speak import SPEAKERS, play, synthesize

LMSTUDIO_URL = "http://localhost:1234/v1"
MODEL = "qwen/qwen3.6-35b-a3b"

SYSTEM_PROMPT = (
    "あなたは音声で話しかけてくるフレンドリーなアシスタントです。"
    "短く自然な話し言葉で、1〜2文で答えてください。"
)


def generate(prompt: str) -> str:
    import requests

    res = requests.post(
        f"{LMSTUDIO_URL}/chat/completions",
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
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

"""COEIROINKv2 経由でテキストを音声合成し再生する発信テスト用スクリプト。

使い方:
    python speak.py "こんにちは、テストです。"
    python speak.py "テスト" --speaker rilin --style whisper
"""
import argparse
import subprocess
import sys
import tempfile

import requests

COEIROINK_URL = "http://localhost:50032"

SPEAKERS = {
    "rilin": {
        "uuid": "cb11bdbd-78fc-4f16-b528-a400bae1782d",
        "styles": {
            "normal": 90,
            "whisper": 91,
            "mesugaki": 92,
            "rikaisare": 93,
        },
    },
    "tsukuyomi": {
        "uuid": "3c37646f-3881-5374-2a83-149267990abc",
        "styles": {
            "reisei": 0,
        },
    },
}


def synthesize(text: str, speaker_uuid: str, style_id: int) -> bytes:
    payload = {
        "speakerUuid": speaker_uuid,
        "styleId": style_id,
        "text": text,
        "speedScale": 1.0,
        "volumeScale": 1.0,
        "pitchScale": 0.0,
        "intonationScale": 1.0,
        "prePhonemeLength": 0.1,
        "postPhonemeLength": 0.1,
        "outputSamplingRate": 44100,
    }
    res = requests.post(f"{COEIROINK_URL}/v1/synthesis", json=payload, timeout=30)
    res.raise_for_status()
    return res.content


def play(wav_bytes: bytes) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(wav_bytes)
        f.flush()
        subprocess.run(["afplay", f.name], check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("text")
    parser.add_argument("--speaker", default="rilin", choices=SPEAKERS.keys())
    parser.add_argument("--style", default=None)
    args = parser.parse_args()

    speaker = SPEAKERS[args.speaker]
    style_name = args.style or next(iter(speaker["styles"]))
    if style_name not in speaker["styles"]:
        sys.exit(f"unknown style '{style_name}' for speaker '{args.speaker}'")
    style_id = speaker["styles"][style_name]

    wav_bytes = synthesize(args.text, speaker["uuid"], style_id)
    play(wav_bytes)


if __name__ == "__main__":
    main()

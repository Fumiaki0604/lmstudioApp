"""プロアクティブ発話のスケジューラ雛形。

在室中 かつ 静穏時間外 のとき、不定期な間隔でLM Studioに一言を
生成させ、COEIROINKで話しかける。

使い方:
    python trigger.py                          # 本番間隔(20〜90分)で動かしっぱなしにする
    python trigger.py --once                   # ゲート判定込みで1回だけ即座に試す
    python trigger.py --min-sec 5 --max-sec 10  # 短い間隔でループ動作を確認する
"""
import argparse
import random
import time

from converse import generate
from gate import can_speak_now
from speak import SPEAKERS, play, synthesize

MIN_INTERVAL_SEC = 20 * 60  # 20分
MAX_INTERVAL_SEC = 90 * 60  # 90分

PROACTIVE_PROMPT = (
    "(これはユーザーからの発言ではありません。あなたから自発的に話しかけてください。"
    "挨拶や雑談、気になっていることなど、短く自然な一言にしてください。)"
)


def speak_once(speaker: str = "rilin", style: str = None) -> None:
    speaker_info = SPEAKERS[speaker]
    style_name = style or next(iter(speaker_info["styles"]))
    style_id = speaker_info["styles"][style_name]

    text = generate(PROACTIVE_PROMPT)
    print(f"[proactive] {text}")
    wav = synthesize(text, speaker_info["uuid"], style_id)
    play(wav)


def run(min_sec: float, max_sec: float, speaker: str = "rilin", style: str = None) -> None:
    while True:
        wait_sec = random.uniform(min_sec, max_sec)
        print(f"次の発話判定まで約{wait_sec / 60:.1f}分待機")
        time.sleep(wait_sec)

        if can_speak_now():
            speak_once(speaker, style)
        else:
            print("発話条件を満たさずスキップ(不在 or 静穏時間)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="ゲート判定込みで1回だけ実行")
    parser.add_argument("--min-sec", type=float, default=MIN_INTERVAL_SEC)
    parser.add_argument("--max-sec", type=float, default=MAX_INTERVAL_SEC)
    parser.add_argument("--speaker", default="rilin", choices=SPEAKERS.keys())
    parser.add_argument("--style", default=None)
    args = parser.parse_args()

    if args.once:
        if can_speak_now():
            speak_once(args.speaker, args.style)
        else:
            print("発話条件を満たさず(不在 or 静穏時間)")
        return

    run(args.min_sec, args.max_sec, args.speaker, args.style)


if __name__ == "__main__":
    main()

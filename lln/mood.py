"""リリンの機嫌バイオリズム。

日付からサイン波で機嫌値(-1.0〜1.0)を決める。乱数ではなく決定論的な
波なので、日をまたいで急変せず、同じ日なら常に同じ機嫌になる。
振れ幅の中心(-0.6〜0.6)は「普通」で、山谷の近くだけ良い/悪い機嫌になる。
"""
import math
from datetime import date

MOOD_EPOCH = date(2026, 1, 1)
MOOD_PERIOD_DAYS = 17  # 機嫌が一巡する日数
GOOD_THRESHOLD = 0.6
BAD_THRESHOLD = -0.6

MOOD_INSTRUCTIONS = {
    "good": "今日は機嫌がとてもいい。テンション高めで、少し甘えるような話し方をする。",
    "bad": "今日は機嫌が悪い。ぶっきらぼうで、言葉遣いが少し荒くなる。",
    "neutral": "",
}


def mood_value(today: date = None) -> float:
    today = today or date.today()
    days = (today - MOOD_EPOCH).days
    return math.sin(2 * math.pi * days / MOOD_PERIOD_DAYS)


def mood_label(today: date = None) -> str:
    v = mood_value(today)
    if v > GOOD_THRESHOLD:
        return "good"
    if v < BAD_THRESHOLD:
        return "bad"
    return "neutral"


def mood_instruction(today: date = None) -> str:
    return MOOD_INSTRUCTIONS[mood_label(today)]


if __name__ == "__main__":
    print(f"today mood: {mood_label()} (value={mood_value():.2f})")

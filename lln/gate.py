"""能動発話を許可するかどうかの判定。

在室していても、静かにしてほしい時間帯(深夜〜早朝)は話しかけない。
将来のスケジューラは、発話前に can_speak_now() を必ず呼ぶ想定。
"""
from datetime import datetime
from typing import Optional

from presence import is_home

QUIET_HOUR_START = 23  # この時刻以降は静か
QUIET_HOUR_END = 9  # この時刻より前は静か


def in_quiet_hours(now: Optional[datetime] = None) -> bool:
    now = now or datetime.now()
    hour = now.hour
    if QUIET_HOUR_START > QUIET_HOUR_END:
        # 23時〜翌9時のように日をまたぐ範囲
        return hour >= QUIET_HOUR_START or hour < QUIET_HOUR_END
    return QUIET_HOUR_START <= hour < QUIET_HOUR_END


def can_speak_now(now: Optional[datetime] = None) -> bool:
    if in_quiet_hours(now):
        return False
    return is_home()


def main() -> None:
    now = datetime.now()
    print(f"time={now.strftime('%H:%M')} quiet_hours={in_quiet_hours(now)} can_speak={can_speak_now(now)}")


if __name__ == "__main__":
    main()

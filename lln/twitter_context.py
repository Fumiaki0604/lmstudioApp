"""AI関連Xリストの最新投稿を要約する(twikit、Python 3.11の.venv311経由)。

twikitはPython 3.9では動かせない(2.x系がX|Y型ユニオン構文を要求)ため、
lln/.venv311 に隔離したPython 3.11環境で動かす。呼び出し元(converse.py)
は subprocess 経由でこのファイル自体を .venv311 の python で叩く。
"""
import asyncio
import json
import sys

import twikit_patch
from twikit import Client

COOKIES_PATH = "/Users/fumiakisato/.lmstudio_assistant/twitter_cookies.json"
LIST_ID = "2079199733495775662"


async def _fetch(count: int = 10):
    twikit_patch.apply()
    client = Client(language="ja-JP")
    client.load_cookies(COOKIES_PATH)
    tweets = await client.get_list_tweets(LIST_ID, count=count)
    return [
        {"user": t.user.screen_name, "text": (t.full_text or t.text or "").replace("\n", " ")}
        for t in tweets
    ]


def main() -> None:
    try:
        tweets = asyncio.run(_fetch())
        print(json.dumps({"ok": True, "tweets": tweets}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()

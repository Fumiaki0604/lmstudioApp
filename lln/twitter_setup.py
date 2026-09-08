"""twikit用cookieの保存(lln版)。ホームタイムライン取得で動作確認する。"""
import asyncio
import json
from pathlib import Path

COOKIES_PATH = Path.home() / ".lmstudio_assistant" / "twitter_cookies.json"


async def verify(client) -> bool:
    tweets = await client.get_timeline(count=1)
    return len(tweets) > 0


def main():
    from twikit import Client

    print("ブラウザ開発者ツール → Application → Cookies → https://x.com")
    auth_token = input("auth_token: ").strip()
    ct0 = input("ct0: ").strip()

    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_PATH.write_text(json.dumps({"auth_token": auth_token, "ct0": ct0}))

    client = Client(language="ja-JP")
    client.load_cookies(str(COOKIES_PATH))

    print("\n動作確認中(ホームタイムライン取得)...")
    try:
        ok = asyncio.run(verify(client))
        if ok:
            print(f"成功。cookie保存済み: {COOKIES_PATH}")
        else:
            print("認証は通ったがタイムラインが空でした。cookieは保存済みです。")
    except Exception as e:
        print(f"確認呼び出しは失敗しましたが、cookieは削除せず保存したままにします: {e}")


if __name__ == "__main__":
    main()

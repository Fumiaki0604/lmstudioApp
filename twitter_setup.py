"""ブラウザcookieをtwikit用に保存するセットアップスクリプト。"""
import json
from pathlib import Path

COOKIES_PATH = Path.home() / ".lmstudio_assistant" / "twitter_cookies.json"


def main():
    from twikit import Client

    print("ブラウザ開発者ツール → Application → Cookies → https://x.com")
    auth_token = input("auth_token: ").strip()
    ct0 = input("ct0: ").strip()

    COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_PATH.write_text(json.dumps({"auth_token": auth_token, "ct0": ct0}))

    client = Client(language="ja-JP")
    client.load_cookies(str(COOKIES_PATH))

    print("\n動作確認中...")
    try:
        user = client.get_user_by_screen_name("hau_hareu")
        print(f"成功: {user.name} (@{user.screen_name})")
        print(f"cookie保存済み: {COOKIES_PATH}")
    except Exception as e:
        print(f"失敗: {e}")
        COOKIES_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

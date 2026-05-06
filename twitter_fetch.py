"""VoiceVoxキャラの公式Xアカウントからツイートを取得してsoul.mdに反映する。"""
import json
from pathlib import Path
from datetime import datetime

COOKIES_PATH = Path.home() / ".lmstudio_assistant" / "twitter_cookies.json"
SOULS_DIR = Path.home() / ".lmstudio_assistant" / "souls"

# キャラ名 → Xアカウント対応表
CHAR_ACCOUNTS = {
    "雨晴はう": "hau_hareu",
    "春日部つむぎ": "KasukabeTsumugi",
    "小夜": "NEKO_NO_SAYO",
    "冥鳴ひまり": "HimariKTRN",
    "WhiteCUL": "Whitecul_yuki",
    "もち子": "V_Mochiko",
    "東北ずん子": "t_zunko",
    "ぞん子": "zone_eculture",
    "voidoll": "cps_niconico",
}

MAX_TWEETS = 8


def fetch_tweets(client, screen_name: str) -> list[str]:
    """オリジナル + リポストを取得（URL除去・空テキスト除外）。"""
    import re
    user = client.get_user_by_screen_name(screen_name)
    tweets = client.get_user_tweets(user.id, tweet_type="Tweets", count=20)
    results = []
    for tweet in tweets:
        raw = tweet.retweeted_tweet.text if tweet.retweeted_tweet else tweet.text
        text = re.sub(r"https://t\.co/\S+", "", raw).strip()
        if not text:
            continue
        results.append(text)
        if len(results) >= MAX_TWEETS:
            break
    return results


def update_soul(char_name: str, tweets: list[str]) -> None:
    soul_path = SOULS_DIR / f"{char_name}.md"
    if not soul_path.exists():
        print(f"  soul.md が見つかりません: {char_name}")
        return

    content = soul_path.read_text(encoding="utf-8")
    section_header = "## 最近の行動・つぶやき（X）"
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_section = f"{section_header}\n<!-- {updated_at} 自動更新 -->\n"
    for t in tweets:
        new_section += f"- {t}\n"

    # 既存セクションを置き換え or 末尾に追加
    if section_header in content:
        lines = content.split("\n")
        result = []
        in_section = False
        for line in lines:
            if line.strip() == section_header:
                in_section = True
                result.append(new_section.rstrip())
                continue
            if in_section and line.startswith("## "):
                in_section = False
            if not in_section:
                result.append(line)
        content = "\n".join(result)
    else:
        content = content.rstrip() + "\n\n" + new_section.rstrip()

    soul_path.write_text(content, encoding="utf-8")
    print(f"  更新完了: {char_name} ({len(tweets)}件)")


def main():
    if not COOKIES_PATH.exists():
        print("cookieが見つかりません。twitter_setup.py を先に実行してください。")
        return

    from twikit import Client
    client = Client(language="ja-JP")
    client.load_cookies(str(COOKIES_PATH))

    for char_name, screen_name in CHAR_ACCOUNTS.items():
        print(f"{char_name} (@{screen_name}) を取得中...")
        try:
            tweets = fetch_tweets(client, screen_name)
            print(f"  {len(tweets)}件取得")
            for t in tweets[:3]:
                print(f"    - {t[:60]}")
            update_soul(char_name, tweets)
        except Exception as e:
            print(f"  失敗: {e}")


if __name__ == "__main__":
    main()

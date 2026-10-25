"""Discordへの投稿(Incoming Webhook経由)。Bot登録や認証は不要。"""
import requests

DISCORD_MESSAGE_LIMIT = 2000


def post(webhook_url: str, content: str) -> None:
    if not webhook_url:
        return
    if len(content) > DISCORD_MESSAGE_LIMIT:
        content = content[: DISCORD_MESSAGE_LIMIT - 10] + "\n…(省略)"
    try:
        requests.post(webhook_url, json={"content": content}, timeout=10).raise_for_status()
    except Exception as e:
        print(f"[error] discord post: {e}")

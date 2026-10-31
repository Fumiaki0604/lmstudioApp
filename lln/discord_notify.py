"""Discordへの投稿(Incoming Webhook経由)。Bot登録や認証は不要。"""
import json
import os

import requests

DISCORD_MESSAGE_LIMIT = 2000


def post(webhook_url: str, content: str, files: list = None) -> None:
    """filesにパスを渡すと添付ファイルとして送る(長い全文は本文ではなくファイルで送るため)。"""
    if not webhook_url:
        return
    if len(content) > DISCORD_MESSAGE_LIMIT:
        content = content[: DISCORD_MESSAGE_LIMIT - 10] + "\n…(省略)"
    try:
        if files:
            handles = [open(p, "rb") for p in files]
            try:
                requests.post(
                    webhook_url,
                    data={"payload_json": json.dumps({"content": content})},
                    files={f"files[{i}]": (os.path.basename(p), h) for i, (p, h) in enumerate(zip(files, handles))},
                    timeout=60,
                ).raise_for_status()
            finally:
                for h in handles:
                    h.close()
        else:
            requests.post(webhook_url, json={"content": content}, timeout=10).raise_for_status()
    except Exception as e:
        print(f"[error] discord post: {e}")

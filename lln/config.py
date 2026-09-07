"""音声オンオフ・ペルソナ・話者設定の永続化。config.json に保存する。"""
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULTS = {
    "voice_enabled": True,
    "persona_prompt": (
        "あなたは音声で話しかけてくるフレンドリーなアシスタントです。"
        "短く自然な話し言葉で、1〜2文で答えてください。"
    ),
    "speaker": "rilin",
    "style": None,
}


def load() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            data = json.load(f)
        return {**DEFAULTS, **data}
    return dict(DEFAULTS)


def save(config: dict) -> None:
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

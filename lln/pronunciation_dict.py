"""COEIROINKの辞書機能(/v1/set_dictionary)に、読み間違いしやすい単語を
あらかじめ登録する。登録内容はconfig.json(pronunciation_entries)で管理し、
設定画面(settings_app.py)から編集できる。

set_dictionaryは呼ぶたびに辞書全体を置き換える(差分追加ではない)ため、
毎回config上の全件を送り直す。
"""
import requests

import config
from speak import COEIROINK_URL

_SMALL_KANA = set("ゃゅょャュョ")  # 拗音は前のモーラに含まれ、独立したモーラ数にはならない


def _count_moras(yomi: str) -> int:
    return sum(1 for ch in yomi if ch not in _SMALL_KANA)


def apply(entries: list = None) -> None:
    """entriesを省略するとconfig.jsonの現在値を使う。
    設定画面で保存直後に即反映させたい場合は、保存したentriesをそのまま渡す。"""
    if entries is None:
        entries = config.load()["pronunciation_entries"]

    dictionary_words = [
        {
            "word": e["word"],
            "yomi": e["yomi"],
            "accent": e["accent"],
            "numMoras": _count_moras(e["yomi"]),
        }
        for e in entries
        if e.get("word") and e.get("yomi")
    ]
    requests.post(
        f"{COEIROINK_URL}/v1/set_dictionary",
        json={"dictionaryWords": dictionary_words},
        timeout=10,
    ).raise_for_status()


if __name__ == "__main__":
    entries = config.load()["pronunciation_entries"]
    apply(entries)
    print(f"{len(entries)}件の読みを登録しました。")

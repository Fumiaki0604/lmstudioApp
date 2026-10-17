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


def _to_katakana(text: str) -> str:
    """COEIROINKの辞書はひらがなを渡すとその単語が無音になることを実測で確認した
    (例: 「ひま」で登録すると「暇」が読み上げられなくなる)。設定画面の列名は
    「読み(カタカナ)」だが、ひらがなで入力されても自動補正して壊れないようにする。
    ひらがな(U+3041-3093)とカタカナ(U+30A1-30F3)はUnicode上0x60差なので変換できる。"""
    return "".join(chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ん" else ch for ch in text)


def _safe_accent(value) -> int:
    """設定画面の表でアクセント核を空欄のまま保存すると、StreamlitのNumberColumn
    はNone/NaNを返す。NaNはrequestsのjson=がstrict JSON非準拠として送信時に
    エラーにするため、ここで0(平板)にフォールバックさせる。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def apply(entries: list = None) -> None:
    """entriesを省略するとconfig.jsonの現在値を使う。
    設定画面で保存直後に即反映させたい場合は、保存したentriesをそのまま渡す。"""
    if entries is None:
        entries = config.load()["pronunciation_entries"]

    dictionary_words = []
    for e in entries:
        if not e.get("word") or not e.get("yomi"):
            continue
        yomi = _to_katakana(e["yomi"])
        dictionary_words.append({
            "word": e["word"],
            "yomi": yomi,
            "accent": _safe_accent(e.get("accent")),
            "numMoras": _count_moras(yomi),
        })
    requests.post(
        f"{COEIROINK_URL}/v1/set_dictionary",
        json={"dictionaryWords": dictionary_words},
        timeout=10,
    ).raise_for_status()


if __name__ == "__main__":
    entries = config.load()["pronunciation_entries"]
    apply(entries)
    print(f"{len(entries)}件の読みを登録しました。")

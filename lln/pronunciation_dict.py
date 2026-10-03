"""COEIROINKの辞書機能(/v1/set_dictionary)に、読み間違いしやすい単語を
あらかじめ登録する。起動時に一度呼ぶだけで、追加の推論コストは無い。

set_dictionaryは呼ぶたびに辞書全体を置き換える(差分追加ではない)ため、
ここで一括管理する。読み間違いを見つけたらENTRIESに追記すればよい。
"""
import requests

from speak import COEIROINK_URL

_SMALL_KANA = set("ゃゅょャュョ")  # 拗音は前のモーラに含まれ、独立したモーラ数にはならない


def _count_moras(yomi: str) -> int:
    return sum(1 for ch in yomi if ch not in _SMALL_KANA)


# word: 実際の表記(漢字・ひらがな等) / yomi: 読ませたいカタカナ
# accent: アクセント核の位置(0=平板型。核の位置が分かる場合だけ指定すればよい)
ENTRIES = [
    {"word": "リリン", "yomi": "リリン", "accent": 0},
]


def apply() -> None:
    dictionary_words = [
        {
            "word": e["word"],
            "yomi": e["yomi"],
            "accent": e["accent"],
            "numMoras": _count_moras(e["yomi"]),
        }
        for e in ENTRIES
    ]
    requests.post(
        f"{COEIROINK_URL}/v1/set_dictionary",
        json={"dictionaryWords": dictionary_words},
        timeout=10,
    ).raise_for_status()


if __name__ == "__main__":
    apply()
    print(f"{len(ENTRIES)}件の読みを登録しました。")

"""機嫌(good/bad/neutral)ごとのRilinの画像を保存・参照する。

アップロードされた画像は形式を問わずPNGに変換して保存する(表示側で
拡張子違いを気にしなくて済むようにするため)。設定画面(settings_app.py)
からのアップロードと、現在の機嫌に応じた画像の参照の両方で使う。
"""
import os

from PIL import Image

MOOD_IMAGE_DIR = os.path.join(os.path.dirname(__file__), "mood_images")
MOOD_LABELS = ("good", "bad", "neutral")


def mood_image_path(mood: str) -> str:
    return os.path.join(MOOD_IMAGE_DIR, f"{mood}.png")


def get_mood_image(mood: str):
    """該当する機嫌の画像パスを返す。未アップロードならNone。"""
    path = mood_image_path(mood)
    return path if os.path.exists(path) else None


def save_mood_image(mood: str, file_obj) -> None:
    """st.file_uploaderが返すファイルオブジェクトを受け取り、PNGとして保存する。

    Streamlitはウィジェットの状態変化で自動的にスクリプトを再実行するため、
    保存処理と並行して別の再実行が同じパスを読みに行くことがある。
    直接パスへ書くと、書き込み途中の不完全なファイルを読んでしまい
    "image file is truncated"エラーになる実例を確認したため、一時ファイルに
    書いてからos.replace()でアトミックに差し替える(読み手は常に完成済みの
    旧ファイルか新ファイルのどちらかしか見えない)。"""
    os.makedirs(MOOD_IMAGE_DIR, exist_ok=True)
    image = Image.open(file_obj).convert("RGBA")
    final_path = mood_image_path(mood)
    tmp_path = final_path + ".tmp"
    image.save(tmp_path, format="PNG")
    os.replace(tmp_path, final_path)

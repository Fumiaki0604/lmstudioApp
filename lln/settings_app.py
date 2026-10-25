"""Rilinの設定画面(音声オンオフ・話者・ペルソナ)。

使い方:
    streamlit run settings_app.py
"""
import base64

import streamlit as st

import config
import pronunciation_dict
from converse import user_impression
from mood import mood_label, mood_value
from mood_images import MOOD_LABELS, get_mood_image, save_mood_image

st.set_page_config(page_title="Rilin設定")

MOOD_LABEL_JA = {"good": "良い", "bad": "悪い", "neutral": "普通"}


def _image_to_base64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


st.title("Rilin 設定")


@st.cache_data(ttl=300)
def _cached_user_impression() -> str:
    return user_impression()


status_header_col, mood_image_col = st.columns([2, 1])
with status_header_col:
    st.subheader("状態(編集不可)")
with mood_image_col:
    current_mood_image = get_mood_image(mood_label())
    if current_mood_image:
        # st.image()はカラム内で常に左詰めになり右寄せのオプションが無いため、
        # 右寄せ用のCSSを当てたHTML<img>で埋め込む。また、Streamlitの画像要素は
        # デフォルトでmax-width:100%(コンテナ幅に収める)が効いており、widthを
        # 指定してもカラム幅までしか拡大されなかったため、max-width:noneで
        # 明示的に解除する(カラム幅を超えた分は右側の余白へはみ出す)。
        st.markdown(
            f'<div style="display:flex;justify-content:flex-end;">'
            f'<img src="data:image/png;base64,{_image_to_base64(current_mood_image)}" '
            f'width="300" style="border-radius:8px;max-width:none;">'
            f"</div>",
            unsafe_allow_html=True,
        )

mood_text_col, _ = st.columns([1, 2])
with mood_text_col:
    st.text_input("今日の機嫌", value=f"{MOOD_LABEL_JA[mood_label()]}(値: {mood_value():.2f})", disabled=True)
st.text_area("ユーザーへの印象", value=_cached_user_impression(), height=100, disabled=True)

cfg = config.load()

listening_enabled = st.toggle("周囲の音声を拾う(マイク認識)", value=cfg["listening_enabled"])
st.caption(
    "オフにすると、マイクで音声認識するプロセス自体を止める(ミュートではなく認識の停止)。"
    "この端末を自宅外に持ち出す時など、周囲の会話を拾いたくない場合はオフにする。"
)

voice_enabled = st.toggle("音声を有効にする", value=cfg["voice_enabled"])

# 話者・スタイルの手動選択は非表示にした。generate()が会話内容に応じて
# スタイルを動的に選ぶようになったため、UI上の固定選択は不要との判断。
# 値自体はconfigにそのまま残し、フォールバック用として使い続ける。
speaker = cfg["speaker"]
style = cfg["style"]

persona_prompt = st.text_area("ペルソナ(システムプロンプト)", value=cfg["persona_prompt"], height=150)

default_weather_location = st.text_input(
    "天気のデフォルト地点(場所を言わずに聞かれた時に使う)", value=cfg["default_weather_location"]
)

user_profile = st.text_area(
    "ユーザープロフィール(名前・属性など、毎回そのままRilinに伝える固定情報)",
    value=cfg["user_profile"],
    height=100,
)

st.subheader("見た目(機嫌別の画像)")
st.caption("機嫌(良い/普通/悪い)ごとに画像をアップロードできる。アップロードすると即座に反映される(保存ボタン不要)。")
mood_image_cols = st.columns(3)
for col, mood in zip(mood_image_cols, MOOD_LABELS):
    with col:
        st.write(MOOD_LABEL_JA[mood])
        existing = get_mood_image(mood)
        if existing:
            st.image(existing, width=120)
        # 保存後にuploaderのkeyを変えて状態をリセットする。keyを固定したまま
        # st.rerun()すると、file_uploaderの選択状態が引き継がれて毎回の
        # 再実行でuploadedがNoneに戻らず、保存→rerunを無限に繰り返す
        # (その結果、同じファイルへの書き込みが競合し続ける)実例を確認した。
        gen_key = f"mood_image_{mood}_gen"
        generation = st.session_state.get(gen_key, 0)
        uploaded = st.file_uploader(
            "画像を選択",
            type=["png", "jpg", "jpeg"],
            key=f"mood_image_{mood}_{generation}",
            label_visibility="collapsed",
        )
        if uploaded is not None:
            save_mood_image(mood, uploaded)
            st.session_state[gen_key] = generation + 1
            st.success("更新しました")
            st.rerun()

st.subheader("発音辞書(読み間違いの修正)")
st.caption(
    "COEIROINKが読み間違える単語を登録する。「表記」はそのまま書く言葉(漢字でもよい)、"
    "「読み」は実際に読ませたいカタカナ。アクセント核は分からなければ0(平板)でよい。"
)
pronunciation_entries = st.data_editor(
    cfg["pronunciation_entries"],
    num_rows="dynamic",
    column_config={
        "word": st.column_config.TextColumn("表記"),
        "yomi": st.column_config.TextColumn("読み(カタカナ)"),
        "accent": st.column_config.NumberColumn("アクセント核(0=平板)", min_value=0, step=1),
    },
    key="pronunciation_entries_editor",
)

if st.button("保存", type="primary"):
    config.save(
        {
            "listening_enabled": listening_enabled,
            "voice_enabled": voice_enabled,
            "speaker": speaker,
            "style": style,
            "persona_prompt": persona_prompt,
            "default_weather_location": default_weather_location,
            "user_profile": user_profile,
            "pronunciation_entries": pronunciation_entries,
        }
    )
    try:
        pronunciation_dict.apply(pronunciation_entries)
    except Exception as e:
        st.warning(f"発音辞書の保存は成功しましたが、COEIROINKへの反映に失敗しました: {e}")
    st.success("保存しました。assistant.py実行中は次のターンから反映されます。")

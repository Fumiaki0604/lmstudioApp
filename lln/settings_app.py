"""Rilinの設定画面(音声オンオフ・話者・ペルソナ)。

使い方:
    streamlit run settings_app.py
"""
import streamlit as st

import config
from converse import user_impression
from mood import mood_label, mood_value
from speak import SPEAKERS

st.set_page_config(page_title="Rilin設定")
st.title("Rilin 設定")

MOOD_LABEL_JA = {"good": "良い", "bad": "悪い", "neutral": "普通"}


@st.cache_data(ttl=300)
def _cached_user_impression() -> str:
    return user_impression()


st.subheader("状態(編集不可)")
st.text_input("今日の機嫌", value=f"{MOOD_LABEL_JA[mood_label()]}(値: {mood_value():.2f})", disabled=True)
st.text_area("ユーザーへの印象", value=_cached_user_impression(), height=100, disabled=True)

cfg = config.load()

voice_enabled = st.toggle("音声を有効にする", value=cfg["voice_enabled"])

speakers = list(SPEAKERS.keys())
speaker = st.selectbox("話者", speakers, index=speakers.index(cfg["speaker"]))

styles = list(SPEAKERS[speaker]["styles"].keys())
default_style = cfg["style"] if cfg["style"] in styles else styles[0]
style = st.selectbox("スタイル", styles, index=styles.index(default_style))

persona_prompt = st.text_area("ペルソナ(システムプロンプト)", value=cfg["persona_prompt"], height=150)

if st.button("保存", type="primary"):
    config.save(
        {
            "voice_enabled": voice_enabled,
            "speaker": speaker,
            "style": style,
            "persona_prompt": persona_prompt,
        }
    )
    st.success("保存しました。assistant.py実行中は次のターンから反映されます。")

"""Lilinの管理画面。設定とセミナーを、ページ上部のメニューで切り替える別ページにしている。

st.tabsだと、ボタン操作後の再描画(st.rerun)で毎回最初のタブに戻ってしまうため、
ページ単位で分けている。

使い方:
    streamlit run settings_app.py
"""
import streamlit as st

st.set_page_config(page_title="Lilin")

st.navigation(
    [
        st.Page("settings_page.py", title="設定", default=True),
        st.Page("seminar_page.py", title="セミナー"),
    ],
    position="top",
).run()

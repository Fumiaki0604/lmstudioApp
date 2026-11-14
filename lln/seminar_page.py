"""セミナーページ(録音の開始・停止、質疑応答で聞く質問の候補、過去の録音)。settings_app.pyから表示される。"""
import os
from datetime import datetime

import streamlit as st

import seminar

_STATUS_JA = {"recording": "録音中", "processing": "処理中", "done": "完了", "error": "失敗"}

st.title("セミナー")

rec_dir = seminar.active()
if rec_dir:
    meta = seminar.load_meta(rec_dir)
    elapsed = int((datetime.now() - datetime.fromisoformat(meta["started"])).total_seconds())
    st.info(
        f"録音中: {seminar.MODE_LABEL[meta['mode']]} · {meta.get('title') or '(タイトルなし)'} · "
        f"{elapsed // 60}分経過(最長{seminar.MAX_SEC // 3600}時間で自動停止)。録音中のLilinは返事をしない。"
    )
    stop_col, refresh_col = st.columns(2)
    with stop_col:
        if st.button("録音停止", type="primary", use_container_width=True):
            seminar.stop()
            st.rerun()
    with refresh_col:
        if st.button("表示を更新", use_container_width=True):
            st.rerun()

    st.subheader("質疑応答で聞く質問")
    st.caption("ここまでの講演内容から、質疑応答で聞くと良さそうな質問をLLMが考える。1分ほどかかる。何度でも作り直せる。")
    if st.button("質問を考える", use_container_width=True):
        with st.spinner("講演内容を文字起こしして、質問を考えています…"):
            try:
                seminar.suggest_questions(rec_dir)
            except Exception as e:
                st.error(f"質問を作れませんでした: {e}")
    # 新しい候補が上に来るよう、時点ごとの見出しで区切って逆順に並べる
    blocks = [b for b in seminar.load_questions(rec_dir).split("## ") if b.strip()]
    for block in reversed(blocks):
        header, _, body = block.partition("\n")
        st.markdown(f"**{header.strip()}**")
        st.markdown(body)
else:
    st.caption(
        "オフライン=マイクで会場の音声を録る。オンライン=YouTube/Zoom等のPCの音を直接録る"
        "(音量を下げてもイヤホンでも録れる)。停止後に文字起こし・要約してDiscordの#lilin_seminarに送る。"
        "録音中はこのページで、質疑応答で聞く質問の候補を作れる。"
    )
    title = st.text_input("タイトル(任意。空なら内容から自動で付ける)", key="rec_title")
    mic_col, sys_col = st.columns(2)
    for col, mode, label in ((mic_col, "mic", "オフライン録音開始(マイク)"), (sys_col, "system", "オンライン録音開始(PCの音)")):
        with col:
            if st.button(label, use_container_width=True):
                seminar.start(mode, title)
                st.rerun()

st.subheader("最近の録音")
for d, m in seminar.recent(10):
    label = (
        f"{m.get('started', '')[:16].replace('T', ' ')} · {_STATUS_JA.get(m.get('status'), m.get('status'))} · "
        f"{m.get('title') or '(タイトルなし)'}"
    )
    with st.expander(label):
        if m.get("error"):
            st.error(m["error"])
        if m.get("status") not in ("recording", "processing"):
            confirm = st.checkbox("この録音を削除する(音声・要約・感想も消え、元に戻せない)", key=f"del_chk_{d}")
            if confirm and st.button("削除", key=f"del_btn_{d}", type="primary"):
                try:
                    seminar.delete(d)
                    st.rerun()
                except Exception as e:
                    st.error(f"削除できませんでした: {e}")
        summary_path = os.path.join(d, "summary.md")
        if os.path.exists(summary_path):
            with open(summary_path) as f:
                st.markdown(f.read())
        if os.path.exists(summary_path):
            kind_col, btn_col = st.columns([3, 1])
            with kind_col:
                kind = st.selectbox(
                    "感想のタイプ",
                    list(seminar.IMPRESSION_TYPES),
                    format_func=seminar.IMPRESSION_TYPES.get,
                    key=f"imp_kind_{d}",
                    label_visibility="collapsed",
                )
            with btn_col:
                if st.button("感想を書く", key=f"imp_btn_{d}", use_container_width=True):
                    with st.spinner("感想を書いています…"):
                        try:
                            seminar.write_impression(d, kind)
                        except Exception as e:
                            st.error(f"感想を書けませんでした: {e}")
            # 新しい感想が上に来るよう逆順に。コピーしやすいようコードブロックで出す
            for block in reversed([b for b in seminar.load_impressions(d).split("## ") if b.strip()]):
                header, _, body = block.partition("\n")
                st.markdown(f"**感想: {header.strip()}** ({len(body.strip())}字)")
                st.code(body.strip(), language=None, wrap_lines=True)
        questions = seminar.load_questions(d)
        if questions:
            st.markdown("---\n**録音中に作った質問の候補**")
            st.markdown(questions.replace("## ", "#### "))

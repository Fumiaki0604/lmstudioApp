"""会話マップ生成 — pyvis 左→右ツリー可視化。

使い方:
    from mindmap import build_mindmap_html
    html = build_mindmap_html(log_entries, events, session_label="自律会話")
    # → st.components.v1.html(html, height=600, scrolling=True)
"""
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional
import re

_WORD_RE = re.compile(r"[一-鿿ぁ-ゟ]{2,}|[a-zA-Z]{3,}")
_STOP = {
    "する", "なる", "ある", "いる", "こと", "もの", "それ", "これ", "あれ",
    "ので", "から", "けど", "でも", "しか", "ため", "よう", "ない", "でき",
    "思う", "言う", "見る", "来る", "行く", "やる", "みる", "おく", "くれ",
    "いう", "なん", "でし", "まし", "だっ", "てい", "ちゃ", "じゃ", "って",
    "たい", "たら", "なら", "れる", "られ", "せる", "させ", "まで",
}

_PALETTE = [
    "#4e79a7", "#f28e2b", "#e15759", "#76b7b2",
    "#59a14f", "#edc948", "#b07aa1", "#ff9da7",
    "#9c755f", "#bab0ac",
]

_EVENT_COLORS = {
    "planned": "#edc948",
    "maybe_done": "#76b7b2",
    "assumed_done": "#59a14f",
    "decided": "#59a14f",
    "expired": "#888888",
    "needs_resolution": "#f28e2b",
}

_EVENT_STATUS_JA = {
    "planned": "計画中", "maybe_done": "実施?",
    "assumed_done": "完了", "decided": "決定済",
    "expired": "期限切れ", "needs_resolution": "要確認",
}


@dataclass
class TopicSegment:
    index: int
    label: str
    start_idx: int
    end_idx: int
    speakers: list = field(default_factory=list)
    topic_terms: list = field(default_factory=list)


def _words(text: str) -> set:
    return set(_WORD_RE.findall(text)) - _STOP


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def extract_topic_segments(log_entries: list,
                            window: int = 5,
                            threshold: float = 0.15,
                            max_turns: int = 120) -> list:
    """Jaccard 類似度で隣接ウィンドウを比較し、話題転換点を検出する。"""
    entries = log_entries[-max_turns:]
    n = len(entries)
    if n < 2:
        return []

    word_sets = [_words(e.get("text", "")) for e in entries]

    def win_words(start: int) -> set:
        result: set = set()
        for i in range(start, min(start + window, n)):
            result |= word_sets[i]
        return result

    boundaries = [0]
    step = max(1, window // 2)
    prev_w = win_words(0)
    for i in range(step, n, step):
        curr_w = win_words(i)
        if _jaccard(prev_w, curr_w) < threshold and i not in boundaries:
            boundaries.append(i)
        prev_w = curr_w
    boundaries.append(n)
    boundaries = sorted(set(boundaries))

    segments = []
    for bi in range(len(boundaries) - 1):
        s, e = boundaries[bi], boundaries[bi + 1]

        freq: Counter = Counter()
        for idx in range(s, e):
            for w in word_sets[idx]:
                freq[w] += 1
        topic_terms = [w for w, _ in freq.most_common(5)]
        label = "・".join(topic_terms[:3]) if topic_terms else f"話題{bi + 1}"

        spk_cnt: Counter = Counter(entry.get("name", "") for entry in entries[s:e])
        speakers = [sp for sp, c in spk_cnt.most_common() if c >= 2 and sp]

        segments.append(TopicSegment(
            index=bi,
            label=label,
            start_idx=s,
            end_idx=e - 1,
            speakers=speakers,
            topic_terms=topic_terms,
        ))

    return segments


def _find_segment_for_event(ev, segments: list, log_entries: list, offset: int) -> int:
    """EventMemory の first_seen_at タイムスタンプからセグメントを逆引き。"""
    first_seen = (getattr(ev, "first_seen_at", "") or "")[:16]
    if not first_seen:
        return len(segments) - 1
    tail = log_entries[-len(segments[0].end_idx + 1 + offset):]  # approximation
    for raw_i, entry in enumerate(log_entries):
        if entry.get("timestamp", "").startswith(first_seen):
            adj_i = raw_i - offset
            for si, seg in enumerate(segments):
                if seg.start_idx <= adj_i <= seg.end_idx:
                    return si
            break
    return len(segments) - 1


def build_mindmap_html(log_entries: list,
                        events: list = None,
                        session_label: str = "自律会話") -> str:
    """会話ログからマインドマップ HTML を生成。pyvis 未インストール時は空文字列。"""
    try:
        from pyvis.network import Network
    except ImportError:
        return ""

    max_turns = 120
    offset = max(0, len(log_entries) - max_turns)
    segments = extract_topic_segments(log_entries, max_turns=max_turns)
    if not segments:
        return ""

    net = Network(height="580px", width="100%", directed=True,
                  bgcolor="#0e1117", font_color="white")
    net.set_options("""{
      "layout": {
        "hierarchical": {
          "enabled": true,
          "direction": "LR",
          "sortMethod": "directed",
          "levelSeparation": 260,
          "nodeSpacing": 55,
          "treeSpacing": 90,
          "blockShifting": true,
          "edgeMinimization": true,
          "parentCentralization": true
        }
      },
      "physics": { "enabled": false },
      "edges": {
        "smooth": {
          "type": "cubicBezier",
          "forceDirection": "horizontal",
          "roundness": 0.4
        },
        "arrows": { "to": { "enabled": false } }
      },
      "nodes": {
        "font": { "face": "Meiryo, Hiragino Sans, sans-serif" },
        "borderWidth": 0
      },
      "interaction": {
        "hover": true,
        "navigationButtons": true,
        "zoomView": true
      }
    }""")

    # ルートノード
    net.add_node("root", label=session_label, shape="box",
                 color={"background": "#ffffff", "border": "#aaaaaa"},
                 font={"size": 15, "color": "#000000"},
                 level=0)

    events = events or []

    # 話題セグメントと話者ノード
    for seg in segments:
        color = _PALETTE[seg.index % len(_PALETTE)]
        seg_id = f"seg_{seg.index}"
        turn_range = f"ターン {offset + seg.start_idx}〜{offset + seg.end_idx}"

        net.add_node(seg_id, label=seg.label, shape="ellipse",
                     color={"background": color, "border": color},
                     font={"size": 13, "color": "#ffffff"},
                     level=1,
                     title=turn_range)
        net.add_edge("root", seg_id, color={"color": color, "opacity": 0.9}, width=2)

        for spk in seg.speakers:
            spk_id = f"spk_{seg.index}_{spk}"
            net.add_node(spk_id, label=spk, shape="dot",
                         color={"background": color, "border": color},
                         size=12,
                         font={"size": 11, "color": "#ffffff"},
                         level=2)
            net.add_edge(seg_id, spk_id,
                         color={"color": color, "opacity": 0.55}, width=1)

    # イベントノード（セグメントに紐付け）
    for ev in events:
        si = _find_segment_for_event(ev, segments, log_entries, offset)
        ev_color = _EVENT_COLORS.get(ev.status, "#888888")
        status_ja = _EVENT_STATUS_JA.get(ev.status, ev.status)
        ev_label = f"📌 {ev.title[:12]}"
        ev_id = f"ev_{ev.id}"
        net.add_node(ev_id, label=ev_label, shape="box",
                     color={"background": ev_color, "border": ev_color},
                     font={"size": 10, "color": "#000000"},
                     level=2,
                     title=f"{ev.title}（{status_ja}）")
        net.add_edge(f"seg_{si}", ev_id,
                     color={"color": ev_color, "opacity": 0.75},
                     width=1, dashes=True)

    try:
        return net.generate_html()
    except Exception:
        return ""

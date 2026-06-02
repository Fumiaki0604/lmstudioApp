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


def build_mindmap_image(log_entries: list,
                         events: list = None,
                         session_label: str = "自律会話") -> Optional[bytes]:
    """matplotlib で左→右ツリーの PNG を生成。失敗時は None。"""
    try:
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        from matplotlib.patches import FancyBboxPatch
        import matplotlib.font_manager as fm
        # macOS 日本語フォントを優先設定
        _jp_fonts = ["Hiragino Sans", "Hiragino Kaku Gothic Pro", "Apple SD Gothic Neo", "BIZ UDGothic"]
        _available = {f.name for f in fm.fontManager.ttflist}
        for _f in _jp_fonts:
            if _f in _available:
                matplotlib.rcParams["font.family"] = _f
                break

        max_turns = 120
        offset = max(0, len(log_entries) - max_turns)
        segments = extract_topic_segments(log_entries, max_turns=max_turns)
        if not segments:
            return None
        events = events or []

        # --- ノード・エッジ収集 ---
        nodes: list = []   # (id, label, level, color, shape)
        edges: list = []   # (src_id, dst_id, color)

        ROOT = "root"
        nodes.append((ROOT, session_label, 0, "#ffffff", "box"))

        for seg in segments:
            color = _PALETTE[seg.index % len(_PALETTE)]
            seg_id = f"seg_{seg.index}"
            nodes.append((seg_id, seg.label, 1, color, "ellipse"))
            edges.append((ROOT, seg_id, color))
            for spk in seg.speakers:
                spk_id = f"spk_{seg.index}_{spk}"
                nodes.append((spk_id, spk, 2, color, "dot"))
                edges.append((seg_id, spk_id, color))

        for ev in events:
            si = _find_segment_for_event(ev, segments, log_entries, offset)
            ev_color = _EVENT_COLORS.get(ev.status, "#888888")
            ev_id = f"ev_{ev.id}"
            ev_label = f"📌{ev.title[:10]}"
            nodes.append((ev_id, ev_label, 2, ev_color, "box"))
            edges.append((f"seg_{si}", ev_id, ev_color))

        # --- 座標計算（レベル別に Y を均等配置） ---
        from collections import defaultdict
        level_nodes: dict = defaultdict(list)
        for nid, label, level, color, shape in nodes:
            level_nodes[level].append(nid)

        # 各ノードの親を記録
        parent_map: dict = {}
        for src, dst, _ in edges:
            parent_map[dst] = src

        # レベル2ノードを親ごとにグループ化してY座標を決定
        def assign_y(node_id: str, child_ids: list, y_start: float, y_step: float) -> dict:
            coords = {}
            for i, nid in enumerate(child_ids):
                coords[nid] = y_start + i * y_step
            return coords

        # L1ノード（セグメント）のY座標
        l1_ids = level_nodes[1]
        n_l1 = len(l1_ids)
        y_coords: dict = {}
        x_coords: dict = {}

        # L2の合計数からL1のY間隔を計算
        l2_per_l1 = {}
        for nid in level_nodes[2]:
            par = parent_map.get(nid)
            if par:
                l2_per_l1.setdefault(par, []).append(nid)

        # L1ごとのY中心を決定
        y_cursor = 0.0
        l1_y_center = {}
        for seg_id in l1_ids:
            children = l2_per_l1.get(seg_id, [])
            span = max(1, len(children))
            l1_y_center[seg_id] = y_cursor + (span - 1) / 2.0
            # L2ノードのY
            for i, c in enumerate(children):
                y_coords[c] = y_cursor + i
                x_coords[c] = 2.0
            y_cursor += span + 0.4

        for seg_id in l1_ids:
            y_coords[seg_id] = l1_y_center[seg_id]
            x_coords[seg_id] = 1.0

        y_coords[ROOT] = (y_cursor - 0.4) / 2.0
        x_coords[ROOT] = 0.0

        # --- 描画 ---
        total_h = max(4, y_cursor + 1)
        fig_w = 14
        fig_h = max(5, total_h * 0.7)
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))
        fig.patch.set_facecolor("#0e1117")
        ax.set_facecolor("#0e1117")
        ax.axis("off")

        # エッジ
        for src, dst, ecolor in edges:
            sx, sy = x_coords.get(src, 0), y_coords.get(src, 0)
            dx, dy = x_coords.get(dst, 0), y_coords.get(dst, 0)
            ax.plot([sx, (sx + dx) / 2, dx], [sy, sy, dy],
                    color=ecolor, linewidth=1.5, alpha=0.7,
                    solid_capstyle="round")

        # ノード
        for nid, label, level, color, shape in nodes:
            x = x_coords.get(nid, 0)
            y = y_coords.get(nid, 0)
            fs = 10 if level == 2 else (13 if level == 1 else 14)
            fc = "#000000" if color == "#ffffff" else "#ffffff"
            bx = ax.text(x, y, label,
                         ha="left" if level > 0 else "center",
                         va="center",
                         fontsize=fs,
                         color=fc,
                         bbox=dict(
                             boxstyle="round,pad=0.3",
                             facecolor=color,
                             edgecolor="none",
                             alpha=0.9,
                         ))

        # x軸の余白
        ax.set_xlim(-0.5, 2.8)
        ax.set_ylim(-0.8, y_cursor + 0.3)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception:
        return None


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
                  bgcolor="#0e1117", font_color="white",
                  cdn_resources="in_line")
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

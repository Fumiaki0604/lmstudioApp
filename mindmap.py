"""会話マップ生成 — 時間ベースセグメント + 3段階 matplotlib ツリー。

使い方:
    from mindmap import build_mindmap_image
    png = build_mindmap_image(log_entries, events, session_label="自律会話")
    # → st.image(png, use_container_width=True)
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
    "ます", "です", "ない", "から", "まで", "より", "でも", "けど",
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

_STATUS_MARK = {
    "assumed_done": "✓", "decided": "✓",
    "maybe_done": "?", "planned": "…",
    "needs_resolution": "!", "expired": "×",
}


_PARTICLES = {"の", "に", "を", "は", "が", "で", "と", "も", "か", "へ", "や",
              "ら", "り", "て", "し", "な", "ね", "よ", "わ", "さ", "ぞ"}


def _words(text: str) -> list:
    """漢字を含む5文字以内の語 or 英単語のみ抽出。助詞始まり・終わり・平仮名のみを除外。"""
    result = []
    for w in _WORD_RE.findall(text):
        if w in _STOP or len(w) < 2:
            continue
        if re.search(r'[一-鿿]', w) and len(w) <= 5:
            # 先頭・末尾が助詞なら除外
            if w[0] in _PARTICLES or w[-1] in _PARTICLES:
                continue
            result.append(w)
        elif re.match(r'[a-zA-Z]{3,}$', w):
            result.append(w)
    return result


# ─── TimeSegment dataclass ────────────────────────────────────────────────────

@dataclass
class TimeSegment:
    index: int
    label: str           # 話題キーワード
    time_start: str      # 開始時刻 ("22:30")
    time_end: str        # 終了時刻
    start_idx: int       # log_entries 上の絶対インデックス
    end_idx: int
    speakers: list       # [(name, count), ...]
    events: list = field(default_factory=list)  # EventMemory


# ─── セグメント抽出（時間ベース固定分割）────────────────────────────────────

def extract_time_segments(log_entries: list,
                           segment_size: int = 20,
                           max_turns: int = 120) -> list:
    """N ターンごとに固定分割し、各セグメントに識別キーワードを付ける。"""
    entries = log_entries[-max_turns:]
    n = len(entries)
    offset = max(0, len(log_entries) - max_turns)
    if n == 0:
        return []

    # 全ターンの単語頻度（TF-IDF 計算用）
    all_words: Counter = Counter()
    for e in entries:
        for w in _words(e.get("text", "")):
            all_words[w] += 1

    segments = []
    for si, start in enumerate(range(0, n, segment_size)):
        chunk = entries[start:start + segment_size]
        if not chunk:
            break

        # 時刻
        t_start = chunk[0].get("time", "")
        t_end = chunk[-1].get("time", "")

        # 話者カウント
        spk_cnt: Counter = Counter(e.get("name", "") for e in chunk if e.get("name"))
        speakers = [(name, cnt) for name, cnt in spk_cnt.most_common(3) if name]

        # 識別キーワード: このセグメントに多く出てグローバルでは普通の語
        chunk_words: Counter = Counter()
        for e in chunk:
            for w in _words(e.get("text", "")):
                chunk_words[w] += 1

        scores = {
            w: freq / max(1.0, all_words[w] ** 0.4)
            for w, freq in chunk_words.items() if freq >= 2
        }
        top_words = sorted(scores, key=lambda w: -scores[w])[:3]
        # 識別語が少ない場合は頻出語で補完
        if len(top_words) < 2:
            freq_fallback = [w for w, _ in chunk_words.most_common(5)
                             if w not in top_words]
            top_words = (top_words + freq_fallback)[:3]
        label = "・".join(top_words) if top_words else f"話題{si + 1}"

        segments.append(TimeSegment(
            index=si,
            label=label,
            time_start=t_start,
            time_end=t_end,
            start_idx=offset + start,
            end_idx=offset + start + len(chunk) - 1,
            speakers=speakers,
        ))

    return segments


def _assign_events(segments: list, events: list, log_entries: list, offset: int) -> None:
    """EventMemory を first_seen_at のタイムスタンプで対応セグメントに振り分ける。"""
    for ev in events:
        first_seen = (getattr(ev, "first_seen_at", "") or "")[:16]
        target_si = len(segments) - 1
        if first_seen:
            for raw_i, entry in enumerate(log_entries):
                if entry.get("timestamp", "").startswith(first_seen):
                    for si, seg in enumerate(segments):
                        if seg.start_idx <= raw_i <= seg.end_idx:
                            target_si = si
                            break
                    break
        segments[target_si].events.append(ev)


# ─── 描画ユーティリティ ───────────────────────────────────────────────────────

def _bezier(ax, x1, y1, x2, y2, color, lw=1.8, alpha=0.75):
    """S字ベジェ曲線でノードを接続（MindWeaver スタイル）。"""
    from matplotlib.path import Path
    import matplotlib.patches as mpatches
    cx = (x1 + x2) / 2
    verts = [(x1, y1), (cx, y1), (cx, y2), (x2, y2)]
    codes = [Path.MOVETO, Path.CURVE4, Path.CURVE4, Path.CURVE4]
    path = Path(verts, codes)
    patch = mpatches.PathPatch(
        path, facecolor="none", edgecolor=color,
        linewidth=lw, alpha=alpha, capstyle="round", zorder=2,
    )
    ax.add_patch(patch)


def _dot(ax, x, y, color, r=0.055, zorder=4):
    import matplotlib.patches as mpatches
    ax.add_patch(mpatches.Circle((x, y), r, color=color, zorder=zorder))


# ─── メイン描画関数 ──────────────────────────────────────────────────────────

def build_mindmap_image(log_entries: list,
                         events: list = None,
                         session_label: str = "自律会話") -> Optional[bytes]:
    """matplotlib で MindWeaver 風の左→右ツリー PNG を生成。失敗時は None。"""
    try:
        import io
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm

        # 日本語フォント
        _jp = ["Hiragino Sans", "Hiragino Kaku Gothic Pro", "Apple SD Gothic Neo",
               "BIZ UDGothic", "Noto Sans CJK JP"]
        _avail = {f.name for f in fm.fontManager.ttflist}
        for _f in _jp:
            if _f in _avail:
                matplotlib.rcParams["font.family"] = _f
                break

        # --- セグメント抽出 ---
        max_turns = 120
        offset = max(0, len(log_entries) - max_turns)
        segments = extract_time_segments(log_entries, max_turns=max_turns)
        if not segments:
            return None

        # --- イベント振り分け（expired 除外・上位8件） ---
        _ev_pri = {"decided": 0, "assumed_done": 1, "needs_resolution": 2,
                   "maybe_done": 3, "planned": 4}
        ev_list = sorted(
            [e for e in (events or []) if e.status != "expired"],
            key=lambda e: _ev_pri.get(e.status, 9)
        )[:8]
        _assign_events(segments, ev_list, log_entries, offset)

        # --- Y レイアウト計算 ---
        Y_ITEM = 0.75   # L2 ノード間の間隔
        Y_GAP  = 0.55   # L1 グループ間の余白

        y_cur = 0.0
        for seg in segments:
            n = max(1, len(seg.events))
            seg._y_start = y_cur
            seg._y_center = y_cur + (n - 1) * Y_ITEM / 2
            y_cur += n * Y_ITEM + Y_GAP

        total_h = y_cur - Y_GAP
        root_y = total_h / 2

        # X 座標
        X_ROOT, X_L1, X_L2 = 0.0, 3.0, 6.2

        # --- Figure 初期化 ---
        fig_w = 13
        fig_h = max(4.5, total_h * 1.05 + 1.0)
        fig, ax = plt.subplots(figsize=(fig_w, fig_h))
        fig.patch.set_facecolor("#0e1117")
        ax.set_facecolor("#0e1117")
        ax.axis("off")
        ax.set_xlim(-0.8, X_L2 + 2.8)
        ax.set_ylim(-0.8, total_h + 0.8)

        # --- ルートノード ---
        ax.text(
            X_ROOT, root_y, session_label,
            ha="center", va="center", fontsize=13, color="#000000",
            fontweight="bold", zorder=5,
            bbox=dict(boxstyle="round,pad=0.4", facecolor="#ffffff",
                      edgecolor="#cccccc", linewidth=1.2),
        )

        # --- L1 セグメント + L2 イベント ---
        for seg in segments:
            color = _PALETTE[seg.index % len(_PALETTE)]
            y1 = seg._y_center

            # Root → L1
            _bezier(ax, X_ROOT, root_y, X_L1, y1, color, lw=2.0)
            _dot(ax, X_L1, y1, color, r=0.07)

            # L1 ラベル（時刻 + トピック）
            time_label = f"{seg.time_start}" if seg.time_start else ""
            full_label = f"{time_label}\n{seg.label}" if time_label else seg.label
            ax.text(
                X_L1 + 0.18, y1, full_label,
                ha="left", va="center", fontsize=10.5, color="#ffffff",
                fontweight="normal", zorder=5, linespacing=1.3,
            )

            # L2 イベントノード
            for ei, ev in enumerate(seg.events):
                y2 = seg._y_start + ei * Y_ITEM
                ev_color = _EVENT_COLORS.get(ev.status, "#888888")
                mark = _STATUS_MARK.get(ev.status, "")

                _bezier(ax, X_L1, y1, X_L2, y2, ev_color, lw=1.4, alpha=0.65)
                _dot(ax, X_L2, y2, ev_color, r=0.05)

                ev_label = f"{mark} {ev.title[:14]}"
                ax.text(
                    X_L2 + 0.12, y2, ev_label,
                    ha="left", va="center", fontsize=9.5, color="#ffffff",
                    zorder=5,
                )

            # イベントなし時: 主要話者を L1 ラベルの下に小さく表示
            if not seg.events and seg.speakers:
                top_spk = " / ".join(n for n, _ in seg.speakers[:3])
                ax.text(
                    X_L1 + 0.18, y1 - 0.28, top_spk,
                    ha="left", va="top", fontsize=8.5, color="#666666",
                    style="italic", zorder=5,
                )

        # --- 出力 ---
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    except Exception:
        return None


# ─── 後方互換（旧 pyvis HTML 版、未使用） ────────────────────────────────────

def build_mindmap_html(*args, **kwargs) -> str:
    return ""

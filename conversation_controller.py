"""App-side conversation controller: ConversationState + MovePlanner + OutputGuardrail."""
import random
import re
from dataclasses import dataclass, field

CONTROL_RATE = 0.75

HOOK_PATTERNS = [
    "？", "?", "どう", "誰", "何", "どこ", "いつ",
    "かも", "かな", "迷う", "気になる", "見てみたい",
    "だろうね", "したいな",
]

# ─── 一人称分類 ───────────────────────────────────────────────────────────────
# 強くチェックする独自一人称（被りが少なく、汚染が致命的なもの）
DISTINCTIVE_FPS = {"ボク", "僕", "俺", "ワタクシ", "わし", "小生", "うち", "あっし"}
# 共通になりやすい一人称（Guardrail で弱くチェック）
COMMON_FPS = {"私", "わたし", "ワタシ", "あたし", "自分"}

# ─── move_type ────────────────────────────────────────────────────────────────
MOVE_TYPES = [
    "agree_and_extend",
    "ask",
    "specific_question",
    "bring_new_detail",
    "react_to_detail",
    "assign_role",
    "tease",
    "introduce_conflict",
    "short_reaction",
    "summarize_and_close",
    "bridge",
    "soft_punchline",
    "observe",
    "reframe",
    "shift",
    "care_but_move",
    "imagine_risk",
    "calm_reframe",
    "invite_other",
    "process_risk",
    "reflect_on_event",
    "mark_event_expired",
    "next_day_followup",
]

MOVE_INSTRUCTIONS = {
    "agree_and_extend": "直前の発言に同意しつつ、自分だけが知っている具体的な情報や体験を1つ加える。",
    "ask": (
        "直前の話題に関連した質問を1つ投げかける。"
        "「〜はどう思う？」だけで終わらせず、相手が選べる具体的な選択肢を1つ以上含める。"
        "例：「味見係と混ぜる係どっちがいい？」「先に買い出し行く？それとも家にあるもので試す？」"
    ),
    "specific_question": (
        "直前の話題について、答えやすい具体的な質問を1つ。"
        "「どう思う？」系は使わず、選択肢か対象を明確にする。"
    ),
    "bring_new_detail": (
        "現在の話題に新しい具体的な要素を1つ持ち込む。"
        "必ず 物・行動・役割 のどれかにすること。抽象語だけで終わらない。"
        "例（物）: お茶、毛布　例（行動）: 味見する、順番を決める　例（役割）: 見張り係、寝落ち監視係"
    ),
    "react_to_detail": (
        "直前の具体案への感想ではなく反応を返す。"
        "問題点・笑い・役割のどれかを1つ出す。評価で終わらない。"
        "例：「それ、夜中にフライパン出した時点で片付け係が泣くやつでは？」"
    ),
    "assign_role": "今の話題で「誰が何をやるか」を1つ決める。役割・順番・担当を具体的に提案する。",
    "tease": "軽いツッコミや冗談で場の雰囲気を少し動かす。1〜2文。",
    "introduce_conflict": "「でも〜じゃない？」と軽く別視点を出す。言い争いではなく好奇心ベース。小さなズレを作る。",
    "short_reaction": "長文は不要。感情的な短いリアクション1文だけ。「えっ、それマジ？」「それは辛い」など。",
    "summarize_and_close": "今の話題を1文でまとめ、「で、次は〜」と話を次に渡す。",
    "bridge": "今の話題から連想できる別の話題への橋渡しをする。唐突な転換は避ける。",
    "soft_punchline": "今の話題を軽くオチにする。完全に終わらせず、次に続けられる余白を残す。",
    "observe": "会話全体を少し引いて見た観察者的な一言を添える。断定せず余白を残す。",
    "reframe": "今の話題を別の切り口から見直す。否定ではなく、視点をずらす。",
    "shift": "今の話題をひと言でまとめてから、自分の関心事で別の話題に自然につなげる。",
    "care_but_move": (
        "相手を気遣いつつ、会話を止めずに次の行動・役割へ移す。"
        "「〜は大丈夫？じゃあ次に〜しよう」の形式。気遣いで終わらない。"
    ),
    "imagine_risk": (
        "今の案・行動について、起きそうな小さなリスクや失敗パターンを1つ出す。"
        "深刻にせず、軽いトーンで。"
    ),
    "calm_reframe": "今の話題を落ち着いたトーンでまとめ直す。判断を押しつけず、別の見方を添える。",
    "invite_other": "他のメンバーに話を振る。具体的な役割や行動を提案しながら巻き込む。",
    "process_risk": "怖い・面倒・難しいという懸念を、やめる理由ではなく役割分担・罰ゲーム・ゲーム化に変えてください。「〜係」「〜したら負け」の形で具体的に処理する。",
    "reflect_on_event": "過去に出た企画・計画について、実施後の感想や「結局どうなったか」を1文で話す。準備話には戻らない。既成事実として扱う。",
    "mark_event_expired": "数日前・数時間前の企画を「流れた話」として軽く閉じ、次の話題への橋渡しをする。過去形で扱い、今も準備中にしない。",
    "next_day_followup": "前回の企画の結果を受けて、次に何をするかへ進める。結果はポジティブでも中立でもよい。準備に戻らない。",
}

# ─── キャラ別 move_type バイアス ──────────────────────────────────────────────
CHAR_MOVE_BIAS: dict = {
    "東北ずん子":  ["invite_other", "assign_role", "care_but_move", "process_risk"],
    "東北きりたん": ["tease", "introduce_conflict", "short_reaction"],
    "四国めたん":  ["summarize_and_close", "ask", "calm_reframe", "reflect_on_event"],
    "中国うさぎ":  ["observe", "bridge", "soft_punchline", "imagine_risk"],
    "Noah":       ["observe", "bridge", "soft_punchline", "reflect_on_event", "mark_event_expired"],
    "Hermes":     ["reframe", "specific_question", "introduce_conflict"],
    "雨晴はう":   ["shift", "bring_new_detail", "invite_other"],
    "春日部つむぎ": ["bring_new_detail", "tease", "short_reaction", "process_risk"],
    "WhiteCUL":   ["reframe", "ask", "soft_punchline"],
}

# ─── care_loop 定数 ───────────────────────────────────────────────────────────
CARE_LOOP_TERMS = [
    "休み", "休もう", "お休み", "無理", "疲れ",
    "気をつけ", "元気", "大事", "待ってる", "心配", "懸念",
    "頑張", "ゆっくり", "体調", "眠れ",
]

# ─── OutputGuardrail 定数 ────────────────────────────────────────────────────
WEAK_ENDINGS = [
    "大事", "大切", "楽しもう", "楽しみ", "リラックス", "最高", "いい考え",
    "いいですね", "ですね", "だよね", "いいね", "ですよね", "だと思う",
]
_CONCRETE_ACTION_RE = re.compile(
    r"[一-鿿]{2,}(?:する|した|して|したい|しよう|係|役|担当|買|作|食|飲|持|使)"
    r"|(?:係|役|担当|買い出し|味見|混ぜる|片付け|並べ)"
)

_WORD_RE = re.compile(r"[一-鿿]{2,}|[ぁ-ゟ]{3,}|[ァ-ヿ]{2,}|[a-zA-Z]{3,}")

_STOP = {
    "います", "ます", "です", "から", "けど", "ので", "して", "ある", "いる", "なる",
    "こと", "それ", "これ", "あの", "その", "みんな", "一緒", "思う", "感じ",
    "する", "なる", "てる", "いう", "よう", "ため", "とき", "さん", "ちゃん",
    "だよ", "だね", "かな", "よね", "ので", "けど", "から",
}

_AGREEMENT_MARKERS = [
    "そうだね", "だよね", "いいね", "確かに", "そうですね", "だよな", "ですね",
    "ほんと", "たしかに", "うんうん",
]


def _extract_content_words(text: str) -> set:
    return set(_WORD_RE.findall(text)) - _STOP


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass
class TopicMemory:
    label: str
    terms: list
    consumed_as: str
    allowed_reuse_as: list
    cooldown_turns: int = 4


@dataclass
class ConversationState:
    current_scene: str = ""
    current_topic_terms: list = field(default_factory=list)
    topic_age: int = 0
    repetition_score: float = 0.0
    low_progress_streak: int = 0
    open_hooks: list = field(default_factory=list)
    should_shift_topic: bool = False
    do_not_repeat: list = field(default_factory=list)
    do_not_repeat_intents: list = field(default_factory=list)  # Phase 3以降
    last_move_types: list = field(default_factory=list)
    topic_stage: str = "active"  # active / aging / closing
    care_loop_score: float = 0.0
    should_close_topic: bool = False
    recent_full_texts: list = field(default_factory=list)  # 直近3件の全文（Jaccard用）
    consumed_topics: list = field(default_factory=list)   # 消化済み話題（TopicMemory）
    active_goal: str = ""                                  # 現在の会話目標（EventMemoryから注入）


def update_conv_state(log_entries: list) -> ConversationState:
    recent = log_entries[-5:] if log_entries else []

    # --- topic terms ---
    freq: dict = {}
    for e in recent:
        words = _WORD_RE.findall(e["text"])
        seen = set()
        for w in words:
            if w not in _STOP and w not in seen:
                freq[w] = freq.get(w, 0) + 1
                seen.add(w)
    topic_terms = [w for w, c in sorted(freq.items(), key=lambda x: -x[1]) if c >= 2][:5]

    if topic_terms:
        current_scene = f"{'・'.join(topic_terms[:3])}の話をしている"
    elif recent:
        current_scene = recent[-1]["text"][:20] + "…"
    else:
        current_scene = "会話開始"

    # --- topic_age ---
    topic_age = 0
    for e in reversed(recent):
        if topic_terms and any(t in e["text"] for t in topic_terms):
            topic_age += 1
        else:
            break

    # --- repetition_score ---
    recent3_text = " ".join(e["text"] for e in recent[-3:])
    words_in_recent = set(_WORD_RE.findall(recent3_text))
    term_overlap = len(words_in_recent & set(topic_terms)) / max(len(topic_terms), 1) if topic_terms else 0.0

    phrase_overlap = 0.0
    if len(recent) >= 2:
        t1 = set(_WORD_RE.findall(recent[-1]["text"]))
        t2 = set(_WORD_RE.findall(recent[-2]["text"]))
        if t1 and t2:
            phrase_overlap = len(t1 & t2) / max(len(t1), 1)

    repetition_score = min(1.0, term_overlap * 0.6 + phrase_overlap * 0.4)

    # --- low_progress_streak ---
    low_streak = 0
    for e in reversed(recent):
        if len(e["text"]) < 15 or any(m in e["text"] for m in _AGREEMENT_MARKERS):
            low_streak += 1
        else:
            break

    should_shift = (topic_age >= 4 and repetition_score >= 0.5) or low_streak >= 3

    if topic_age <= 2:
        stage = "active"
    elif topic_age <= 4:
        stage = "aging"
    elif should_shift:
        stage = "closing"
    else:
        stage = "active"

    last_text = recent[-1]["text"] if recent else ""
    open_hooks = [p for p in HOOK_PATTERNS if p in last_text]
    do_not_repeat = [e["text"][:50] for e in recent[-2:]]

    # --- care_loop_score ---
    care_hits = sum(1 for e in recent if any(t in e["text"] for t in CARE_LOOP_TERMS))
    care_loop_score = care_hits / max(len(recent), 1)
    should_close = topic_age >= 5 or care_loop_score >= 0.6

    # --- 直近3件の全文（Jaccard用） ---
    recent_full_texts = [e["text"] for e in recent[-3:]]

    # --- consumed_topics: 一度話題になったが直近3件では出ていない語群 ---
    consumed_topics: list = []
    if len(log_entries) >= 8:
        old_entries = log_entries[-10:-3]
        recent3_entries = log_entries[-3:]
        old_freq: dict = {}
        for e in old_entries:
            for w in _WORD_RE.findall(e.get("text", "")):
                if w not in _STOP:
                    old_freq[w] = old_freq.get(w, 0) + 1
        recent3_words = {
            w for e in recent3_entries
            for w in _WORD_RE.findall(e.get("text", "")) if w not in _STOP
        }
        consumed_words = [w for w, cnt in old_freq.items() if cnt >= 2 and w not in recent3_words][:8]
        if consumed_words:
            label_words = sorted(consumed_words, key=lambda w: old_freq.get(w, 0), reverse=True)[:3]
            consumed_topics = [TopicMemory(
                label="・".join(label_words),
                terms=consumed_words,
                consumed_as="話し合った話題",
                allowed_reuse_as=["役割分担", "失敗", "オチ", "次の行動"],
            )]

    return ConversationState(
        current_scene=current_scene,
        current_topic_terms=topic_terms,
        topic_age=topic_age,
        repetition_score=repetition_score,
        low_progress_streak=low_streak,
        open_hooks=open_hooks,
        should_shift_topic=should_shift,
        do_not_repeat=do_not_repeat,
        topic_stage=stage,
        care_loop_score=care_loop_score,
        should_close_topic=should_close,
        recent_full_texts=recent_full_texts,
        consumed_topics=consumed_topics,
    )


class MovePlanner:
    def pick_move(self, state: ConversationState, last_move_types: list = None,
                  char_name: str = "") -> str:
        last_moves = (last_move_types or [])[-3:]

        # ステージ別ベース pool
        if state.care_loop_score >= 0.6:
            pool = ["tease", "short_reaction", "introduce_conflict", "summarize_and_close", "bridge"]
        elif state.should_close_topic:
            pool = ["summarize_and_close", "bridge", "shift", "assign_role"]
        elif state.topic_stage == "closing":
            pool = ["shift", "bridge", "summarize_and_close", "invite_other", "short_reaction"]
        elif state.topic_stage == "aging":
            pool = [
                "react_to_detail", "ask", "tease",
                "introduce_conflict", "introduce_conflict",
                "assign_role", "observe",
            ]
        else:  # active
            pool = ["agree_and_extend", "ask", "bring_new_detail", "tease", "short_reaction", "observe"]

        # キャラ別バイアスを pool の前に挿入（優先度を上げる）
        char_bias = CHAR_MOVE_BIAS.get(char_name, [])
        if char_bias:
            pool = char_bias + pool

        # open_hook 優先（care_loop/close 中は除外）
        if state.open_hooks and state.care_loop_score < 0.6 and not state.should_close_topic:
            pool = ["specific_question", "react_to_detail", "tease"] + pool

        # 同 move_type の連続抑制（完全禁止でなく候補外し）
        if len(last_moves) >= 2 and last_moves[-1] == last_moves[-2]:
            filtered = [m for m in pool if m != last_moves[-1]]
            pool = filtered if filtered else pool

        # 直近2件の move_type は除外
        candidates = [m for m in pool if m not in last_moves[-2:]] or pool
        return random.choice(candidates)


def build_move_instruction(move: str, state: ConversationState) -> str:
    base = MOVE_INSTRUCTIONS.get(move, "")
    if not base:
        return ""

    lines = [f"\n【今回の発言スタイル: {move}】{base}"]

    if state.do_not_repeat:
        phrases = "・".join(f"「{p[:20]}」" for p in state.do_not_repeat[-2:])
        lines.append(f"- 直近の発言（{phrases}）と同じ内容・言い回しは避ける。")

    if state.topic_stage in ("aging", "closing") and state.current_topic_terms:
        terms = "・".join(state.current_topic_terms[:3])
        lines.append(f"- 「{terms}」を文頭・主語に使わない。話を一歩前へ進める。")

    if state.consumed_topics:
        for ct in state.consumed_topics:
            t_str = "・".join(ct.terms[:4])
            r_str = "・".join(ct.allowed_reuse_as[:3])
            lines.append(f"- 「{t_str}」はすでに話した話題です。同じ話題として繰り返さず、{r_str}として扱ってください。")

    if state.active_goal:
        lines.append(f"- 現在の会話目標: 「{state.active_goal}」を進める。準備の話に戻らない。")

    return "\n".join(lines)


# ─── Phase 2 / 2.5 / 2.6: OutputGuardrail ───────────────────────────────────

def check_output(reply: str, state: ConversationState,
                 own_fp: str = "", other_fps: dict = None,
                 move_type: str = "") -> tuple:
    """(is_ng, ng_score, reasons, shared_words) を返す。"""
    ng_score = 0.0
    reasons = []
    shared_words: list = []
    other_fps = other_fps or {}

    reply_words = _extract_content_words(reply)

    # ── 1. 一人称汚染チェック（最優先）────────────────────────────────────
    for name, fp in other_fps.items():
        if not fp or fp == own_fp:
            continue
        if fp in DISTINCTIVE_FPS and fp in reply:
            # 引用（「ボク」）は弱め、それ以外は即NG水準
            if f"「{fp}」" in reply or f"『{fp}』" in reply:
                ng_score += 0.2
                reasons.append(f"他キャラ（{name}）の一人称「{fp}」を引用（弱めペナルティ）")
            else:
                ng_score += 0.6
                reasons.append(f"他キャラ（{name}）の一人称「{fp}」を使用")
        elif fp in COMMON_FPS and fp != own_fp and fp in reply and own_fp not in reply:
            # 自分の一人称が出ていないのに他の一人称が出ている場合のみ弱くチェック
            ng_score += 0.2
            reasons.append(f"一人称が不明確（「{fp}」が出ているが「{own_fp}」が出ていない）")

    # ── 1b. own_fp 強制チェック ──────────────────────────────────────────
    if own_fp:
        ALL_FPS = DISTINCTIVE_FPS | COMMON_FPS | {own_fp}
        used_fps = {fp for fp in ALL_FPS if fp in reply}
        if used_fps and own_fp not in used_fps:
            ng_score += 0.4
            reasons.append(f"一人称が「{own_fp}」でなく「{'・'.join(sorted(used_fps))}」になっている")

    # ── 2. 文頭繰り返し語 ────────────────────────────────────────────────
    for term in state.current_topic_terms:
        if re.match(rf"^{re.escape(term)}[はがもでの、]?", reply):
            ng_score += 0.4
            reasons.append(f"文頭に繰り返し語「{term}」")
            break

    # ── 3. 弱い結び + 具体要素なし ─────────────────────────────────────
    has_weak = any(reply.endswith(w) or reply.endswith(w + "。") or reply.endswith(w + "！")
                   for w in WEAK_ENDINGS)
    has_concrete = bool(_CONCRETE_ACTION_RE.search(reply))
    if has_weak and not has_concrete:
        ng_score += 0.3
        reasons.append("抽象的な結びのみ（具体的な物・行動・役割なし）")

    # ── 4. do_not_repeat との語重複 ──────────────────────────────────────
    for phrase in state.do_not_repeat:
        phrase_words = _extract_content_words(phrase)
        overlap = reply_words & phrase_words
        if len(overlap) >= 2:
            ng_score += 0.4
            reasons.append(f"直近発言と語重複（{'・'.join(list(overlap)[:2])}）")
            break

    # ── 5. care_loop_intent ───────────────────────────────────────────────
    if state.care_loop_score >= 0.5 and not has_concrete:
        care_hits = sum(1 for t in CARE_LOOP_TERMS if t in reply)
        if care_hits >= 1:
            ng_score += 0.4
            reasons.append("気遣いループ継続（休み・無理・元気だけで終わっている）")

    # ── 6. 汎用質問（「どう思う？」系） ──────────────────────────────────
    if re.search(r"どう思[うう][？?]|どうでしょう[？?]|どう感じ", reply):
        if not re.search(r"どっち|どちら|どれ|するのと|にする[？?]", reply):
            ng_score += 0.3
            reasons.append("汎用質問（「どう思う？」系・選択肢なし）")

    # ── 7. RecentSimilarityGuard（Phase 2.6） ────────────────────────────
    # short_reaction は類似度チェックを緩める
    if move_type != "short_reaction" and state.recent_full_texts and reply_words:
        best_shared: set = set()
        best_jaccard = 0.0
        for text in state.recent_full_texts:
            other_words = _extract_content_words(text)
            j = _jaccard(reply_words, other_words)
            s = reply_words & other_words
            if j > best_jaccard or len(s) > len(best_shared):
                best_jaccard = j
                best_shared = s

        shared_words = list(best_shared)
        shared_count = len(best_shared)
        # 共有語 >= 5 はクローン（単独NG水準）、3-4 語 + Jaccard は組み合わせで判定
        if shared_count >= 5:
            ng_score += 0.6
            reasons.append(f"直前発話とほぼ同内容（共有語: {'・'.join(shared_words[:4])}）")
        elif shared_count >= 4:
            ng_score += 0.6
            reasons.append(f"直前発話と内容が近すぎる（共有語: {'・'.join(shared_words[:4])}）")
        elif best_jaccard >= 0.35 and shared_count >= 3:
            ng_score += 0.5
            reasons.append(f"直前発話と内容が近すぎる（共有語: {'・'.join(shared_words[:4])}）")

    is_ng = ng_score >= 0.6
    return is_ng, round(ng_score, 2), reasons, shared_words


def build_retry_instruction(reasons: list, state: ConversationState,
                            shared_words: list = None) -> str:
    lines = ["前の発言を書き直してください。発言テキストのみ出力し、指示内容を発言に含めないこと。"]

    # 一人称崩れを最優先で表示
    fp_issues = [r for r in reasons if "一人称" in r]
    other_issues = [r for r in reasons if "一人称" not in r]
    for r in fp_issues + other_issues:
        lines.append(f"- {r}")

    # 発話類似・なぞりへの対処は「役割変更」を指示
    if any("直前発話" in r or "内容が近" in r or "語重複" in r for r in reasons):
        lines.append("- 言い換えではなく、会話の機能を変えてください：")
        lines.append("  問題点を出す / 役割を決める / 別キャラに振る / 小さく茶化す / 次の行動を決める")
        if shared_words:
            lines.append(f"- 特に「{'・'.join(shared_words[:4])}」を中心に使わないこと")

    if state.current_topic_terms:
        terms = "・".join(state.current_topic_terms[:3])
        lines.append(f"- 「{terms}」を文頭・主語に置かない（文中での自然な使用はOK）")

    lines.append("- 「どう思う？」だけで終わる質問は禁止。選択肢か具体対象を含める")
    lines.append("- 「楽しもう」「大事」「リラックス」などの抽象的な結びを避け、物・行動・役割を含める")
    lines.append("1〜2文で書き直してください。")
    return "\n".join(lines)


# ─── Phase 2.7: FinalReplySanitizer ──────────────────────────────────────────

_META_INLINE_RE = re.compile(
    r"[（(]注[:：][^）)]{0,150}[）)]?"
    r"|[（(]再生成[）)]"
)
_META_LINE_KEYWORDS = [
    "元の指示", "指示内容", "書き直し", "避けることだったが",
    "システムプロンプト", "文頭・主語", "文頭や主語",
]
_CHAR_FALLBACKS: dict = {
    "東北ずん子":   "……ちょっと、考えてみます。",
    "東北きりたん": "……まぁ、そういうこともあるよね。",
    "四国めたん":   "……少し整理させてください。",
    "Noah":        "……そうですね。",
    "Hermes":      "……なるほど。",
    "ずんだもん":   "……うーん、なのだ。",
    "雨晴はう":    "……ボクもそう思う。",
    "春日部つむぎ": "……あーし、考え中っす。",
    "WhiteCUL":    "……ちょっと待ってね。",
    "中国うさぎ":   "……うさぎ、考えてる。",
    "東北イタコ":   "……少し間を置かせてもらいます。",
}
_DEFAULT_FALLBACK = "……少し考え直しますね。"


def get_char_fallback(name: str) -> str:
    return _CHAR_FALLBACKS.get(name, _DEFAULT_FALLBACK)


def _looks_truncated(text: str) -> bool:
    if not text or len(text) < 5:
        return False
    if text[-1] in "。！？!?…」』～":
        return False
    if len(text) <= 20:
        return False
    last_end = max(text.rfind("。"), text.rfind("！"), text.rfind("？"),
                   text.rfind("!"), text.rfind("?"))
    if last_end >= 0 and len(text) - last_end > 10:
        return True
    if last_end < 0:
        return True
    return False


def _truncate_at_last_sentence(text: str) -> str:
    last = max(text.rfind("。"), text.rfind("！"), text.rfind("？"),
               text.rfind("!"), text.rfind("?"))
    return text[:last + 1] if last >= 0 else ""


def sanitize_reply(reply: str, speaker_name: str = "", own_fp: str = "",
                   prev_speaker_text: str = "", prev_speaker_fp: str = "") -> tuple:
    """(is_ng, ng_score, reasons) — 会話状態非依存の出力品質チェック。"""
    ng_score = 0.0
    reasons = []

    # ── 1. メタ文検出（インライン括弧 + 行単位）────────────────────────────
    if _META_INLINE_RE.search(reply):
        ng_score += 0.8
        reasons.append("メタ注釈/再生成タグ混入")
    else:
        for line in reply.split("\n"):
            if any(kw in line for kw in _META_LINE_KEYWORDS):
                ng_score += 0.8
                reasons.append(f"メタ行混入: {line[:40]}")
                break

    # ── 2. 自分宛てメンション────────────────────────────────────────────────
    if speaker_name and f"@{speaker_name}" in reply:
        ng_score += 0.8
        reasons.append("自分宛てメンション")

    # ── 3. 途中切れ ────────────────────────────────────────────────────────
    if _looks_truncated(reply):
        ng_score += 0.8
        reasons.append("文が途中で切れている")

    # ── 4. 直前話者からの動的汚染検出────────────────────────────────────────
    if prev_speaker_text and prev_speaker_fp and prev_speaker_fp != own_fp:
        if prev_speaker_fp in reply:
            ng_score += 0.7
            reasons.append(f"直前話者の一人称「{prev_speaker_fp}」を使用")
        else:
            prev_hira = set(re.findall(r"[ぁ-ゟ]{3,4}", prev_speaker_text)) - _STOP
            hits = [t for t in prev_hira if t in reply]
            if len(hits) >= 2:
                ng_score += 0.4
                reasons.append(f"直前話者の特徴的語句を流用（{'・'.join(hits[:3])}）")

    is_ng = ng_score >= 0.6
    return is_ng, round(ng_score, 2), reasons

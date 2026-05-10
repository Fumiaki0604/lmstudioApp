"""App-side conversation controller: ConversationState + MovePlanner."""
import random
import re
from dataclasses import dataclass, field

CONTROL_RATE = 0.75

HOOK_PATTERNS = [
    "？", "?", "どう", "誰", "何", "どこ", "いつ",
    "かも", "かな", "迷う", "気になる", "見てみたい",
    "だろうね", "したいな",
]

MOVE_TYPES = [
    "agree_and_extend",
    "ask",
    "bring_new_detail",
    "tease",
    "introduce_conflict",
    "shift",
    "summarize_and_close",
    "bridge",
    "invite_other",
    "short_reaction",
    "observe",
]

MOVE_INSTRUCTIONS = {
    "agree_and_extend": "直前の発言に同意しつつ、自分だけが知っている具体的な情報や体験を1つ加える。",
    "ask": "直前の話題に関連した答えやすい質問を1つ投げかける。",
    "bring_new_detail": "現在の話題に新しい具体的な要素（人・物・行動）を1つ持ち込む。",
    "tease": "軽いツッコミや冗談で場の雰囲気を少し動かす。1〜2文。",
    "introduce_conflict": "「でも〜じゃない？」と軽く別視点を出す。言い争いではなく好奇心ベース。",
    "shift": "今の話題をひと言でまとめてから、自分の関心事で別の話題に自然につなげる。",
    "summarize_and_close": "今の話題を1文でまとめ、「で、次は〜」と話を次に渡す。",
    "bridge": "今の話題から連想できる別の話題への橋渡しをする。唐突な転換は避ける。",
    "invite_other": "他のメンバーに「〜はどう思う？」と話を振る。",
    "short_reaction": "長文は不要。「えっ、それマジ？」など感情的な短いリアクション1文だけ。",
    "observe": "会話全体を少し引いて見た観察者的な一言を添える。断定せず余白を残す。",
}

_WORD_RE = re.compile(r"[一-鿿]{2,}|[ぁ-ゟ]{2,}|[ァ-ヿ]{2,}|[a-zA-Z]{3,}")

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
    do_not_repeat_intents: list = field(default_factory=list)  # Phase 2
    last_move_types: list = field(default_factory=list)
    topic_stage: str = "active"  # active / aging / closing


def update_conv_state(log_entries: list) -> ConversationState:
    recent = log_entries[-5:] if log_entries else []

    # --- topic terms: 2文字以上・2件以上の登場語 ---
    freq: dict = {}
    for e in recent:
        words = _WORD_RE.findall(e["text"])
        seen = set()
        for w in words:
            if w not in _STOP and w not in seen:
                freq[w] = freq.get(w, 0) + 1
                seen.add(w)
    topic_terms = [w for w, c in sorted(freq.items(), key=lambda x: -x[1]) if c >= 2][:5]

    # --- current_scene: 状況文テンプレ ---
    if topic_terms:
        current_scene = f"{'・'.join(topic_terms[:3])}の話をしている"
    elif recent:
        # 最新発言の先頭20文字を状況として使う
        current_scene = recent[-1]["text"][:20] + "…"
    else:
        current_scene = "会話開始"

    # --- topic_age: current_topic_terms が連続して登場するターン数 ---
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

    # フレーズ重複: 直近2件で同じ語尾・同じ言い回しが出ているか
    phrase_overlap = 0.0
    if len(recent) >= 2:
        t1 = set(_WORD_RE.findall(recent[-1]["text"]))
        t2 = set(_WORD_RE.findall(recent[-2]["text"]))
        if t1 and t2:
            phrase_overlap = len(t1 & t2) / max(len(t1), 1)

    repetition_score = min(1.0, term_overlap * 0.6 + phrase_overlap * 0.4)

    # --- low_progress_streak: 短文 or 同意だけのターン数（連続） ---
    low_streak = 0
    for e in reversed(recent):
        if len(e["text"]) < 15 or any(m in e["text"] for m in _AGREEMENT_MARKERS):
            low_streak += 1
        else:
            break

    # --- shift 判定 ---
    should_shift = (topic_age >= 4 and repetition_score >= 0.5) or low_streak >= 3

    # --- topic_stage ---
    if topic_age <= 2:
        stage = "active"
    elif topic_age <= 4:
        stage = "aging"
    elif should_shift:
        stage = "closing"
    else:
        stage = "active"

    # --- open_hooks: 直近1件から ---
    last_text = recent[-1]["text"] if recent else ""
    open_hooks = [p for p in HOOK_PATTERNS if p in last_text]

    # --- do_not_repeat: 直近2件の先頭50文字 ---
    do_not_repeat = [e["text"][:50] for e in recent[-2:]]

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
    )


class MovePlanner:
    def pick_move(self, state: ConversationState, last_move_types: list = None) -> str:
        last_moves = (last_move_types or [])[-3:]

        if state.topic_stage == "closing":
            pool = ["shift", "bridge", "summarize_and_close", "invite_other", "short_reaction"]
        elif state.topic_stage == "aging":
            pool = ["ask", "bring_new_detail", "tease", "introduce_conflict", "invite_other", "observe"]
        else:  # active
            pool = ["agree_and_extend", "ask", "bring_new_detail", "tease", "short_reaction", "observe"]

        # open_hook があれば応答系を優先
        if state.open_hooks:
            pool = ["ask", "bring_new_detail", "tease"] + pool

        candidates = [m for m in pool if m not in last_moves] or pool
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
        lines.append(f"- 「{terms}」を繰り返さず、話を一歩前へ進める。")

    return "\n".join(lines)

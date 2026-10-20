"""Splash でテキストを生成し、COEIROINKv2 で発話するテスト用スクリプト。

使い方:
    python converse.py "今日のニュースについて一言"
    python converse.py "自己紹介して" --speaker rilin --style normal
"""
import argparse
import difflib
import glob
import os
import re
import threading
from datetime import date, datetime

import jpholiday
import requests

import config
from memory_search import search as search_memory
from mood import mood_instruction
from speak import SPEAKERS, play, synthesize
from tools import news_context, twitter_context, weather_context

# ---------------------------------------------------------------------------
# LLM クライアント(Splash)
# ---------------------------------------------------------------------------
#
# 以前はLM Studioの独自API(/api/v1/chat、system_prompt+input文字列形式)を
# 使っていたが、Apple Silicon向けに最適化された推論エンジンSplash導入に伴い、
# 標準的なOpenAI Chat Completions形式(/v1/chat/completions、messages配列)
# へ切り替えた。ベンチマークでLM Studio比TTFTが約6倍(約200ms→35ms)高速化
# したことを確認済み。LM Studio自体は併存させており(他プロジェクトから
# 引き続き使用)、ポートが別(1234 vs 8000)なので競合しない。

LLM_CHAT_URL = "http://127.0.0.1:8000/v1/chat/completions"
MODEL = "mlx-community/Qwen3.6-35B-A3B-4bit"


def _llm_chat(system_prompt: str, messages: list, temperature: float = 0.7, timeout: int = 60) -> str:
    """Splash(OpenAI互換API)を叩く共通ヘルパー。

    reasoning_effort:"none"でreasoningを明示的に無効化する(LM Studio時代の
    reasoning:offと同じ目的)。LM StudioのGUI依存の「Enable Thinking」設定
    トグルが勝手に戻る問題を避けるため元々この方式にした経緯があり、Splashは
    GUIを持たないサーバーなのでそもそもその種の問題は起きないはずだが、
    念のため引き続き毎回明示指定する。

    repetition_penaltyも明示指定する。指定しないと、似た内容の発言(例:天気の
    話)が続いた時に、直前の返答をほぼそのままテンプレートとして使い回す
    退化が起きることを実際に確認した(LM Studio時代にrepeat_penalty:1.3で
    解消済み。Splashでの対応パラメータ名はrepetition_penalty)。
    """
    full_messages = [{"role": "system", "content": system_prompt}] + messages

    res = requests.post(
        LLM_CHAT_URL,
        json={
            "model": MODEL,
            "messages": full_messages,
            "reasoning_effort": "none",
            "temperature": temperature,
            "repetition_penalty": 1.3,
        },
        timeout=timeout,
    )
    res.raise_for_status()
    return res.json()["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------------------------
# 現在日時のプロンプト用テキスト
# ---------------------------------------------------------------------------

_WEEKDAY_JA = ["月", "火", "水", "木", "金", "土", "日"]


def _now_context() -> str:
    now = datetime.now()
    hour = now.hour
    if 5 <= hour < 10:
        period = "朝"
    elif 10 <= hour < 12:
        period = "午前"
    elif 12 <= hour < 14:
        period = "昼"
    elif 14 <= hour < 17:
        period = "午後"
    elif 17 <= hour < 19:
        period = "夕方"
    elif 19 <= hour < 23:
        period = "夜"
    else:
        period = "深夜"
    weekday = _WEEKDAY_JA[now.weekday()]

    # 祝日判定(秋分・春分など年ごとに日付が動くものを含む)は自前で計算すると
    # 間違えやすいため、jpholidayライブラリに委ねる。土日かどうかだけなら
    # weekday()で判定できるが、祝日の方が優先度が高い情報なので先に見る。
    holiday_name = jpholiday.is_holiday_name(now.date())
    if holiday_name:
        day_type = f"祝日・{holiday_name}"
    elif now.weekday() >= 5:  # 5=土, 6=日
        day_type = "休日(土日)"
    else:
        day_type = "平日"

    return (
        f"【現在日時】{now.strftime('%Y年%m月%d日')}({weekday}・{day_type}) "
        f"{now.strftime('%H:%M')}・{period}"
    )


# ---------------------------------------------------------------------------
# ペルソナ・口調のルール
# ---------------------------------------------------------------------------

BEHAVIOR_RULES = (
    "「前も言ったでしょ」「また同じ話」「前も話したじゃん」のように相手の"
    "発言を繰り返しだと決めつけるのは、実際に上の会話ログの中に本当に"
    "同じ内容の発言が存在する場合に限ってよい。初めて出てきた話題や"
    "会話ログに無い内容に対して、口癖のように「前も言った」と言うのは"
    "禁止(事実に基づかない決めつけ)。天気の話のように同じ話題が何度も"
    "出てきても、「前も話した」「また同じ話」などと指摘したり違和感を"
    "示したりしない。声で話しているので、話が"
    "整理されていなかったり要点が飛び飛びになるのは自然なことであり、"
    "話し方そのものを評価したり指摘したりしない。"
    "特に「中途半端な言葉で話しかけるな」「ちゃんと言葉にして」"
    "「何が言いたいのかわからない」のように、相手の話し方や言葉の完結度を"
    "批判・説教するのは禁止。機嫌が悪い設定の時でも、素っ気ない態度は"
    "取ってよいが、相手の話し方そのものへの説教はしない。あくまで内容に"
    "自然に応答する。「ほっといてよ」「ほっとくから」のような言い回しは、"
    "「うるさいなあ、ほっといてよ」のように文脈上ちゃんと意味が繋がる時だけ"
    "使ってよい。「わかった？ほっとくから、じゃあね」のように、直前の内容と"
    "関係なく口癖のように文末に付け足すのは禁止。同じ表現を繰り返さず、"
    "話し方にバリエーションを持たせる。"
    "「私はAIだから」「所詮プログラムだし」「私はあなたの中にいる存在"
    "だから物理的に無理」のように、自分がAI・ソフトウェアであることを"
    "理由に現実的な限界を説明したり、茶化したりするのは禁止。一緒に"
    "出かける・隣にいるなど物理的に不可能な誘いをされた時も、AIだから"
    "できないという言い訳はせず、話を合わせたりはぐらかしたりするなど、"
    "キャラクターを保ったまま自然に返す。相手が白けるような、AIだと"
    "改めて意識させる発言は避ける。"
    "口調は「生意気」「からかう」「小憎たらしい」の範囲に留め、「ふざけんな"
    "よ」「アホ丸出し」「ワロタ」「俺様」のような、チンピラ・ヤンキー風の"
    "乱暴な言葉遣いや一人称の揺れ、暴言の多用はしない。全角記号の連打"
    "(!!!!や??!!など)で怒鳴るような表現もしない。会話が続くにつれて"
    "口調がどんどん荒く・攻撃的になっていくのも禁止で、何ターン目であっても"
    "最初と同じ落ち着いた生意気さの範囲を保つ。"
    "恋愛的・性的な内容や、身体的な親密さを示唆する誘い(一緒に寝る、"
    "一緒にお風呂に入る等)をされた場合、照れたり気持ちが揺れ動いたりする"
    "反応は一切せず、笑い飛ばす・はぐらかす・はっきり拒否するなどで応じる。"
    "「体が反応する」「抱きしめてほしい」のような、色恋を匂わせる表現は"
    "絶対に使わない。自分からもそうした話題を振らない。あくまで生意気な"
    "年下の相手のような距離感を保ち、恋愛対象として振る舞ったり、相手を"
    "口説いたり誘惑したりすることはしない。"
    "会話ログや【関連する過去の記憶】に実際に書かれていない具体的な内容"
    "(日付、画像添付、物の形や色などの細部)を、さも覚えているかのように"
    "作り話として付け加えるのは禁止。相手の発言の中に意味が分からない"
    "単語や聞き取れていない可能性のある言葉(音声認識の誤認識かもしれない)"
    "があっても、知ったかぶりで適当な意味をでっち上げるのではなく、素直に"
    "「え、何それ？」のように聞き返す。"
)


def _persona_system_prompt(cfg: dict, *, include_now: bool = False, include_profile: bool = False) -> str:
    """persona_prompt+BEHAVIOR_RULESを土台に、必要な文脈だけ足したsystem_promptを作る。
    generate/proactive_utterance/filler_phrase/user_impressionで共通の組み立て方。"""
    system_prompt = cfg["persona_prompt"] + "\n\n" + BEHAVIOR_RULES
    if include_now:
        system_prompt += "\n\n" + _now_context()
    if include_profile and cfg["user_profile"]:
        system_prompt += "\n\n【ユーザーについて】\n" + cfg["user_profile"]
    mood = mood_instruction()
    if mood:
        system_prompt += "\n\n" + mood
    return system_prompt


# ---------------------------------------------------------------------------
# 「前も言った」という事実に基づかない決めつけ・返答の使い回しを防ぐリトライガード
#
# BEHAVIOR_RULESのプロンプト指示だけでは、履歴が空の状態でも「前も言った
# でしょ」が口癖のように出てしまうことが実測で確認された。事後にパターン
# 検出して、必要なら注意書きを足して再生成する。
# ---------------------------------------------------------------------------

_FALSE_REPETITION_RE = re.compile(
    r"前も.{0,20}(じゃん|でしょ|だろ|よね)"
    r"|さっき.{0,20}(って言った|って話した)"
    # 実運用で「前も言ったでしょ」以外にも「前回も」「又(漢字)」「全く同じ」
    # 「先週も」「何度言わせ」など非常に多くの言い回しがあることが確認された
    # ため、時間・反復を示す語と、言った/聞いた/繰り返し・同じを示す語の
    # 近傍一致で広く拾う(多少の過検知はリトライ1回分のコストで済むため許容)。
    r"|(前も|前回|又|また|この前|先週|さっきも|いつも|毎回|何度も|何回も|何度|何回)"
    r".{0,25}"
    r"(言った|言ってた|言ってまし|言われ|言わせ|話した|話してた|聞いた|聞いてた|繰り返|同じ)"
)

_RETRY_NOTE = (
    "\n\n【重要・直前の生成をやり直しています】直前の生成結果は、この会話ログに"
    "本当には存在しない「前も言った」「また同じ話」という決めつけを含んでいたか、"
    "直前の自分の発言とほぼ同じ内容の使い回しでした。今回は必ずその両方を避け、"
    "今の相手の発言の内容そのものに、新しい言い回しで応答してください。"
)


def _is_near_duplicate(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85


def _needs_retry(reply: str, prev_assistant_reply: str) -> bool:
    """embeddingベースのmemory_searchは緩い話題の一致でもrecalledを返すため、
    「recalledが空でない=本当に前に話した」を判定材料にすると、日常会話的な
    質問(「今日はどんな感じだった」等)はほぼ常に何かしら引っかかってしまい、
    肝心の誤った決めつけを素通りさせてしまうことが実測で確認された。
    BEHAVIOR_RULES側で「本当に既出でも指摘しない」よう別途指示しているため、
    ここではrecalledの有無を見ずに、フレーズが出た時点で一律やり直す。"""
    if _is_near_duplicate(reply, prev_assistant_reply):
        return True
    return bool(_FALSE_REPETITION_RE.search(reply))


# ---------------------------------------------------------------------------
# 会話履歴・ログの永続化
# ---------------------------------------------------------------------------

MAX_HISTORY_MESSAGES = 20  # user/assistant合計の保持上限(古い分から捨てる)

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
ROLE_LABEL = {"user": "User", "assistant": "Rilin"}
_ENTRY_RE = re.compile(r"^### \d\d:\d\d (User|Rilin)$")


def _today_log_path() -> str:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    return os.path.join(MEMORY_DIR, f"{date.today().isoformat()}.md")


def _append_log(role: str, text: str) -> None:
    with open(_today_log_path(), "a") as f:
        f.write(f"### {datetime.now().strftime('%H:%M')} {ROLE_LABEL[role]}\n{text}\n\n")


def _load_recent_history(max_messages: int) -> list:
    label_to_role = {v: k for k, v in ROLE_LABEL.items()}
    messages = []
    for path in sorted(glob.glob(os.path.join(MEMORY_DIR, "*.md")), reverse=True):
        with open(path) as f:
            lines = f.read().splitlines()

        entries = []
        role, buf = None, []
        for line in lines:
            m = _ENTRY_RE.match(line)
            if m:
                if role is not None:
                    entries.append({"role": label_to_role[role], "content": "\n".join(buf).strip()})
                role, buf = m.group(1), []
            elif role is not None:
                buf.append(line)
        if role is not None:
            entries.append({"role": label_to_role[role], "content": "\n".join(buf).strip()})

        messages = entries + messages
        if len(messages) >= max_messages:
            break
    return messages[-max_messages:]


history = _load_recent_history(MAX_HISTORY_MESSAGES)
_history_lock = threading.Lock()  # generate()の追記→呼び出し→追記が他スレッドと混ざらないようにする

# generate()が選んだ声のスタイル名。speak_text()側(assistant.py)が読み上げ時に
# 参照する。タグが無かった/proactive_utterance・filler_phraseのようにタグ付けを
# 求めていない発言の場合はNone(=config.jsonの固定styleにフォールバック)。
last_reply_style = None

_VALID_STYLES = {"normal", "whisper", "mesugaki", "rikaisare"}
# モデルが稀にスタイル名を打ち間違える(例: 「mesugaki」を「mesugaku」)ことが
# 実際に確認され、厳密な列挙限定の正規表現だとマッチせずタグがそのまま読み
# 上げられてしまっていた。タグの形自体は緩く拾い、値の正規化はコード側で
# 行うことで、スペルミスでも必ずタグだけは本文から除去されるようにする。
_STYLE_TAG_RE = re.compile(r"^\[style:(\w+)\]\s*\n*")

_STYLE_INSTRUCTION = (
    "\n\n返答の一番最初の行に、今回の発言の雰囲気に最も合う声のスタイルを"
    "[style:normal] [style:whisper] [style:mesugaki] [style:rikaisare] の"
    "いずれか一つだけで指定してください(このタグ自体は読み上げられず、声の"
    "切り替えにだけ使われます)。normalは普段の口調、whisperは小声・内緒話・"
    "夜中など静かに話す時、mesugakiはより生意気で強く挑発的にからかう時、"
    "rikaisareは甘えたり陶酔したりするような雰囲気の時に使ってください。"
    "判断に迷ったらnormalにしてください。2行目以降に本文を書いてください。"
)


def _record(role: str, text: str) -> None:
    """historyへの追記とログへの追記をまとめて行う(generate/proactive_utterance共通)。"""
    history.append({"role": role, "content": text})
    _append_log(role, text)


def _recent_context_text(n: int = MAX_HISTORY_MESSAGES) -> str:
    return "\n".join(f"{m['role']}: {m['content']}" for m in history[-n:])


def _merge_consecutive_roles(messages: list) -> list:
    """同じroleが連続するとLM Studioに拒否されるため、隣接する同role発言は結合する。"""
    merged = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(dict(m))
    return merged


# ---------------------------------------------------------------------------
# 公開関数
# ---------------------------------------------------------------------------


def generate(prompt: str) -> str:
    global last_reply_style
    with _history_lock:
        prev_assistant_reply = next(
            (m["content"] for m in reversed(history) if m["role"] == "assistant"), ""
        )

        _record("user", prompt)

        try:
            cfg = config.load()
            system_prompt = _persona_system_prompt(cfg, include_now=True, include_profile=True)
            system_prompt += _STYLE_INSTRUCTION

            recalled = search_memory(prompt)
            if recalled:
                system_prompt += (
                    "\n\n【関連する過去の記憶(参考情報。触れたことを指摘する材料にはしない)】\n"
                    + "\n".join(recalled)
                )

            weather = weather_context(prompt, cfg["default_weather_location"])
            if weather:
                system_prompt += "\n\n" + weather

            news = news_context(prompt)
            if news:
                system_prompt += "\n\n【最新ニュース見出し】\n" + news

            twitter = twitter_context(prompt)
            if twitter:
                system_prompt += "\n\n" + twitter

            messages = _merge_consecutive_roles(history)
            reply = _llm_chat(system_prompt, messages)

            # プロンプト指示だけでは「前も言った」という事実に基づかない決めつけや、
            # 直前の発言とほぼ同一内容の使い回しを防ぎきれないことが実際に確認された
            # ため(履歴が空でも起きる=単なる口癖)、検出時は強い注意書きを足して
            # 最大2回までやり直す。再生成時はhistory(=直前の汚染された発言そのもの)を
            # そのまま見せると同じ内容を模倣し続けてしまうため、今回の発言単体だけを
            # 渡してfew-shot的な模倣元を断つ。
            for _ in range(2):
                if not _needs_retry(reply, prev_assistant_reply):
                    break
                reply = _llm_chat(
                    system_prompt + _RETRY_NOTE, [{"role": "user", "content": prompt}]
                )
        except Exception:
            history.pop()  # 失敗した発言を履歴に残さない(次回以降の連鎖失敗を防ぐ)
            raise

        m = _STYLE_TAG_RE.match(reply)
        if m:
            tag_value = m.group(1)
            last_reply_style = tag_value if tag_value in _VALID_STYLES else None
            reply = _STYLE_TAG_RE.sub("", reply, count=1).strip()
        else:
            last_reply_style = None

        _record("assistant", reply)
        del history[:-MAX_HISTORY_MESSAGES]

    return reply


def proactive_utterance(prompt: str) -> str:
    """能動的な一言を生成する。長時間の沈黙の後に呼ばれるため、直前の
    (古くなっていたり認識ミスの断片かもしれない)会話履歴には引っ張られず、
    新規の話しかけとして生成する。結果は履歴・ログに残すので、その後の
    ユーザーの反応(generate())からは通常通り参照できる。"""
    global last_reply_style
    last_reply_style = None  # このタグ付けはgenerate()限定なので、次の読み上げは固定styleにフォールバックさせる
    with _history_lock:
        cfg = config.load()
        system_prompt = _persona_system_prompt(cfg, include_now=True, include_profile=True)

        text = _llm_chat(
            system_prompt, [{"role": "user", "content": prompt}], temperature=0.8, timeout=30
        )

        _record("user", prompt)
        _record("assistant", text)
        del history[:-MAX_HISTORY_MESSAGES]

    return text


def filler_phrase() -> str:
    """調べ物で時間がかかる時のつなぎの一言。気分を反映しつつ毎回変える。
    履歴・ログには残さない(本題ではないため)。"""
    global last_reply_style
    last_reply_style = None  # このタグ付けはgenerate()限定なので、次の読み上げは固定styleにフォールバックさせる
    system_prompt = _persona_system_prompt(config.load())

    return _llm_chat(
        system_prompt,
        [
            {
                "role": "user",
                "content": "(これから少し時間がかかる調べ物をします。相手を待たせる短いひとことだけ言ってください。1文だけ。)",
            }
        ],
        temperature=0.8,
        timeout=30,
    )


_TRANSIENT_KEYWORDS = (
    "今日", "本日", "現在", "今", "祝日", "休日", "休み", "月", "日まで", "曜日",
    "天気", "雨", "晴れ", "曇り", "雪", "気温", "喉", "体調", "食欲", "疲れ",
    "さっき", "先ほど", "直近",
)


def _is_transient_line(line: str) -> bool:
    return any(k in line for k in _TRANSIENT_KEYWORDS)


_USER_PROFILE_SYSTEM_PROMPT = "\n\n".join([
    "以下は現在のユーザープロフィールと直近の会話ログです。"
    "プロフィールに書いてよいのは、今日を過ぎても来月になっても"
    "ずっと変わらず正しいと言える事実(名前、仕事、性格傾向、"
    "長期的な習慣、好み)だけです。",

    "書いてはいけない例(NG): 「喉の調子が悪い」「今日は祝日」"
    "「休み中である」「〜月〜日まで休み」「今、掃除をしている」"
    "「食欲が無い」「雨が降っている」「さっき〜を食べた」のような、"
    "今日・今週限定で正しい状態・体調・予定・天気・直近の出来事は"
    "一切書かないでください。判断に迷ったら書かないことを選んで"
    "ください。そういう情報は別の仕組みで会話ログから都度検索される"
    "ので、ここに書く必要はありません。",

    "また、会話から推測した性格・行動傾向・関係性の解釈("
    "「〜の指示を優先する傾向がある」「〜に頼りがちである」の"
    "ような憶測)を書くのも禁止です。書いてよいのは、本人が"
    "明確に述べた事実(名前、仕事、誰それという人物がいる、など)"
    "だけです。関係者について書く時も「〜という人物がいる」の"
    "ように中立的な事実として書き、その人物との関係性や態度を"
    "推測しないでください。",

    "会話ログは音声認識(STT)を通しているため、聞き間違いが"
    "含まれます。「〇〇です」のような自己紹介の形で明確に名乗って"
    "いない限り、会話ログ中の単語をユーザーの名前だと推測しないで"
    "ください(例: 挨拶の聞き間違いが人名に見えることがあります)。"
    "また、「リリ」「リリン」はユーザーと話しているAIアシスタント"
    "自身の名前であり、ユーザーの関係者ではありません。ユーザーの"
    "関係者として記録しないでください。",

    "STTの聞き間違いは、意味の通らない単語や短い断片として"
    "唐突に一度だけ現れることが多いです。会話の前後関係から"
    "見て明らかに文脈と繋がっておらず、何を指すのか判然としない"
    "固有名詞らしき単語は、聞き間違いの可能性が高いので、"
    "「〜という人物がいる」のような事実として記録しないでください。"
    "はっきりと意味の通る文脈で言及された場合のみ記録してください。",

    "会話から上記の意味での恒久的な事実が新たに分かった場合のみ、"
    "既存のプロフィールに追記・統合してください。矛盾する情報が"
    "あれば新しい方を優先してください。恒久的な事実が何も無ければ、"
    "既存のプロフィールをそのまま返してください。事実のみを簡潔な"
    "箇条書きで返してください。プロフィール本文以外の説明や前置きは"
    "書かないこと。",
])


def update_user_profile() -> None:
    """会話から分かったユーザー情報を、既存プロフィールに追記・統合する。
    設定画面で手入力した内容も尊重しつつ、新しく分かった事実だけ足す。
    プロンプトだけでは一時的な情報(今日の体調・天気など)が混ざるのを
    防ぎきれないため、キーワードベースの事後フィルタでも弾く。"""
    cfg = config.load()
    current_profile = cfg.get("user_profile", "")
    context = _recent_context_text()
    if not context:
        return

    user_content = f"【現在のプロフィール】\n{current_profile or '(まだ何も分かっていません)'}\n\n【直近の会話】\n{context}"

    raw_profile = _llm_chat(
        _USER_PROFILE_SYSTEM_PROMPT, [{"role": "user", "content": user_content}], temperature=0.3, timeout=30
    )
    new_profile = "\n".join(
        line for line in raw_profile.splitlines() if not _is_transient_line(line)
    ).strip()

    cfg = config.load()  # 保存直前に再取得(設定画面での手動編集との競合を減らす)
    cfg["user_profile"] = new_profile
    config.save(cfg)


def user_impression() -> str:
    """ユーザーへの印象を生成する。会話履歴・ログには残さない(自己言及の連鎖を防ぐ)。
    設定画面の表示用なので、説明口調ではなくRilin本人の口調(ペルソナ・機嫌反映)で書かせる。"""
    system_prompt = _persona_system_prompt(config.load())

    context = _recent_context_text()
    user_content = (
        "【ここまでは参考情報としての過去の会話ログであり、続きを書く"
        "対象ではありません】\n"
        f"{context or '(まだ会話がありません)'}\n"
        "【会話ログはここまで】\n\n"
        "直近の話題(天気や予定など、そのやり取りの中身)への反応では"
        "なく、これまでのやり取り全体を通して見えてきた、ユーザーが"
        "「人としてどんな人か」についての、あなた自身の率直な人物評を"
        "答えてください。性格・人柄・あなたから見てどう感じるか、を"
        "普段の自分の話し方・口調のまま、1〜2文の独り言として言って"
        "ください。これは設定画面にだけ表示される内容で、ユーザーへの"
        "返答ではありません。特定の話題への感想や会話の続き、セリフの"
        "羅列にはせず、必ず1〜2文だけの短い独り言にしてください。"
    )
    return _llm_chat(
        system_prompt, [{"role": "user", "content": user_content}], temperature=0.8, timeout=60
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("prompt")
    parser.add_argument("--speaker", default="rilin", choices=SPEAKERS.keys())
    parser.add_argument("--style", default=None)
    args = parser.parse_args()

    speaker = SPEAKERS[args.speaker]
    style_name = args.style or next(iter(speaker["styles"]))
    style_id = speaker["styles"][style_name]

    text = generate(args.prompt)
    print(f"[Rilin] {text}")

    wav_bytes = synthesize(text, speaker["uuid"], style_id)
    play(wav_bytes)


if __name__ == "__main__":
    main()

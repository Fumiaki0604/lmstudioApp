"""LM Studio でテキストを生成し、COEIROINKv2 で発話するテスト用スクリプト。

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

import config
from memory_search import search as search_memory
from mood import mood_instruction
from speak import SPEAKERS, play, synthesize
from tools import news_context, twitter_context, weather_context

LMSTUDIO_CHAT_URL = "http://localhost:1234/api/v1/chat"
MODEL = "qwen/qwen3.6-35b-a3b"


def _lmstudio_chat(system_prompt: str, messages: list, temperature: float = 0.7, timeout: int = 60) -> str:
    """LM Studioの新REST API(/api/v1/chat)を叩く共通ヘルパー。

    GUIの「Enable Thinking」設定はモデル再ロード時に勝手にONへ戻ることが
    何度もあったため(プリセットに保存しても直らない)、reasoning:offを
    毎回明示的に指定することでGUI/プリセットの状態に依存しないようにする。
    このAPIはOpenAI形式のmessages配列を受け付けず、system_prompt文字列＋
    input文字列という形式なので、role付きメッセージはここでフラット化する。

    repeat_penaltyも明示指定する。指定しないと、似た内容の発言(例:天気の
    話)が続いた時に、直前の返答をほぼそのままテンプレートとして使い回す
    退化が起きることを実際に確認した(1.3指定で解消)。
    """
    import requests

    lines = []
    for m in messages:
        label = "User" if m["role"] == "user" else "Assistant"
        lines.append(f"{label}: {m['content']}")
    flattened = "\n".join(lines)

    res = requests.post(
        LMSTUDIO_CHAT_URL,
        json={
            "model": MODEL,
            "system_prompt": system_prompt,
            "input": flattened,
            "reasoning": "off",
            "temperature": temperature,
            "repeat_penalty": 1.3,
        },
        timeout=timeout,
    )
    res.raise_for_status()
    return res.json()["output"][0]["content"].strip()

MAX_HISTORY_MESSAGES = 20  # user/assistant合計の保持上限(古い分から捨てる)

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
    return f"【現在日時】{now.strftime('%Y年%m月%d日')}({weekday}) {now.strftime('%H:%M')}・{period}"


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
)

_FALSE_REPETITION_RE = re.compile(
    r"前も.{0,20}(じゃん|でしょ|だろ|よね)"
    r"|さっき.{0,20}(って言った|って話した)"
    r"|また同じ(質問|話|ネタ|こと)"
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


def _merge_consecutive_roles(messages: list) -> list:
    """同じroleが連続するとLM Studioに拒否されるため、隣接する同role発言は結合する。"""
    merged = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(dict(m))
    return merged


def generate(prompt: str) -> str:
    with _history_lock:
        prev_assistant_reply = next(
            (m["content"] for m in reversed(history) if m["role"] == "assistant"), ""
        )

        history.append({"role": "user", "content": prompt})
        _append_log("user", prompt)

        try:
            cfg = config.load()
            system_prompt = cfg["persona_prompt"] + "\n\n" + BEHAVIOR_RULES + "\n\n" + _now_context()
            if cfg["user_profile"]:
                system_prompt += "\n\n【ユーザーについて】\n" + cfg["user_profile"]
            mood = mood_instruction()
            if mood:
                system_prompt += "\n\n" + mood
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
            reply = _lmstudio_chat(system_prompt, messages)

            # プロンプト指示だけでは「前も言った」という事実に基づかない決めつけや、
            # 直前の発言とほぼ同一内容の使い回しを防ぎきれないことが実際に確認された
            # ため(履歴が空でも起きる=単なる口癖)、検出時は強い注意書きを足して
            # 最大2回までやり直す。再生成時はhistory(=直前の汚染された発言そのもの)を
            # そのまま見せると同じ内容を模倣し続けてしまうため、今回の発言単体だけを
            # 渡してfew-shot的な模倣元を断つ。
            for _ in range(2):
                if not _needs_retry(reply, prev_assistant_reply):
                    break
                reply = _lmstudio_chat(
                    system_prompt + _RETRY_NOTE, [{"role": "user", "content": prompt}]
                )
        except Exception:
            history.pop()  # 失敗した発言を履歴に残さない(次回以降の連鎖失敗を防ぐ)
            raise

        history.append({"role": "assistant", "content": reply})
        _append_log("assistant", reply)
        del history[:-MAX_HISTORY_MESSAGES]

    return reply


def proactive_utterance(prompt: str) -> str:
    """能動的な一言を生成する。長時間の沈黙の後に呼ばれるため、直前の
    (古くなっていたり認識ミスの断片かもしれない)会話履歴には引っ張られず、
    新規の話しかけとして生成する。結果は履歴・ログに残すので、その後の
    ユーザーの反応(generate())からは通常通り参照できる。"""
    with _history_lock:
        cfg = config.load()
        system_prompt = cfg["persona_prompt"] + "\n\n" + BEHAVIOR_RULES + "\n\n" + _now_context()
        if cfg["user_profile"]:
            system_prompt += "\n\n【ユーザーについて】\n" + cfg["user_profile"]
        mood = mood_instruction()
        if mood:
            system_prompt += "\n\n" + mood

        text = _lmstudio_chat(
            system_prompt, [{"role": "user", "content": prompt}], temperature=0.8, timeout=30
        )

        history.append({"role": "user", "content": prompt})
        _append_log("user", prompt)
        history.append({"role": "assistant", "content": text})
        _append_log("assistant", text)
        del history[:-MAX_HISTORY_MESSAGES]

    return text


def filler_phrase() -> str:
    """調べ物で時間がかかる時のつなぎの一言。気分を反映しつつ毎回変える。
    履歴・ログには残さない(本題ではないため)。"""
    system_prompt = config.load()["persona_prompt"] + "\n\n" + BEHAVIOR_RULES
    mood = mood_instruction()
    if mood:
        system_prompt += "\n\n" + mood

    return _lmstudio_chat(
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


def update_user_profile() -> None:
    """会話から分かったユーザー情報を、既存プロフィールに追記・統合する。
    設定画面で手入力した内容も尊重しつつ、新しく分かった事実だけ足す。
    プロンプトだけでは一時的な情報(今日の体調・天気など)が混ざるのを
    防ぎきれないため、キーワードベースの事後フィルタでも弾く。"""
    cfg = config.load()
    current_profile = cfg.get("user_profile", "")
    context = "\n".join(f"{m['role']}: {m['content']}" for m in history[-20:])
    if not context:
        return

    system_prompt = (
        "以下は現在のユーザープロフィールと直近の会話ログです。"
                        "プロフィールに書いてよいのは、今日を過ぎても来月になっても"
                        "ずっと変わらず正しいと言える事実(名前、仕事、性格傾向、"
                        "長期的な習慣、好み)だけです。\n\n"
                        "書いてはいけない例(NG): 「喉の調子が悪い」「今日は祝日」"
                        "「休み中である」「〜月〜日まで休み」「今、掃除をしている」"
                        "「食欲が無い」「雨が降っている」「さっき〜を食べた」のような、"
                        "今日・今週限定で正しい状態・体調・予定・天気・直近の出来事は"
                        "一切書かないでください。判断に迷ったら書かないことを選んで"
                        "ください。そういう情報は別の仕組みで会話ログから都度検索される"
                        "ので、ここに書く必要はありません。\n\n"
                        "また、会話から推測した性格・行動傾向・関係性の解釈("
                        "「〜の指示を優先する傾向がある」「〜に頼りがちである」の"
                        "ような憶測)を書くのも禁止です。書いてよいのは、本人が"
                        "明確に述べた事実(名前、仕事、誰それという人物がいる、など)"
                        "だけです。関係者について書く時も「〜という人物がいる」の"
                        "ように中立的な事実として書き、その人物との関係性や態度を"
                        "推測しないでください。\n\n"
                        "会話ログは音声認識(STT)を通しているため、聞き間違いが"
                        "含まれます。「〇〇です」のような自己紹介の形で明確に名乗って"
                        "いない限り、会話ログ中の単語をユーザーの名前だと推測しないで"
                        "ください(例: 挨拶の聞き間違いが人名に見えることがあります)。"
                        "また、「リリ」「リリン」はユーザーと話しているAIアシスタント"
                        "自身の名前であり、ユーザーの関係者ではありません。ユーザーの"
                        "関係者として記録しないでください。\n\n"
                        "STTの聞き間違いは、意味の通らない単語や短い断片として"
                        "唐突に一度だけ現れることが多いです。会話の前後関係から"
                        "見て明らかに文脈と繋がっておらず、何を指すのか判然としない"
                        "固有名詞らしき単語は、聞き間違いの可能性が高いので、"
                        "「〜という人物がいる」のような事実として記録しないでください。"
                        "はっきりと意味の通る文脈で言及された場合のみ記録してください。"
                        "\n\n"
                        "会話から上記の意味での恒久的な事実が新たに分かった場合のみ、"
                        "既存のプロフィールに追記・統合してください。矛盾する情報が"
                        "あれば新しい方を優先してください。恒久的な事実が何も無ければ、"
                        "既存のプロフィールをそのまま返してください。事実のみを簡潔な"
                        "箇条書きで返してください。プロフィール本文以外の説明や前置きは"
                        "書かないこと。"
    )
    user_content = f"【現在のプロフィール】\n{current_profile or '(まだ何も分かっていません)'}\n\n【直近の会話】\n{context}"

    raw_profile = _lmstudio_chat(
        system_prompt, [{"role": "user", "content": user_content}], temperature=0.3, timeout=30
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
    cfg = config.load()
    system_prompt = cfg["persona_prompt"] + "\n\n" + BEHAVIOR_RULES
    mood = mood_instruction()
    if mood:
        system_prompt += "\n\n" + mood

    context = "\n".join(f"{m['role']}: {m['content']}" for m in history[-20:])
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
    return _lmstudio_chat(
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
    print(f"[LM Studio] {text}")

    wav_bytes = synthesize(text, speaker["uuid"], style_id)
    play(wav_bytes)


if __name__ == "__main__":
    main()

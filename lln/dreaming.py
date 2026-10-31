"""毎晩の記憶整理(Dreaming)。前日の会話ログから事実を抜き出し、facts.mdへ統合する。

AgentMemoryRepoの「Dreaming」(定期的に重複統合・古い情報削除・矛盾解消を行う)
の考え方だけを取り入れたもの。LLMにはfacts.md全体を書き直させず、
追加・更新・削除の操作だけをJSONで返させてコード側で適用する。全文を
書き直させると、無関係な事実まで落としたり言い換えたりする恐れがあるため。
[手動]の項目への操作と期限切れの削除は、LLMに任せずコードで扱う。
変更内容はmemory/_dreaming/log/に日付ごとに残す。

assistant.pyの夜間処理(log_janitorの後)から呼ばれる。手動実行:
    python dreaming.py                   # 未処理の日をまとめて処理
    python dreaming.py --day 2026-10-30  # 指定日だけ処理(状態は進めない)
    python dreaming.py --import-profile  # 旧user_profileを恒久事実として取り込む(初回のみ)
"""
import argparse
import json
import os
import re
from datetime import date, timedelta

import config
import facts
from converse import _llm_chat

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
LOG_DIR = os.path.join(facts.DREAMING_DIR, "log")
STATE_PATH = os.path.join(facts.DREAMING_DIR, "state.json")

MAX_CATCHUP_DAYS = 7  # 何日も止まっていた後に一気に処理しすぎないための上限
DEFAULT_DAYS = {"一時": 3, "話題": 7}
MAX_DAYS = 30

_SYSTEM_PROMPT = "\n\n".join([
    "あなたは、AIアシスタント「リリン(リリ)」の記憶を整理する係です。"
    "ユーザー(フミ)とリリンの1日分の会話ログと、現在の事実メモを読み、"
    "事実メモへの変更操作だけをJSONで返してください。",

    "事実メモのカテゴリ:\n"
    "- 恒久: 来月になっても変わらず正しい、ユーザー本人が明確に述べた事実"
    "(名前、仕事、住まい、長期的な習慣、好み、関係者がいること)\n"
    "- 一時: 数日で変わる状態(体調、今週の予定、最近の出来事)。daysに有効日数を入れる\n"
    "- 話題: 後日リリンから「あれどうだった？」と続きを聞く価値のある話"
    "(これから行く予定、始めたこと、約束)。daysに有効日数を入れる",

    "守ること:\n"
    "- 記録してよいのはユーザー本人が述べたことだけ。リリンの発言内容や、"
    "会話から推測した性格・関係性の解釈は記録しない\n"
    "- 会話ログは音声認識を通しており、聞き間違いが多い。文脈と繋がらない"
    "単語や、意味の通らない短い断片は記録しない。迷ったら記録しない\n"
    "- 「リリ」「リリン」はAIアシスタント自身の名前で、ユーザーの関係者ではない\n"
    "- 既存の事実と同じ内容は追加しない。内容が新しく分かった・変わった場合は"
    "updateで既存の項目を書き換える(矛盾したら新しい方を優先)\n"
    "- 事実メモの項目が誤り・重複・もう当てはまらないと会話から分かった場合だけdeleteする\n"
    "- 天気・時刻など、その場限りの情報は記録しない\n"
    "- textは「〜が好き」「〜に住んでいる」のような短い一文\n"
    "- 「今日」「明日」「来週」のような相対的な言い方は、後日読むと意味がずれるため使わず、"
    "会話ログの日付を基準に「10/31に〜」のような日付で書く",

    "出力形式(このJSONだけを返す。説明や前置きは書かない。変更が無ければ空の配列):\n"
    '{"add": [{"category": "恒久|一時|話題", "text": "...", "days": 3}], '
    '"update": [{"id": "f12", "text": "...", "days": 7}], '
    '"delete": ["f20"]}\n'
    "daysは一時・話題の場合のみ。恒久ではnull。",
])


def _load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def _save_state(state: dict) -> None:
    os.makedirs(facts.DREAMING_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def _facts_for_prompt(items: list) -> str:
    if not items:
        return "(まだ何もありません)"
    lines = []
    for f in items:
        meta = "手動入力" if f["source"] == facts.MANUAL else f"出典{f['source']}"
        if f.get("expires"):
            meta += f"・{f['expires']}まで"
        lines.append(f"{f['id']} [{f['category']}] {f['text']} ({meta})")
    return "\n".join(lines)


def _parse_ops(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"JSONが見つからない: {raw[:200]}")
    ops = json.loads(m.group(0))
    return {k: ops.get(k) or [] for k in ("add", "update", "delete")}


def _expires(day: str, category: str, days) -> str:
    if category == "恒久":
        return None
    try:
        n = int(days)
    except (TypeError, ValueError):
        n = DEFAULT_DAYS[category]
    n = max(1, min(n, MAX_DAYS))
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def _apply(items: list, ops: dict, day: str) -> list:
    """適用した変更内容(ログ用の文字列)を返す。itemsはその場で書き換える。"""
    changes = []
    by_id = {f["id"]: f for f in items}

    for fid in ops["delete"]:
        f = by_id.get(fid)
        if f and f["source"] != facts.MANUAL:
            items.remove(f)
            changes.append(f"- 削除 {fid} [{f['category']}] {f['text']}")

    for u in ops["update"]:
        f = by_id.get(u.get("id"))
        if not f or f not in items or f["source"] == facts.MANUAL or not u.get("text"):
            continue
        old = f["text"]
        f["text"] = u["text"].strip()
        f["source"] = day
        f["expires"] = _expires(day, f["category"], u.get("days"))
        changes.append(f"- 更新 {f['id']} {old} → {f['text']}")

    existing_texts = {f["text"] for f in items}
    for a in ops["add"]:
        category, text = a.get("category"), (a.get("text") or "").strip()
        if category not in facts.CATEGORIES or not text or text in existing_texts:
            continue
        f = {
            "id": facts.next_id(items),
            "category": category,
            "text": text,
            "source": day,
            "expires": _expires(day, category, a.get("days")),
        }
        items.append(f)
        existing_texts.add(text)
        changes.append(f"- 追加 {f['id']} [{category}] {text}")
    return changes


def _write_log(day: str, lines: list) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(os.path.join(LOG_DIR, f"{day}.md"), "a") as f:
        f.write("\n".join(lines) + "\n\n")


def dream_day(day: str) -> int:
    """指定日のログを整理してfacts.mdに反映する。適用した変更の件数を返す。"""
    path = os.path.join(MEMORY_DIR, f"{day}.md")
    if not os.path.exists(path):
        return 0
    with open(path) as f:
        log_text = f.read().strip()
    if not log_text:
        return 0

    user_content = (
        f"【会話ログの日付】{day}\n\n"
        f"【現在の事実メモ】\n{_facts_for_prompt(facts.active(facts.load(), date.fromisoformat(day)))}\n\n"
        f"【会話ログ】\n{log_text}"
    )
    raw = _llm_chat(
        _SYSTEM_PROMPT,
        [{"role": "user", "content": user_content}],
        temperature=0.2,
        timeout=600,
        reasoning_effort="medium",
    )
    ops = _parse_ops(raw)

    # LLM呼び出し中に設定画面で削除された項目を復活させないよう、適用直前に読み直す
    items = facts.load()
    changes = _apply(items, ops, day)
    facts.save(items)
    _write_log(day, [f"## {day}の会話からの整理"] + (changes or ["- 変更なし"]))
    return len(changes)


def prune_expired() -> int:
    items = facts.load()
    alive = facts.active(items)
    expired = [f for f in items if f not in alive]
    if expired:
        facts.save(alive)
        _write_log(
            date.today().isoformat(),
            ["## 期限切れで削除"] + [f"- {f['id']} [{f['category']}] {f['text']}" for f in expired],
        )
    return len(expired)


def run() -> int:
    """前回処理した日の翌日から昨日までを順に処理する。"""
    state = _load_state()
    yesterday = date.today() - timedelta(days=1)
    start = (
        date.fromisoformat(state["last_day"]) + timedelta(days=1)
        if state.get("last_day")
        else yesterday
    )
    start = max(start, yesterday - timedelta(days=MAX_CATCHUP_DAYS - 1))

    total = 0
    day = start
    while day <= yesterday:
        n = dream_day(day.isoformat())
        print(f"[dreaming] {day.isoformat()}: {n}件の変更")
        total += n
        state["last_day"] = day.isoformat()
        _save_state(state)
        day += timedelta(days=1)

    total += prune_expired()
    return total


def import_profile() -> int:
    """旧config.jsonのuser_profile(箇条書きの自由文)を恒久事実として取り込む。"""
    items = facts.load()
    today = date.today().isoformat()
    n = 0
    for line in config.load().get("user_profile", "").splitlines():
        text = re.sub(r"^[\s*・\-]+", "", line).strip()
        if text and text not in {f["text"] for f in items}:
            items.append(
                {"id": facts.next_id(items), "category": "恒久", "text": text, "source": today, "expires": None}
            )
            n += 1
    facts.save(items)
    return n


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--day")
    parser.add_argument("--import-profile", action="store_true")
    args = parser.parse_args()
    if args.import_profile:
        print(f"{import_profile()}件を取り込みました")
    elif args.day:
        print(f"{dream_day(args.day)}件の変更")
    else:
        run()

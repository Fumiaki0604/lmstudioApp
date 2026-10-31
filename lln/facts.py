"""Rilinの事実メモ(memory/_dreaming/facts.md)の読み書き。

自由文のuser_profileをLLMに丸ごと書き直させる方式では、一時的な状態
(「喉が痛い」)や聞き間違い(「移動先：霞ヶ所」)が恒久情報として残り
続けたため、1行1事実・出典日付・期限付きの形式に置き換えた。更新は
dreaming.pyが毎晩行い、期限切れの削除はここでコードが機械的に行う。
[手動]の項目は自動では消さない。

ファイルは手で編集してもよい。memory/直下に置くと日次ログとして
_load_recent_history()やmemory_searchに拾われてしまうため、サブディレクトリに置く。
"""
import os
import re
from datetime import date

DREAMING_DIR = os.path.join(os.path.dirname(__file__), "memory", "_dreaming")
FACTS_PATH = os.path.join(DREAMING_DIR, "facts.md")

CATEGORIES = ("恒久", "一時", "話題")
MANUAL = "手動"

_HEADER = "# Rilinの事実メモ(dreaming.pyが毎晩更新。手で編集してもよい。[手動]は自動で消されない)"
_LINE_RE = re.compile(
    r"^- (f\d+) (.+?) \[(手動|\d{4}-\d\d-\d\d)(?: → (\d{4}-\d\d-\d\d))?\]$"
)
_SECTION_RE = re.compile(r"^## (\S+)$")


def load() -> list:
    if not os.path.exists(FACTS_PATH):
        return []
    facts, category = [], None
    with open(FACTS_PATH) as f:
        for line in f.read().splitlines():
            m = _SECTION_RE.match(line)
            if m:
                category = m.group(1) if m.group(1) in CATEGORIES else None
                continue
            m = _LINE_RE.match(line)
            if m and category:
                facts.append(
                    {
                        "id": m.group(1),
                        "category": category,
                        "text": m.group(2),
                        "source": m.group(3),
                        "expires": m.group(4),
                    }
                )
    return facts


def save(facts: list) -> None:
    os.makedirs(DREAMING_DIR, exist_ok=True)
    lines = [_HEADER]
    for category in CATEGORIES:
        lines += ["", f"## {category}"]
        for f in facts:
            if f["category"] != category:
                continue
            meta = f["source"] + (f" → {f['expires']}" if f.get("expires") else "")
            lines.append(f"- {f['id']} {f['text']} [{meta}]")
    tmp = FACTS_PATH + ".tmp"
    with open(tmp, "w") as fp:
        fp.write("\n".join(lines) + "\n")
    os.replace(tmp, FACTS_PATH)


def next_id(facts: list) -> str:
    return f"f{max((int(f['id'][1:]) for f in facts), default=0) + 1}"


def active(facts: list, today: date = None) -> list:
    today = (today or date.today()).isoformat()
    return [f for f in facts if not f.get("expires") or f["expires"] >= today]


def delete(fact_id: str) -> None:
    save([f for f in load() if f["id"] != fact_id])


def _short_date(iso: str) -> str:
    return f"{int(iso[5:7])}/{int(iso[8:10])}"


def prompt_text() -> str:
    """会話用system_promptに入れる「ユーザーについて」の節。話題(約束・続き)は
    毎回入れると会話中に何度も蒸し返すため、ここには入れずproactive側で使う。"""
    facts = active(load())
    lines = [f"- {f['text']}" for f in facts if f["category"] == "恒久"]
    lines += [
        f"- {f['text']}(一時的な状態。{_short_date(f['expires'])}頃まで)"
        for f in facts
        if f["category"] == "一時"
    ]
    if not lines:
        return ""
    return "【ユーザーについて分かっていること】\n" + "\n".join(lines)


def open_topics() -> list:
    return [f for f in active(load()) if f["category"] == "話題"]

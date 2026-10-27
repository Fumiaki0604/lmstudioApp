"""会話ログ(memory/*.md)から、口癖化した定型文を含むRilinの発言を退避する。

履歴は日付をまたいで直近N件が読み込まれるため、一度テンプレ化した返答
(「……てかさ、フミくん、本当に頭の回転が遅すぎない？」が毎回付く等)が
ログに残ると、翌日以降もfew-shotの手本として模倣され続けることが実際に
確認された(2026-10-25〜26)。同じ日のRilin発言のうち、同一の文が
MIN_REPEAT_ENTRIES件以上に現れたら、その文を含む発言ごと
memory/_quarantine/ に移す(削除ではなく退避なので誤検知時は戻せる)。

使い方:
    python log_janitor.py            # 全日付を掃除
    python log_janitor.py --dry-run  # 退避対象を表示するだけ
"""
import argparse
import glob
import os
import re
from collections import Counter

import memory_search

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
QUARANTINE_DIR = os.path.join(MEMORY_DIR, "_quarantine")
_ENTRY_RE = re.compile(r"^### (\d\d:\d\d) (User|Rilin)$")

MIN_LINE_LEN = 12  # 「はぁ……」「ふぅ。」程度の短い相槌は口癖扱いしない
MIN_REPEAT_ENTRIES = 3


def normalize_line(line: str) -> str:
    return re.sub(r"\s+", "", line).strip("「」『』\"“”")


def significant_lines(text: str) -> set:
    """口癖判定の対象になる文(正規化済み・一定以上の長さ)の集合。"""
    return {n for n in (normalize_line(l) for l in text.splitlines()) if len(n) >= MIN_LINE_LEN}


def _parse(path: str):
    """(preamble行, [{"time","role","lines"}...]) を返す。linesは見出し行を含む生テキスト。"""
    with open(path) as f:
        lines = f.read().splitlines()
    preamble, entries = [], []
    for line in lines:
        m = _ENTRY_RE.match(line)
        if m:
            entries.append({"time": m.group(1), "role": m.group(2), "lines": [line]})
        elif entries:
            entries[-1]["lines"].append(line)
        else:
            preamble.append(line)
    return preamble, entries


def _body(entry: dict) -> str:
    return "\n".join(entry["lines"][1:])


def _contaminated(entries: list) -> list:
    rilin = [e for e in entries if e["role"] == "Rilin"]
    counts = Counter(l for e in rilin for l in significant_lines(_body(e)))
    repeated = {l for l, c in counts.items() if c >= MIN_REPEAT_ENTRIES}
    return [e for e in rilin if significant_lines(_body(e)) & repeated]


def _write(path: str, preamble: list, entries: list) -> None:
    lines = preamble + [l for e in entries for l in e["lines"]]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(lines).rstrip("\n") + "\n\n")
    os.replace(tmp, path)


def clean_day(path: str, dry_run: bool = False) -> int:
    day = os.path.basename(path)[: -len(".md")]
    preamble, entries = _parse(path)
    bad = _contaminated(entries)
    if not bad or dry_run:
        return len(bad)

    os.makedirs(QUARANTINE_DIR, exist_ok=True)
    with open(os.path.join(QUARANTINE_DIR, f"{day}.md"), "a") as f:
        for e in bad:
            f.write("\n".join(e["lines"]).rstrip("\n") + "\n\n")

    bad_ids = {id(e) for e in bad}
    _write(path, preamble, [e for e in entries if id(e) not in bad_ids])
    memory_search.forget({f"{day}_{e['time']}_{e['role']}" for e in bad})
    return len(bad)


def run(dry_run: bool = False) -> int:
    total = 0
    for path in sorted(glob.glob(os.path.join(MEMORY_DIR, "*.md"))):
        n = clean_day(path, dry_run)
        if n:
            print(f"[janitor] {os.path.basename(path)}: {n}件{'(dry-run)' if dry_run else 'を退避'}")
        total += n
    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    run(parser.parse_args().dry_run)

"""connpassの新着イベントから、ユーザーが興味を持ちそうな勉強会を選んでおく。

新着フィード(ja.atom)は最新10件しか載らないため、assistant.pyのスレッドが
10〜18時の2時間おきに取りに行って未見のものを溜め、18時の取得後に1回まとめて判定する。場所(オンライン/東京/神奈川)は
コードで絞り、興味の判定は事実メモ+設定画面のキーワードを元にLLMが行う。
選んだものはproactive_utterance()が話題として1件ずつ口頭で紹介する。

使い方(手動で取得して即判定する):
    python connpass.py
"""
import html
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime

import requests

import config
import facts
from converse import _llm_chat

FEED_URL = "https://connpass.com/explore/ja.atom"
# 取得は日中に数回行って溜めておき、LLMでの判定と紹介は1日1回(JUDGE_HOUR以降)にまとめる
FETCH_HOURS = (10, 12, 14, 16, 18)
POLL_INTERVAL_SEC = 300
JUDGE_HOUR = 18
JUDGE_BATCH = 15
STATE_PATH = os.path.join(os.path.dirname(__file__), "memory", "_connpass", "state.json")
# 住所は「東京都」を省いて区名から書かれることもあるため、都市名でも拾う
PLACE_PATTERNS = ("オンライン", "東京", "神奈川", "横浜", "川崎")
MAX_SEEN = 500

_NS = {"a": "http://www.w3.org/2005/Atom"}
_DATE_RE = re.compile(r"開催日時:\s*(\d{4})/(\d\d)/(\d\d)\s*(\d\d:\d\d)?")
_PLACE_RE = re.compile(r"開催場所:\s*([^<\n]*)")

_JUDGE_PROMPT = """あなたはユーザーの秘書です。勉強会の一覧から、ユーザーが参加したくなりそうなものだけを選んでください。

【ユーザーについて分かっていること】
{facts}

【ユーザーが登録した興味のキーワード】
{keywords}

判断基準:
- 上の情報(特にキーワード)の具体的な趣味・関心と内容が一致するものだけを選ぶ。0件でよい
- 「PCを持っている」「社会人」「東京在住」のような誰にでも当てはまる情報は、選ぶ理由にしない
- 子ども向け・学生限定・初心者向けの入門でユーザーの水準に合わなそうなものは選ばない

出力はJSONのみ: {{"picks": [{{"id": "一覧のid", "reason": "一致した関心(キーワードや事実)と、その理由(20字程度)"}}]}}"""


def _load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"seen": [], "picks": []}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    state["seen"] = state["seen"][-MAX_SEEN:]
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def fetch() -> list:
    res = requests.get(FEED_URL, timeout=15)
    res.raise_for_status()
    events = []
    for e in ET.fromstring(res.content).findall("a:entry", _NS):
        body = e.findtext("a:summary", "", _NS) or e.findtext("a:content", "", _NS)
        m = _DATE_RE.search(body)
        place = _PLACE_RE.search(body)
        link = e.find("a:link", _NS)
        events.append(
            {
                "id": e.findtext("a:id", "", _NS),
                "title": e.findtext("a:title", "", _NS).strip(),
                "url": link.get("href") if link is not None else "",
                "date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else "",
                "time": (m.group(4) or "") if m else "",
                "place": place.group(1).strip() if place else "",
                "summary": _text(_PLACE_RE.split(body, 1)[-1].split("<br />", 1)[-1])[:400],  # 日時・場所の行は別項目にしたので除く
            }
        )
    return events


def _place_ok(e: dict) -> bool:
    # 会場+オンライン併催は開催場所に会場住所しか載らないため、タイトルも見る
    return any(p in e["place"] for p in PLACE_PATTERNS) or "オンライン" in e["title"]


def _judge(events: list) -> list:
    keywords = config.load().get("connpass_keywords") or "(未登録)"
    listing = "\n".join(
        f"id={i} | {e['title']} | {e['date']} | {e['place']} | {e['summary'][:250]}" for i, e in enumerate(events)
    )
    raw = _llm_chat(
        _JUDGE_PROMPT.format(facts=facts.prompt_text() or "(なし)", keywords=keywords),
        [{"role": "user", "content": listing}],
        temperature=0.2,
        timeout=120,
        reasoning_effort="low",
    )
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"JSONが見つからない: {raw[:200]}")
    picks = []
    for p in json.loads(m.group(0)).get("picks") or []:
        try:
            e = events[int(p["id"])]
        except (KeyError, ValueError, IndexError):
            continue
        picks.append({**e, "reason": p.get("reason", ""), "told": False})
    return picks


def collect() -> int:
    """未見の新着のうち場所が合うものを判定待ちに溜め、溜めた件数を返す(LLMは使わない)。"""
    state = _load_state()
    seen = set(state["seen"])
    new = [e for e in fetch() if e["id"] not in seen]
    state["seen"] += [e["id"] for e in new]
    state.setdefault("pending", []).extend(e for e in new if _place_ok(e))
    _save_state(state)
    return len(new)


def judge_pending() -> int:
    """溜まった候補をまとめて判定し、選んだ件数を返す。"""
    state = _load_state()
    today = date.today().isoformat()
    candidates = [e for e in state.get("pending", []) if e["date"] >= today]
    picks = []
    for i in range(0, len(candidates), JUDGE_BATCH):
        picks += _judge(candidates[i : i + JUDGE_BATCH])
    state["picks"] = [p for p in state["picks"] if p["date"] >= today] + picks
    state["pending"] = []
    state["judged_date"] = today
    _save_state(state)
    for p in picks:
        print(f"[connpass] picked: {p['title']} ({p['reason']})")
    return len(picks)


def poll() -> None:
    """FETCH_HOURSの各時に1回新着を溜め、JUDGE_HOUR以降なら1日1回まとめて判定する。"""
    now = datetime.now()
    slot = f"{now.date().isoformat()} {now.hour}"
    if now.hour in FETCH_HOURS and _load_state().get("fetched_slot") != slot:
        collect()
        state = _load_state()
        state["fetched_slot"] = slot
        _save_state(state)
    if datetime.now().hour >= JUDGE_HOUR and _load_state().get("judged_date") != date.today().isoformat():
        judge_pending()


def next_untold():
    """まだ伝えていない、開催日が過ぎていない候補を1件取り出し、伝えた扱いにする。"""
    state = _load_state()
    today = date.today().isoformat()
    for p in sorted(state["picks"], key=lambda p: p["date"]):
        if not p["told"] and p["date"] >= today:
            p["told"] = True
            _save_state(state)
            return p
    return None


def describe(p: dict) -> str:
    d = datetime.strptime(p["date"], "%Y-%m-%d")
    when = f"{d.month}月{d.day}日" + (f" {p['time']}から" if p["time"] else "")
    return f"「{p['title']}」{when}、{p['place']}。合いそうな理由:{p['reason']}。内容:{p['summary'][:200]}"


if __name__ == "__main__":
    collect()
    print(judge_pending())

"""memory/*.md の過去ログをembedding化し、意味検索する。

lmstudioApp の embedding.py と同じモデル(GLuCoSE-base-ja、日本語特化)を使う。
埋め込みは memory/.embeddings.json にキャッシュする。過去の(今日以外の)
日付ファイルは一度全件キャッシュしたら二度と読み返さない
(scanned_days に記録)。読み返すのは today のファイルだけなので、
履歴が長くなっても毎回のコストは増えない。

検索対象は「興味度TOP5(リリン視点でLLM採点)＋直近15件」の計20件に絞った
作業セットのみ。直近だけど瑣末な話題(お風呂入る・もう寝る等)が意味的に
近いというだけで埋もれさせず、逆に少し前でも興味を持った話題は拾い続ける
ための仕組み。全件(数百件)と毎回比較するより候補が絞られる分、検索自体は
むしろ速くなる。興味度の採点はLLM呼び出しを伴うため、検索のたび(=会話の
たび)には行わず、assistant.pyのproactive_loopから低頻度(30分おき等)で
update_interest_scores()を呼ぶ想定。
"""
import glob
import json
import os
import re
from datetime import date

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
CACHE_PATH = os.path.join(MEMORY_DIR, ".embeddings.json")
MODEL_NAME = "pkshatech/GLuCoSE-base-ja"

# converse.pyがmemory_search.pyをimportしているため、循環import回避のため
# LLM接続先はconverse.pyから共有せずここで独立に持つ(値は重複するが許容)。
LLM_CHAT_URL = "http://127.0.0.1:8000/v1/chat/completions"
LLM_MODEL = "mlx-community/Qwen3.6-35B-A3B-4bit"

WORKING_SET_INTEREST_N = 5
WORKING_SET_RECENT_N = 15

_ENTRY_RE = re.compile(r"^### (\d\d:\d\d) (User|Rilin)$")

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _iter_entries_for_day(path: str):
    day = os.path.basename(path)[: -len(".md")]
    with open(path) as f:
        lines = f.read().splitlines()

    time_, role, buf = None, None, []
    for line in lines:
        m = _ENTRY_RE.match(line)
        if m:
            if role is not None:
                key = f"{day}_{time_}_{role}"
                yield key, f"[{day} {time_} {role}] " + "\n".join(buf).strip()
            time_, role, buf = m.group(1), m.group(2), []
        elif role is not None:
            buf.append(line)
    if role is not None:
        key = f"{day}_{time_}_{role}"
        yield key, f"[{day} {time_} {role}] " + "\n".join(buf).strip()


def _load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            data = json.load(f)
        if "entries" in data and "scanned_days" in data:
            return data
        return {"entries": data, "scanned_days": []}  # 旧形式(フラット辞書)からの移行
    return {"entries": {}, "scanned_days": []}


def _save_cache(cache: dict) -> None:
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, ensure_ascii=False)


def _update_index() -> dict:
    cache = _load_cache()
    entries = cache["entries"]
    scanned_days = set(cache["scanned_days"])
    today = date.today().isoformat()

    new_texts = {}
    for path in sorted(glob.glob(os.path.join(MEMORY_DIR, "*.md"))):
        day = os.path.basename(path)[: -len(".md")]
        if day in scanned_days and day != today:
            continue  # 完了済みの過去日は二度と開かない
        for key, text in _iter_entries_for_day(path):
            if key not in entries:
                new_texts[key] = text
        if day != today:
            scanned_days.add(day)

    if new_texts:
        model = _get_model()
        keys = list(new_texts.keys())
        vectors = model.encode([new_texts[k] for k in keys]).tolist()
        for k, v in zip(keys, vectors):
            entries[k] = {"text": new_texts[k], "vector": v}

    cache["scanned_days"] = sorted(scanned_days)
    _save_cache(cache)
    return entries


def forget(keys: set) -> None:
    """退避した発言のembeddingをキャッシュから消す(過去日は再スキャンされないため明示的に消す必要がある)。"""
    cache = _load_cache()
    for k in keys:
        cache["entries"].pop(k, None)
    _save_cache(cache)


def _select_working_set(entries: dict) -> dict:
    """検索対象を興味度TOP5+直近15件の計20件に絞る。
    keyは"day_HH:MM_role"形式でISO日付+ゼロ埋め時刻のため、文字列として
    降順ソートするだけで新しい順になる(専用のタイムスタンプ不要)。
    興味度は機械的な時間減衰をせず、_refresh_top_interest_scores()による
    リリン自身の定期的な再判断だけで上下する(詳細はその関数のdocstring)。"""
    newest_first = sorted(entries.items(), key=lambda kv: kv[0], reverse=True)

    scored = [kv for kv in newest_first if kv[1].get("interest") is not None]
    scored.sort(key=lambda kv: -kv[1]["interest"])  # 安定ソートなので同点は新しい順を維持
    top_interest = scored[:WORKING_SET_INTEREST_N]
    top_interest_keys = {k for k, _ in top_interest}

    remaining = [kv for kv in newest_first if kv[0] not in top_interest_keys]
    top_recent = remaining[:WORKING_SET_RECENT_N]

    working = dict(top_interest)
    working.update(top_recent)
    return working


def search(query: str, top_k: int = 3, min_score: float = 0.5):
    import numpy as np

    entries = _update_index()
    if not entries:
        return []

    working = _select_working_set(entries)

    model = _get_model()
    q_vec = np.array(model.encode([query])[0])

    scored = []
    for item in working.values():
        v = np.array(item["vector"])
        denom = np.linalg.norm(q_vec) * np.linalg.norm(v) + 1e-9
        score = float(np.dot(q_vec, v) / denom)
        scored.append((score, item["text"]))
    scored.sort(key=lambda x: -x[0])

    return [text for score, text in scored[:top_k] if score >= min_score]


_SCORE_LINE_RE = re.compile(r"^\s*(\d+)\s*[:：]\s*(\d+)")


def _llm_interest_scores(system_prompt: str, items: list) -> dict:
    """「番号: 点数」形式で0〜10点を付けさせる採点呼び出しの共通処理。
    itemsのインデックス(0始まり)→採点結果のdictを返す。採点自体に失敗
    したり、一部の番号が出力されなかった場合は、該当分だけ結果に含まれない
    (呼び出し側は既存スコアをそのまま維持すればよい)。"""
    import requests

    numbered = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(items))
    try:
        res = requests.post(
            LLM_CHAT_URL,
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": numbered},
                ],
                "reasoning_effort": "none",
                "temperature": 0.2,
            },
            timeout=60,
        )
        res.raise_for_status()
        raw = res.json()["choices"][0]["message"]["content"]
    except Exception:
        return {}

    results = {}
    for line in raw.splitlines():
        m = _SCORE_LINE_RE.match(line)
        if not m:
            continue
        idx, score = int(m.group(1)) - 1, int(m.group(2))
        if 0 <= idx < len(items):
            results[idx] = max(0, min(10, score))
    return results


def _refresh_top_interest_scores(entries: dict) -> None:
    """機械的な時間減衰や登場回数ではなく、リリン自身に「今振り返っても
    まだ興味があるか」を定期的に再判断させる。スイーツのように世の中の
    方で話題が次々入れ替わるジャンルは、リリンが今も関心を持ち続けて
    いれば高いままでよく、単に日数が経ったというだけで機械的には下げない。
    対象は現在の興味度上位勢(WORKING_SET_INTEREST_N件)のみに絞り、全件を
    毎回問い合わせるコストを避ける。"""
    scored = [(k, v) for k, v in entries.items() if v.get("interest") is not None]
    scored.sort(key=lambda kv: -kv[1]["interest"])
    top = scored[:WORKING_SET_INTEREST_N]
    if not top:
        return

    today = date.today().isoformat()
    items = [
        f"(前回採点:{v['interest']}点 / {k.split('_', 1)[0]}時点) {v['text'][:200]}"
        for k, v in top
    ]
    system_prompt = (
        f"あなたは「リリン」というキャラクターです。今日の日付は{today}です。"
        "以下は、これまでリリンが特に興味を持ったと判定された話題です。"
        "時間が経った今あらためて振り返って、まだ同じくらい興味があるかを"
        "あなた自身の判断で0〜10に採点し直してください。\n\n"
        "スイーツや流行りものなど、世の中で中身が次々入れ替わっていく"
        "ジャンルでも、そのジャンル自体への関心が続いているなら高いまま"
        "でよく、単に日数が経ったという理由だけで機械的に下げないで"
        "ください。逆に、今思い返すとそれほどでもなかった、もう気分が"
        "乗らないと思うなら素直に下げてください。\n\n"
        "出力は「番号: 点数」の形式で1行に1つずつ、他の説明は書かずに"
        "返してください。"
    )
    keys = [k for k, _ in top]
    for idx, score in _llm_interest_scores(system_prompt, items).items():
        entries[keys[idx]]["interest"] = score


def update_interest_scores() -> None:
    """未採点のキャッシュ済みエントリに対し、リリン視点の「興味度」(0〜10)を
    LLMでまとめて判定し、キャッシュに書き込む。LLM呼び出しを伴うため検索時
    (=会話のたび)には呼ばず、assistant.pyのproactive_loopから低頻度で
    呼ぶことを想定している。未採点エントリの新規採点と、既存の上位勢の
    再判断(_refresh_top_interest_scores参照)の両方を行う。"""
    cache = _load_cache()
    entries = cache["entries"]
    unscored = {k: v for k, v in entries.items() if "interest" not in v}
    if unscored:
        keys = list(unscored.keys())
        system_prompt = (
            "あなたは「リリン」というキャラクターです。以下は過去の会話ログの"
            "一部です。それぞれの発言について、リリン自身の視点で「この話題に"
            "どれくらい興味を持ったか」を0〜10の整数で判定してください。\n\n"
            "「お風呂に入る」「もう夜だから寝る」のような定型的な生活ルーティン"
            "の報告は低く(0〜2程度)評価してください。新しい趣味の話、相手の"
            "悩み相談、強い感情が伴う出来事、継続して話題になりそうな固有名詞"
            "(プロジェクト名など)は高く評価してください。\n\n"
            "出力は「番号: 点数」の形式で1行に1つずつ、他の説明は書かずに"
            "返してください。"
        )
        items = [unscored[k]["text"][:200] for k in keys]
        for idx, score in _llm_interest_scores(system_prompt, items).items():
            entries[keys[idx]]["interest"] = score

    _refresh_top_interest_scores(entries)
    _save_cache(cache)

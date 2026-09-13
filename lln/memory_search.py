"""memory/*.md の過去ログをembedding化し、意味検索する。

lmstudioApp の embedding.py と同じモデル(GLuCoSE-base-ja、日本語特化)を使う。
埋め込みは memory/.embeddings.json にキャッシュする。過去の(今日以外の)
日付ファイルは一度全件キャッシュしたら二度と読み返さない
(scanned_days に記録)。読み返すのは today のファイルだけなので、
履歴が長くなっても毎回のコストは増えない。
"""
import glob
import json
import os
import re
from datetime import date

MEMORY_DIR = os.path.join(os.path.dirname(__file__), "memory")
CACHE_PATH = os.path.join(MEMORY_DIR, ".embeddings.json")
MODEL_NAME = "pkshatech/GLuCoSE-base-ja"

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


def search(query: str, top_k: int = 3, min_score: float = 0.5):
    import numpy as np

    entries = _update_index()
    if not entries:
        return []

    model = _get_model()
    q_vec = np.array(model.encode([query])[0])

    scored = []
    for item in entries.values():
        v = np.array(item["vector"])
        denom = np.linalg.norm(q_vec) * np.linalg.norm(v) + 1e-9
        score = float(np.dot(q_vec, v) / denom)
        scored.append((score, item["text"]))
    scored.sort(key=lambda x: -x[0])

    return [text for score, text in scored[:top_k] if score >= min_score]

"""memory/*.md の過去ログをembedding化し、意味検索する。

lmstudioApp の embedding.py と同じモデル(GLuCoSE-base-ja、日本語特化)を使う。
埋め込みは memory/.embeddings.json にキャッシュし、新規分だけ差分計算する。
"""
import glob
import json
import os
import re

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


def _iter_entries():
    for path in sorted(glob.glob(os.path.join(MEMORY_DIR, "*.md"))):
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
            return json.load(f)
    return {}


def _save_cache(cache: dict) -> None:
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, ensure_ascii=False)


def _update_index() -> dict:
    cache = _load_cache()
    entries = dict(_iter_entries())
    new_keys = [k for k in entries if k not in cache]
    if new_keys:
        model = _get_model()
        vectors = model.encode([entries[k] for k in new_keys]).tolist()
        for k, v in zip(new_keys, vectors):
            cache[k] = {"text": entries[k], "vector": v}
        _save_cache(cache)
    return cache


def search(query: str, top_k: int = 3, min_score: float = 0.5):
    import numpy as np

    cache = _update_index()
    if not cache:
        return []

    model = _get_model()
    q_vec = np.array(model.encode([query])[0])

    scored = []
    for item in cache.values():
        v = np.array(item["vector"])
        denom = np.linalg.norm(q_vec) * np.linalg.norm(v) + 1e-9
        score = float(np.dot(q_vec, v) / denom)
        scored.append((score, item["text"]))
    scored.sort(key=lambda x: -x[0])

    return [text for score, text in scored[:top_k] if score >= min_score]

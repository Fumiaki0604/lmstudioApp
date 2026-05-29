"""HuggingFace embedding utility — pkshatech/GLuCoSE-base-ja."""
import hashlib
import threading
from typing import Optional

import numpy as np

_MODEL_NAME = "pkshatech/GLuCoSE-base-ja"
_SIMILARITY_THRESHOLD = 0.42  # この値以上で「関連あり」と判定

_model = None
_model_lock = threading.Lock()
_cache: dict[str, np.ndarray] = {}
_cache_lock = threading.Lock()
_MAX_CACHE = 500


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            _model = SentenceTransformer(_MODEL_NAME)
    return _model


def get_embedding(text: str) -> Optional[np.ndarray]:
    """テキストのembeddingベクトルを返す（キャッシュあり）。失敗時はNone。"""
    if not text or not text.strip():
        return None
    key = hashlib.md5(text.encode()).hexdigest()
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        model = _get_model()
        vec = model.encode(text, normalize_embeddings=True)
        with _cache_lock:
            if len(_cache) >= _MAX_CACHE:
                # 古いエントリを半分削除
                keys = list(_cache.keys())
                for k in keys[:_MAX_CACHE // 2]:
                    del _cache[k]
            _cache[key] = vec
        return vec
    except Exception:
        return None


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """正規化済みベクトル同士のコサイン類似度。"""
    return float(np.dot(a, b))


def text_similarity(text_a: str, text_b: str) -> float:
    """2テキスト間のコサイン類似度。失敗時は0.0。"""
    va = get_embedding(text_a)
    vb = get_embedding(text_b)
    if va is None or vb is None:
        return 0.0
    return cosine_similarity(va, vb)


def is_related(text_a: str, text_b: str, threshold: float = _SIMILARITY_THRESHOLD) -> bool:
    """2テキストが意味的に関連しているか判定。"""
    return text_similarity(text_a, text_b) >= threshold

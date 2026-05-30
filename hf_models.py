"""HuggingFace モデルコレクション（lazy-loaded / thread-safe）。

モデル一覧:
  1. 感情分析     koheiduck/bert-japanese-finetuned-sentiment  (~110MB)
  2. T5 要約      sonoisa/t5-base-japanese                      (~250MB)
  3. Cross-Encoder hotchpotch/japanese-reranker-cross-encoder-xsmall-v1 (~120MB)
  4. NLI          Formzu/bert-base-japanese-jsnli               (~110MB)

使い方:
  from hf_models import get_sentiment, summarize, cross_score, nli_classify
  from hf_models import SENTIMENT_AVAILABLE, T5_AVAILABLE, CE_AVAILABLE, NLI_AVAILABLE
"""
import threading
from typing import Optional

import numpy as np

# ─── 可用フラグ（importに失敗していたら False のまま） ──────────────────────
SENTIMENT_AVAILABLE = False
T5_AVAILABLE = False
CE_AVAILABLE = False
NLI_AVAILABLE = False

# ════════════════════════════════════════════════════════════════════════════
# 1. 感情分析  koheiduck/bert-japanese-finetuned-sentiment
# ════════════════════════════════════════════════════════════════════════════
_SENTIMENT_NAME = "koheiduck/bert-japanese-finetuned-sentiment"
_sentiment_pipe = None
_sentiment_lock = threading.Lock()


def _get_sentiment_pipe():
    global _sentiment_pipe, SENTIMENT_AVAILABLE
    if _sentiment_pipe is not None:
        return _sentiment_pipe
    with _sentiment_lock:
        if _sentiment_pipe is None:
            from transformers import pipeline
            _sentiment_pipe = pipeline(
                "text-classification",
                model=_SENTIMENT_NAME,
                tokenizer=_SENTIMENT_NAME,
                top_k=None,
            )
            SENTIMENT_AVAILABLE = True
    return _sentiment_pipe


def get_sentiment(text: str) -> dict:
    """Returns {"label": "POSITIVE"/"NEGATIVE"/"NEUTRAL", "score": float}.
    失敗時は {"label": "NEUTRAL", "score": 1.0}。
    """
    if not text or not text.strip():
        return {"label": "NEUTRAL", "score": 1.0}
    try:
        pipe = _get_sentiment_pipe()
        results = pipe(text[:512], truncation=True)[0]
        best = max(results, key=lambda r: r["score"])
        return {"label": best["label"].upper(), "score": best["score"],
                "all": {r["label"].upper(): r["score"] for r in results}}
    except Exception:
        return {"label": "NEUTRAL", "score": 1.0}


def sentiment_label(text: str) -> str:
    """POSITIVE / NEGATIVE / NEUTRAL を返す。"""
    return get_sentiment(text)["label"]


# ════════════════════════════════════════════════════════════════════════════
# 2. T5 要約  sonoisa/t5-base-japanese
# ════════════════════════════════════════════════════════════════════════════
_T5_NAME = "sonoisa/t5-base-japanese"
_t5_tokenizer = None
_t5_model = None
_t5_lock = threading.Lock()


def _get_t5():
    global _t5_tokenizer, _t5_model, T5_AVAILABLE
    if _t5_model is not None:
        return _t5_tokenizer, _t5_model
    with _t5_lock:
        if _t5_model is None:
            from transformers import T5ForConditionalGeneration, T5Tokenizer
            _t5_tokenizer = T5Tokenizer.from_pretrained(_T5_NAME, legacy=False)
            _t5_model = T5ForConditionalGeneration.from_pretrained(_T5_NAME)
            _t5_model.eval()
            T5_AVAILABLE = True
    return _t5_tokenizer, _t5_model


def summarize(text: str, max_length: int = 128, min_length: int = 20) -> str:
    """テキストを要約して返す。失敗時は先頭 max_length 文字を返す。"""
    if not text or not text.strip():
        return text
    try:
        import torch
        tokenizer, model = _get_t5()
        input_text = "要約: " + text.replace("\n", " ")
        inputs = tokenizer.encode(
            input_text, return_tensors="pt", max_length=512, truncation=True
        )
        with torch.no_grad():
            outputs = model.generate(
                inputs,
                max_length=max_length,
                min_length=min_length,
                num_beams=4,
                early_stopping=True,
                no_repeat_ngram_size=3,
            )
        return tokenizer.decode(outputs[0], skip_special_tokens=True)
    except Exception:
        return text[:max_length]


# ════════════════════════════════════════════════════════════════════════════
# 3. Cross-Encoder  hotchpotch/japanese-reranker-cross-encoder-xsmall-v1
# ════════════════════════════════════════════════════════════════════════════
_CE_NAME = "hotchpotch/japanese-reranker-cross-encoder-xsmall-v1"
_cross_encoder = None
_ce_lock = threading.Lock()


def _get_cross_encoder():
    global _cross_encoder, CE_AVAILABLE
    if _cross_encoder is not None:
        return _cross_encoder
    with _ce_lock:
        if _cross_encoder is None:
            from sentence_transformers import CrossEncoder
            _cross_encoder = CrossEncoder(_CE_NAME, max_length=512)
            CE_AVAILABLE = True
    return _cross_encoder


def cross_score(text_a: str, text_b: str) -> float:
    """2テキスト間の関連スコア (0〜1)。失敗時は 0.5。"""
    if not text_a or not text_b:
        return 0.5
    try:
        ce = _get_cross_encoder()
        raw = ce.predict([[text_a, text_b]])[0]
        # raw はロジット値なので sigmoid で正規化
        return float(1.0 / (1.0 + np.exp(-float(raw))))
    except Exception:
        return 0.5


# ════════════════════════════════════════════════════════════════════════════
# 4. NLI  Formzu/bert-base-japanese-jsnli
# ════════════════════════════════════════════════════════════════════════════
_NLI_NAME = "Formzu/bert-base-japanese-jsnli"
_nli_pipe = None
_nli_lock = threading.Lock()


def _get_nli_pipe():
    global _nli_pipe, NLI_AVAILABLE
    if _nli_pipe is not None:
        return _nli_pipe
    with _nli_lock:
        if _nli_pipe is None:
            from transformers import pipeline
            _nli_pipe = pipeline(
                "text-classification",
                model=_NLI_NAME,
                tokenizer=_NLI_NAME,
                top_k=None,
            )
            NLI_AVAILABLE = True
    return _nli_pipe


def nli_classify(premise: str, hypothesis: str) -> dict:
    """Returns {"entailment": float, "neutral": float, "contradiction": float}.
    スコアの合計は ≈ 1.0。失敗時は均等分布。
    """
    _default = {"entailment": 0.33, "neutral": 0.34, "contradiction": 0.33}
    if not premise or not hypothesis:
        return _default
    try:
        pipe = _get_nli_pipe()
        # text_pair 形式は結果がフラットなリスト
        results = pipe({"text": premise[:256], "text_pair": hypothesis[:128]}, truncation=True)
        scores = {}
        for r in results:
            label = r["label"].lower()
            # モデルによってラベルが英語 or 数字の場合がある
            if "entail" in label or label == "0":
                scores["entailment"] = r["score"]
            elif "contra" in label or label == "2":
                scores["contradiction"] = r["score"]
            else:
                scores["neutral"] = r["score"]
        return {
            "entailment": scores.get("entailment", 0.33),
            "neutral": scores.get("neutral", 0.34),
            "contradiction": scores.get("contradiction", 0.33),
        }
    except Exception:
        return _default


def is_event_proposal(text: str, threshold: float = 0.35) -> Optional[bool]:
    """NLIで「イベント/活動の提案」かどうか判定。
    entailment >= threshold → True
    contradiction > 0.6    → False
    それ以外                → None（LLMに委ねる）
    """
    hypothesis = "誰かが何かを提案または計画しています。"
    result = nli_classify(text, hypothesis)
    if result["entailment"] >= threshold:
        return True
    if result["contradiction"] > 0.6:
        return False
    return None  # uncertain → LLMへ

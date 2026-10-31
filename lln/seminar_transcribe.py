"""セミナー音声の文字起こし(MLX-Whisper)。.venv_seminar(Python 3.11)で動かす。

本番のassistant.py等はXcode同梱のPython 3.9で動いており、mlx/torch等の
重い依存を混ぜたくないため、seminar.pyから別プロセスとして呼ばれる。

使い方:
    .venv_seminar/bin/python seminar_transcribe.py <入力> <出力JSON> [開始オフセット秒]
入力は16kHz・mono・16bitのWAV、またはrec.appが書き出す生PCM(.pcm)。録音中に
5分ごとのファイルを順に文字起こしする時は、オフセットでファイルの開始時刻を足す。
"""
import json
import sys
import wave

import mlx_whisper
import numpy as np

MODEL = "mlx-community/whisper-large-v3-turbo"

# Whisperは無音・BGM区間で「ご視聴ありがとうございました」等の定型句を
# 捏造しがちなため、無音らしさが高く確信度も低い区間は捨てる
NO_SPEECH_PROB_MAX = 0.6
AVG_LOGPROB_MIN = -1.0


def load_audio(path: str) -> np.ndarray:
    if path.endswith(".pcm"):
        with open(path, "rb") as f:
            data = f.read()
        pcm = np.frombuffer(data[: len(data) - len(data) % 2], dtype="<i2")
    else:
        with wave.open(path) as w:
            assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    return pcm.astype(np.float32) / 32768.0


def main(in_path: str, out_path: str, offset: float = 0.0) -> None:
    audio = load_audio(in_path)
    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo=MODEL,
        language="ja",
        # 前の区間の文を条件にすると、同じ文を延々と繰り返す暴走が起きやすい
        condition_on_previous_text=False,
        verbose=False,
    )
    segments = []
    for s in result["segments"]:
        text = s["text"].strip()
        if not text:
            continue
        if s.get("no_speech_prob", 0) > NO_SPEECH_PROB_MAX and s.get("avg_logprob", 0) < AVG_LOGPROB_MIN:
            continue
        if segments and segments[-1]["text"] == text:
            continue  # 同じ文の連続(暴走)は1つにまとめる
        segments.append({"start": round(s["start"] + offset, 1), "end": round(s["end"] + offset, 1), "text": text})
    with open(out_path, "w") as f:
        json.dump({"duration": round(len(audio) / 16000, 1), "segments": segments}, f, ensure_ascii=False)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 0.0)

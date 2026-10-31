"""セミナーの録音・文字起こし・要約・Discord投稿。

録音はrec.app(マイク or Macの内部音声)が行い、ここでは開始・停止と、
録音後の処理(文字起こし→要約→Discord→音声の圧縮保存)を受け持つ。
開始すると監視用のプロセス(supervise)を切り離して起動し、そのプロセスが
rec.appの終了(停止指示 or 上限時間)を待ってから後処理まで行う。そのため
開始・停止をassistant.py(声)と設定画面(ボタン)のどちらから行っても、
呼び出し元の再起動に後処理が巻き込まれない。

録音ごとに seminars/<日時>/ を作り、meta.json(タイトル・状態)、録音中の
seg_*.pcm、処理後の transcript.md / summary.md / audio.m4a を置く。
音声だけ AUDIO_RETENTION_DAYS 日で消す(文字起こし・要約は残す)。

使い方:
    python seminar.py start mic|system [タイトル]
    python seminar.py stop
    python seminar.py ingest <音声/動画ファイル> [タイトル]   # 手持ちのファイルを処理
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
import wave
from datetime import datetime, timedelta

import config
import discord_notify

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEMINAR_DIR = os.path.join(BASE_DIR, "seminars")
REC_APP = os.path.join(BASE_DIR, "rec.app")
VENV_PY = os.path.join(BASE_DIR, ".venv_seminar", "bin", "python")
TRANSCRIBE_SCRIPT = os.path.join(BASE_DIR, "seminar_transcribe.py")

MAX_SEC = 3 * 60 * 60  # 停止し忘れても、この時間でRilinを通常モードに戻す
CHUNK_SEC = 15 * 60  # 長文を一度に要約すると中盤が薄くなるため、この単位で要約してから統合する
MIN_DURATION_SEC = 10
AUDIO_RETENTION_DAYS = 30
MODE_LABEL = {"mic": "オフライン(マイク)", "system": "オンライン(PCの音)", "file": "ファイル"}


# ---------------------------------------------------------------------------
# 状態管理
# ---------------------------------------------------------------------------


def _meta_path(d: str) -> str:
    return os.path.join(d, "meta.json")


def load_meta(d: str) -> dict:
    with open(_meta_path(d)) as f:
        return json.load(f)


def _update_meta(d: str, **kw) -> dict:
    meta = load_meta(d) if os.path.exists(_meta_path(d)) else {}
    meta.update(kw)
    tmp = _meta_path(d) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _meta_path(d))
    return meta


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _rec_pid(d: str):
    try:
        with open(os.path.join(d, "rec.pid")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def recent(n: int = 10) -> list:
    """新しい順に(ディレクトリ, meta)を返す。"""
    dirs = sorted(glob.glob(os.path.join(SEMINAR_DIR, "*", "meta.json")), reverse=True)[:n]
    return [(os.path.dirname(p), load_meta(os.path.dirname(p))) for p in dirs]


def active():
    """録音中のディレクトリ(なければNone)。監視プロセスが死んでいる古い状態は無視する。"""
    for d, meta in recent(3):
        if meta.get("status") == "recording" and _alive(meta.get("supervisor_pid")):
            return d
    return None


def is_recording() -> bool:
    return active() is not None


# ---------------------------------------------------------------------------
# 開始・停止
# ---------------------------------------------------------------------------


def _new_dir() -> str:
    d = os.path.join(SEMINAR_DIR, datetime.now().strftime("%Y-%m-%d_%H%M%S"))
    os.makedirs(d)
    return d


def start(mode: str, title: str = "") -> str:
    if mode not in ("mic", "system"):
        raise ValueError(mode)
    if is_recording():
        raise RuntimeError("すでに録音中です")
    d = _new_dir()
    _update_meta(d, title=title.strip(), mode=mode, status="recording", started=datetime.now().isoformat(timespec="seconds"))
    with open(os.path.join(d, "supervise.log"), "a") as log:
        proc = subprocess.Popen(
            [sys.executable, "-u", os.path.abspath(__file__), "supervise", d],
            cwd=BASE_DIR,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # 呼び出し元(assistant/設定画面)が再起動しても巻き込まれないように
        )
    _update_meta(d, supervisor_pid=proc.pid)
    return d


def stop() -> bool:
    d = active()
    if not d:
        return False
    pid = _rec_pid(d)
    if pid and _alive(pid):
        os.kill(pid, signal.SIGTERM)
    return True


def supervise(d: str) -> None:
    meta = load_meta(d)
    subprocess.run(
        [
            "open", "-n", REC_APP, "--stdout", os.path.join(d, "rec.log"), "--stderr", os.path.join(d, "rec.log"),
            "--args", "--mode", meta["mode"], "--out", d, "--max-sec", str(MAX_SEC),
        ],
        check=True,
    )
    deadline = time.time() + 30
    while _rec_pid(d) is None and time.time() < deadline:
        time.sleep(0.5)
    pid = _rec_pid(d)
    while pid and _alive(pid):
        time.sleep(2)
    _update_meta(d, ended=datetime.now().isoformat(timespec="seconds"))
    process(d)


# ---------------------------------------------------------------------------
# 録音後の処理
# ---------------------------------------------------------------------------


def _concat_pcm(d: str) -> str:
    wav_path = os.path.join(d, "audio.wav")
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        for seg in sorted(glob.glob(os.path.join(d, "seg_*.pcm"))):
            with open(seg, "rb") as f:
                data = f.read()
            w.writeframes(data[: len(data) - len(data) % 2])  # 異常終了時の端数バイトを捨てる
    return wav_path


def _convert_to_wav(src: str, dst: str) -> None:
    """任意の音声・動画をafconvertで16kHz/mono/16bitにし、標準的なWAVとして書き直す。
    afconvertは入力によってWAVE_FORMAT_EXTENSIBLE形式で出力し、Pythonのwave
    モジュール(3.11以前)がそれを読めないため、dataチャンクだけ取り出して詰め直す。"""
    tmp = dst + ".afconvert.wav"
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", src, tmp], check=True)
    with open(tmp, "rb") as f:
        raw = f.read()
    os.remove(tmp)
    pos, data = 12, b""
    while pos + 8 <= len(raw):
        cid, size = raw[pos : pos + 4], int.from_bytes(raw[pos + 4 : pos + 8], "little")
        if cid == b"data":
            data = raw[pos + 8 : pos + 8 + size]
            break
        pos += 8 + size + (size & 1)
    with wave.open(dst, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(data)


def _wav_duration(path: str) -> float:
    with wave.open(path) as w:
        return w.getnframes() / w.getframerate()


def _ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


_CHUNK_PROMPT = (
    "あなたはセミナーの記録係です。以下はセミナー音声を自動で文字起こししたものの一部"
    "({start}〜{end})です。音声認識の聞き間違いを含むので、文脈から補って解釈してください。"
    "以下の形式のMarkdownだけを返してください(前置きや説明は書かない)。\n\n"
    "### 流れ\n- [h:mm:ss] その時刻から話された話題(数分おきに1行。時刻は文字起こしの時刻を使う)\n"
    "### 要点\n- この部分で語られた重要な内容(最大6個。内容が少なければ少なくてよい)\n"
    "### 質疑応答\n- Q: 質問 / A: 回答(この部分に質疑応答が無ければ「なし」とだけ書く)"
)

_FINAL_PROMPT = (
    "あなたはセミナーの記録係です。以下は、1つのセミナーを時間ごとに区切って要約したものです。"
    "これらを統合して、セミナー全体の要約を以下の形式のMarkdownで作ってください"
    "(前置きや説明は書かない)。\n\n"
    "# タイトル(指定があればそれをそのまま使い、無ければ内容から短く付ける)\n\n"
    "## 要点\n- セミナー全体で重要な内容(最大10個。短いセミナーなら少なくてよい)\n\n"
    "## 時間ごとの流れ\n- [h:mm:ss] 話題(全体の目次として読める粒度に整理する)\n\n"
    "## 質疑応答\n- **Q.** 質問\n  **A.** 回答\n"
    "(質疑応答が1つも無ければ「質疑応答はありませんでした。」とだけ書く。"
    "話者は区別できていないため、誰の発言かは推測しすぎない。"
    "個数を埋めるために、要約に無い内容や一般論を付け足さない)"
)


def summarize(segments: list, title: str) -> str:
    from converse import _llm_chat  # 重い依存を録音開始・停止の経路に持ち込まないため遅延import

    chunks = {}
    for s in segments:
        chunks.setdefault(int(s["start"] // CHUNK_SEC), []).append(s)

    parts = []
    for i in sorted(chunks):
        segs = chunks[i]
        text = "\n".join(f"[{_ts(s['start'])}] {s['text']}" for s in segs)
        prompt = _CHUNK_PROMPT.format(start=_ts(segs[0]["start"]), end=_ts(segs[-1]["end"]))
        part = _llm_chat(prompt, [{"role": "user", "content": text}], temperature=0.3, timeout=600, reasoning_effort="low")
        parts.append(f"## {_ts(segs[0]['start'])}〜{_ts(segs[-1]['end'])}\n{part}")
        print(f"[seminar] chunk {i + 1}/{len(chunks)} summarized", flush=True)

    user_content = f"【タイトルの指定】{title or '(なし)'}\n\n" + "\n\n".join(parts)
    return _llm_chat(_FINAL_PROMPT, [{"role": "user", "content": user_content}], temperature=0.3, timeout=900, reasoning_effort="medium")


def _discord_text(summary: str, meta: dict, duration: float) -> str:
    title = next((l[2:].strip() for l in summary.splitlines() if l.startswith("# ")), meta.get("title") or "セミナー")
    m = re.search(r"## 要点\n(.*?)(?=\n## |\Z)", summary, re.S)
    points = m.group(1).strip() if m else ""
    started = meta.get("started", "")[:16].replace("T", " ")
    header = f"**{title}**\n{started} · {MODE_LABEL.get(meta.get('mode'), '')} · {_ts(duration)}\n\n"
    footer = "\n\n時間ごとの流れ・質疑応答・全文の文字起こしは添付ファイルを見てね。"
    room = discord_notify.DISCORD_MESSAGE_LIMIT - len(header) - len(footer) - 10
    if len(points) > room:
        points = points[:room] + "…"
    return header + points + footer


def process(d: str) -> None:
    meta = _update_meta(d, status="processing")
    webhook = config.load().get("discord_seminar_webhook_url", "")
    try:
        wav = os.path.join(d, "audio.wav")
        if glob.glob(os.path.join(d, "seg_*.pcm")):
            wav = _concat_pcm(d)
        duration = _wav_duration(wav)
        if duration < MIN_DURATION_SEC:
            raise RuntimeError(f"録音が短すぎます({duration:.1f}秒)。マイクや画面収録の許可を確認してください")

        print(f"[seminar] transcribing {_ts(duration)}", flush=True)
        transcript_json = os.path.join(d, "transcript.json")
        subprocess.run([VENV_PY, TRANSCRIBE_SCRIPT, wav, transcript_json], check=True)
        with open(transcript_json) as f:
            segments = json.load(f)["segments"]
        if not segments:
            raise RuntimeError("音声から言葉を聞き取れませんでした")
        transcript_md = os.path.join(d, "transcript.md")
        with open(transcript_md, "w") as f:
            f.write("\n".join(f"[{_ts(s['start'])}] {s['text']}" for s in segments) + "\n")

        summary = summarize(segments, meta.get("title", ""))
        summary_md = os.path.join(d, "summary.md")
        with open(summary_md, "w") as f:
            f.write(summary + "\n")
        title = next((l[2:].strip() for l in summary.splitlines() if l.startswith("# ")), "")

        discord_notify.post(webhook, _discord_text(summary, meta, duration), files=[summary_md, transcript_md])

        # 聞き直し用に圧縮して残す(2時間で約30MB)。元のWAV・PCMは消す
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", "32000", wav, os.path.join(d, "audio.m4a")], check=True)
        for p in [wav] + glob.glob(os.path.join(d, "seg_*.pcm")):
            os.remove(p)
        _update_meta(d, status="done", title=meta.get("title") or title, duration=round(duration))
        print("[seminar] done", flush=True)
    except Exception as e:
        _update_meta(d, status="error", error=str(e))
        discord_notify.post(webhook, f"セミナーの要約に失敗しました({os.path.basename(d)}): {e}")
        raise
    finally:
        cleanup_old_audio()


def ingest(path: str, title: str = "") -> str:
    """手持ちの音声・動画ファイルを処理する(afconvertが読める形式: m4a/mp3/wav/mp4等)。"""
    d = _new_dir()
    _update_meta(d, title=title.strip(), mode="file", status="processing", started=datetime.now().isoformat(timespec="seconds"), source=os.path.abspath(path))
    _convert_to_wav(path, os.path.join(d, "audio.wav"))
    process(d)
    return d


def cleanup_old_audio() -> None:
    limit = datetime.now() - timedelta(days=AUDIO_RETENTION_DAYS)
    for d, meta in recent(1000):
        if meta.get("status") in ("recording", "processing"):
            continue
        try:
            if datetime.fromisoformat(meta["started"]) > limit:
                continue
        except (KeyError, ValueError):
            continue
        for p in glob.glob(os.path.join(d, "audio.*")) + glob.glob(os.path.join(d, "seg_*.pcm")):
            os.remove(p)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "start":
        print(start(sys.argv[2], " ".join(sys.argv[3:])))
    elif cmd == "stop":
        print("stopped" if stop() else "not recording")
    elif cmd == "supervise":
        supervise(sys.argv[2])
    elif cmd == "ingest":
        print(ingest(sys.argv[2], " ".join(sys.argv[3:])))
    else:
        print(__doc__)

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
SEGMENT_SEC = 300  # rec.appがファイルを分ける間隔。録音中はこの単位で順に文字起こしを進めておく
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
            "--segment-sec", str(SEGMENT_SEC),
        ],
        check=True,
    )
    deadline = time.time() + 30
    while _rec_pid(d) is None and time.time() < deadline:
        time.sleep(0.5)
    pid = _rec_pid(d)
    while pid and _alive(pid):
        # 録音中に、書き終わった5分ファイルから順に文字起こししておく。質問候補を
        # すぐ出せるようにするためと、録音後の処理を短くするため
        for idx in _completed_segments(d):
            if not os.path.exists(_part_path(d, idx)):
                _transcribe_segment(d, idx)
        time.sleep(2)
    _update_meta(d, ended=datetime.now().isoformat(timespec="seconds"))
    process(d)


# ---------------------------------------------------------------------------
# 5分ファイル単位の文字起こし
# ---------------------------------------------------------------------------


def _segment_paths(d: str) -> list:
    return sorted(glob.glob(os.path.join(d, "seg_*.pcm")))


def _seg_index(path: str) -> int:
    return int(os.path.basename(path)[4:7])


def _completed_segments(d: str) -> list:
    """書き終わったファイル(=次のファイルが既にあるもの)の番号。最後の1つは書き込み中。"""
    return [_seg_index(p) for p in _segment_paths(d)[:-1]]


def _part_path(d: str, idx: int) -> str:
    return os.path.join(d, f"part_{idx:03d}.json")


def _run_transcribe(in_path: str, out_path: str, offset: float) -> list:
    tmp = out_path + ".tmp"
    subprocess.run([VENV_PY, TRANSCRIBE_SCRIPT, in_path, tmp, str(offset)], check=True)
    os.replace(tmp, out_path)  # 監視プロセスと質問候補の生成が同時に書いても、読み手が書きかけを掴まないように
    with open(out_path) as f:
        return json.load(f)["segments"]


def _transcribe_segment(d: str, idx: int) -> list:
    path = os.path.join(d, f"seg_{idx:03d}.pcm")
    return _run_transcribe(path, _part_path(d, idx), idx * SEGMENT_SEC)


def _transcript_so_far(d: str, include_current: bool) -> list:
    """録音済みの分の文字起こし。include_currentなら書き込み中のファイルもその時点までを読む。"""
    segments = []
    paths = _segment_paths(d)
    for i, path in enumerate(paths):
        idx = _seg_index(path)
        if os.path.exists(_part_path(d, idx)):
            with open(_part_path(d, idx)) as f:
                segments += json.load(f)["segments"]
        elif i < len(paths) - 1:
            segments += _transcribe_segment(d, idx)
        elif include_current:
            snapshot = os.path.join(d, "current_snapshot.pcm")
            with open(path, "rb") as src, open(snapshot, "wb") as dst:
                dst.write(src.read())
            segments += _run_transcribe(snapshot, os.path.join(d, "current_snapshot.json"), idx * SEGMENT_SEC)
            for p in (snapshot, os.path.join(d, "current_snapshot.json")):
                os.remove(p)
        else:
            segments += _transcribe_segment(d, idx)
    return segments


# ---------------------------------------------------------------------------
# 質疑応答で聞く質問の候補
# ---------------------------------------------------------------------------

_QUESTIONS_PROMPT = (
    "あなたは、セミナーに参加しているユーザーのアシスタントです。以下は、ここまでの講演を"
    "自動で文字起こししたものです(音声認識の聞き間違いを含むので文脈から補って解釈する)。"
    "このあとの質疑応答でユーザーが講師に聞くと有意義な質問を5つ考えてください。\n"
    "- 講演の中で既に説明されたことをそのまま聞き返す質問は避ける\n"
    "- 「さっきの〜の話で」のように講演のどこを受けた質問か分かるようにし、具体的にする\n"
    "- その場で口に出してそのまま言える、1〜2文の自然な話し言葉にする\n"
    "- 深掘り・実践での使い方・前提や限界の確認など、なるべく種類をばらけさせる\n"
    "以下の形式のMarkdownだけを返す(前置きや説明は書かない):\n"
    "1. 質問文\n   - ねらい: なぜこの質問が有意義か(短く)"
)


def suggest_questions(d: str) -> str:
    """ここまでの講演内容から質問候補を作り、questions.mdに追記して返す。"""
    from converse import _llm_chat

    segments = _transcript_so_far(d, include_current=True)
    if not segments:
        raise RuntimeError("まだ講演の言葉を聞き取れていません")
    text = "\n".join(f"[{_ts(s['start'])}] {s['text']}" for s in segments)
    questions = _llm_chat(_QUESTIONS_PROMPT, [{"role": "user", "content": text}], temperature=0.5, timeout=600, reasoning_effort="low")
    with open(os.path.join(d, "questions.md"), "a") as f:
        f.write(f"## {datetime.now().strftime('%H:%M')}時点(講演{_ts(segments[-1]['end'])}まで)\n{questions}\n\n")
    return questions


def load_questions(d: str) -> str:
    path = os.path.join(d, "questions.md")
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


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
    "## 時間ごとの流れ\n- [h:mm:ss] 話題(全体の目次として読める粒度に整理する。時刻は各部分の"
    "「流れ」に書かれた時刻をそのまま使い、新しい時刻を作ったり切りのいい値に丸めたりしない)\n\n"
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
    summary = _llm_chat(_FINAL_PROMPT, [{"role": "user", "content": user_content}], temperature=0.3, timeout=900, reasoning_effort="medium")
    return _snap_timestamps(summary, segments)


_TS_RE = re.compile(r"\[(\d+):(\d\d):(\d\d)\]")


def _snap_timestamps(summary: str, segments: list) -> str:
    """統合段階でLLMが時刻を「0:01:00, 0:02:00…」のように丸めて作り直す実例が
    あったため(2026-10-31)、要約中の時刻を文字起こしに実在する発話開始時刻の
    うち最も近いものに置き換える。録音を聞き直す時に頭出しできる時刻にするため。"""
    starts = sorted({int(s["start"]) for s in segments})
    if not starts:
        return summary

    def snap(m):
        sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        return f"[{_ts(min(starts, key=lambda t: abs(t - sec)))}]"

    return _TS_RE.sub(snap, summary)


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
        recorded = bool(_segment_paths(d))
        if recorded:
            wav = _concat_pcm(d)
        duration = _wav_duration(wav)
        if duration < MIN_DURATION_SEC:
            raise RuntimeError(f"録音が短すぎます({duration:.1f}秒)。マイクや画面収録の許可を確認してください")

        print(f"[seminar] transcribing {_ts(duration)}", flush=True)
        transcript_json = os.path.join(d, "transcript.json")
        if recorded:
            # 録音中に済ませた5分ファイル分は使い回し、残りだけ文字起こしする
            segments = _transcript_so_far(d, include_current=False)
            with open(transcript_json, "w") as f:
                json.dump({"duration": round(duration, 1), "segments": segments}, f, ensure_ascii=False)
        else:
            segments = _run_transcribe(wav, transcript_json, 0.0)
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
        for p in [wav] + _segment_paths(d) + glob.glob(os.path.join(d, "part_*.json")):
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

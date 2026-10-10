"""listen.py(反応)とtrigger.py(能動発話)を1プロセスに統合。

発話ロック(speak_lock)で同時再生を防ぎ、直前にRilin自身が言った
内容(last_spoken_text)と似ている確定結果は自己エコーとして捨てる。
タイミングではなく発言内容で判定するため、再生の長さやマイクの
拾いやすさに依存しない。

使い方:
    python assistant.py
"""
import difflib
import json
import os
import re
import subprocess
import threading
import time

import config
import converse
import pronunciation_dict
from converse import filler_phrase, generate, proactive_utterance, update_user_profile
from gate import can_speak_now, in_quiet_hours
from speak import SPEAKERS, play, synthesize
from tools import will_use_tools
from trigger import PROACTIVE_PROMPT

STT_APP = os.path.join(os.path.dirname(__file__), "stt.app")
LOG_PATH = os.path.join(os.path.dirname(__file__), "stt_live.log")
ECHO_SIMILARITY_THRESHOLD = 0.5

WAKE_WORDS = ("ねえ", "あのさ")  # 会話開始時にこれで始まる発話だけ本気の呼びかけとして扱う
CONVERSATION_SESSION_SEC = 60  # この秒数以内の会話継続中はウェイクワード不要


def strip_wake_word(text: str):
    for w in WAKE_WORDS:
        if text.startswith(w):
            return text[len(w):].lstrip("、, ")
    return None

CHECK_INTERVAL_SEC = 30 * 60  # 30分おきに判定
SILENCE_THRESHOLD_SEC = 45 * 60  # 直近45分会話が無ければ対象

speak_lock = threading.Lock()
last_spoken_text = ""
last_interaction_time = time.time()


def is_echo(text: str) -> bool:
    if not last_spoken_text:
        return False
    if text in last_spoken_text or last_spoken_text in text:
        return True
    ratio = difflib.SequenceMatcher(None, text, last_spoken_text).ratio()
    return ratio >= ECHO_SIMILARITY_THRESHOLD


# 独り言・環境音・短い感嘆詞(「あ」「しまった」等)には反応したくない。LLMに
# 「これは自分への発話か」を毎回判定させると呼び出しが1回増えて反応が遅くなる
# ため(過去に検討して速度面で見送った)、is_echo()と同じくLLMを使わない
# 正規表現での事前フィルタで弾く。発言全体がこれらの語だけの場合のみ弾き、
# 「雨」「子」のような短い内容語や、これらを含む文の一部は誤って弾かない。
_MUTTER_RE = re.compile(
    r"^(あ+ー*っ*|ああ+|あー+|えっ+|えー+|うわ+っ*|うわー+|わっ+|げっ+|お+っと|"
    r"うーん+|んー+|はぁ+|ちっ+|くっ+|しまった|やば+い?)[!!。、\s]*$"
)


def is_mutter(text: str) -> bool:
    return bool(_MUTTER_RE.match(text))


def speak_text(text: str) -> None:
    global last_spoken_text
    cfg = config.load()
    if not cfg["voice_enabled"]:
        return

    speaker_info = SPEAKERS[cfg["speaker"]]
    # generate()が会話内容に合わせて選んだスタイル(converse.last_reply_style)を
    # 優先する。話者にそのスタイルが無い場合や、proactive/fillerなど
    # タグ付けしていない発言ではNoneになるので、設定画面の固定styleに戻す。
    dynamic_style = converse.last_reply_style
    if dynamic_style in speaker_info["styles"]:
        style_name = dynamic_style
    else:
        style_name = cfg["style"] or next(iter(speaker_info["styles"]))
    style_id = speaker_info["styles"][style_name]

    with speak_lock:
        last_spoken_text = text
        wav = synthesize(text, speaker_info["uuid"], style_id)
        play(wav)


# stt.appが"No speech detected"を異常な頻度で吐き続ける不具合が2度実際に
# 発生し、どちらも手動でkill+再起動するまで何時間も気付かれなかった
# (マイクが聞こえているように見えて実際は無反応という分かりにくい症状)。
# 根本原因はまだ特定できていないため、せめて自動検知・自動復旧できるように
# しておく。この件数以上「final」を挟まずにエラーが連続したら再起動する。
STT_ERROR_BURST_THRESHOLD = 30


def _launch_stt() -> None:
    # openコマンドは既に起動中のstt.appがあるとそれを使い回し、新規プロセスを
    # 起動しない。古いインスタンスが何らかの理由でエラーループに陥っていても
    # 気付けず居座り続けるため(実際に約23時間ハングしたまま検知されなかった)、
    # 起動前に必ず既存プロセスを終了させ、確実に新しいプロセスへ切り替える。
    subprocess.run(["pkill", "-f", "stt.app/Contents/MacOS/stt"])
    time.sleep(0.5)
    open(LOG_PATH, "w").close()
    subprocess.Popen(["open", "-n", STT_APP, "--stdout", LOG_PATH])


LISTENING_CHECK_INTERVAL_SEC = 1.0  # 設定画面でのON/OFF切り替えをどれくらい早く反映するか


def reactive_loop() -> None:
    global last_interaction_time
    open(LOG_PATH, "a").close()  # stt未起動でもopen(path, "r")できるようにしておく
    stt_running = False
    consecutive_errors = 0
    last_listening_check = 0.0

    with open(LOG_PATH, "r") as f:
        while True:
            now = time.time()
            if now - last_listening_check >= LISTENING_CHECK_INTERVAL_SEC:
                last_listening_check = now
                listening_enabled = config.load()["listening_enabled"]
                if listening_enabled and not stt_running:
                    _launch_stt()
                    f.seek(0, os.SEEK_END)
                    stt_running = True
                    consecutive_errors = 0
                elif not listening_enabled and stt_running:
                    # 設定画面から無効化されたら、マイク認識プロセス自体を止める。
                    # 誤って録れた音声をログに残さないよう、認識結果→generate()への
                    # 受け渡しを止めるだけでなく、認識プロセスごと止める。
                    subprocess.run(["pkill", "-f", "stt.app/Contents/MacOS/stt"])
                    stt_running = False

            if not stt_running:
                time.sleep(0.5)
                continue

            line = f.readline()
            if not line:
                time.sleep(0.2)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "error":
                consecutive_errors += 1
                if consecutive_errors >= STT_ERROR_BURST_THRESHOLD:
                    print(f"[warn] stt error burst detected ({consecutive_errors}件), restarting stt.app")
                    _launch_stt()
                    consecutive_errors = 0
                    f.seek(0, os.SEEK_END)
                continue
            if event.get("type") == "final":
                consecutive_errors = 0
            else:
                continue
            text = event.get("text", "").strip()
            if not text or is_echo(text) or in_quiet_hours() or is_mutter(text):
                continue

            # ウェイクワード必須にすると取りこぼしが増えて反応が悪くなったため無効化。
            # in_session = (time.time() - last_interaction_time) < CONVERSATION_SESSION_SEC
            # if not in_session:
            #     text = strip_wake_word(text)
            #     if not text:
            #         continue

            try:
                if will_use_tools(text):
                    speak_text(filler_phrase())
                reply = generate(text)
                print(f"[user] {text}")
                print(f"[reply] {reply}")
                speak_text(reply)
            except Exception as e:
                print(f"[error] reactive: {e}")
            f.seek(0, os.SEEK_END)  # 発話中に溜まった分を読み捨てる
            last_interaction_time = time.time()


def proactive_loop() -> None:
    global last_interaction_time
    last_profile_update_time = 0.0
    while True:
        time.sleep(CHECK_INTERVAL_SEC)

        if not in_quiet_hours() and last_interaction_time > last_profile_update_time:
            try:
                update_user_profile()
            except Exception as e:
                print(f"[error] profile update: {e}")
            last_profile_update_time = time.time()

        if time.time() - last_interaction_time < SILENCE_THRESHOLD_SEC:
            continue
        if not can_speak_now():
            continue
        try:
            text = proactive_utterance(PROACTIVE_PROMPT)
            print(f"[proactive] {text}")
            speak_text(text)
        except Exception as e:
            print(f"[error] proactive: {e}")
        last_interaction_time = time.time()


def main() -> None:
    try:
        pronunciation_dict.apply()
    except Exception as e:
        print(f"[error] pronunciation_dict: {e}")
    threading.Thread(target=proactive_loop, daemon=True).start()
    reactive_loop()


if __name__ == "__main__":
    main()

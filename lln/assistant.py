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
from datetime import date, datetime

from janome.tokenizer import Tokenizer

import config
import converse
import discord_notify
import pronunciation_dict
from converse import filler_phrase, generate, proactive_utterance, task_ack_phrase, update_user_profile
from gate import can_speak_now, in_quiet_hours
from memory_search import update_interest_scores
from speak import SPEAKERS, play, synthesize
from tools import will_use_tools
from trigger import PROACTIVE_PROMPT

STT_APP = os.path.join(os.path.dirname(__file__), "stt.app")
LOG_PATH = os.path.join(os.path.dirname(__file__), "stt_live.log")
ECHO_SIMILARITY_THRESHOLD = 0.5
# stt.swiftのsilenceInterval(1.2秒)により、自分の発話末尾の確定(final)が
# 再生終了より遅れて届くことがある。その間に生じた確定結果は、内容一致
# (is_echo)の対象外になるほど短い断片でも自己エコーである可能性が高いため、
# 再生終了直後はこの秒数だけ内容を問わず確定結果を読み捨てる。
ECHO_GUARD_SEC = 1.8
# 短い断片(ECHO_FRAGMENT_MIN_LEN未満)はis_echo()の内容一致判定の対象外に
# なるため、ECHO_GUARD_SECだけでは直前の返答が長いほど取りこぼしやすい
# (実例:長い返答中に出た「フミくん」という呼び名の4文字の残響が、1.8秒を
# 過ぎてから確定して素通りした)。短い断片に限り、直前の返答の長さに応じて
# ガード時間を伸ばす。長い返答ほど再生時間も長く、STTの確定が遅れて届く
# 余地が増えるという経験則によるもの。
SHORT_FRAGMENT_GUARD_PER_CHAR_SEC = 0.02
SHORT_FRAGMENT_GUARD_MAX_SEC = 6.0

WAKE_WORDS = ("ねえ", "あのさ")  # 会話開始時にこれで始まる発話だけ本気の呼びかけとして扱う
CONVERSATION_SESSION_SEC = 60  # この秒数以内の会話継続中はウェイクワード不要

# ---------------------------------------------------------------------------
# Goose(AIエージェントCLI)へのタスク委譲
#
# 「〜しておいて」で終わる発話だけを対象にした狭いトリガーにする。LLMに
# 「これはタスク依頼か」を判定させる案もあったが、(1) 判定のためのLLM
# 呼び出しが毎ターン増えて反応が遅くなる、(2) ファイル操作やエージェント
# 実行のような実害のある処理を、誤判定で勝手に実行してしまうリスクの方が、
# トリガーを言い忘れて頼み直す程度のコストより高い、という理由で
# 正規表現による確定的な判定を選んだ。
# ---------------------------------------------------------------------------

GOOSE_BIN = "/opt/homebrew/bin/goose"
GOOSE_TASK_DIR = os.path.join(os.path.dirname(__file__), "goose_tasks")
# 動詞のテ形+「おいて」という文法パターン全般を拾う。「しておいて」限定
# (する動詞のみ)だと「調べておいて」「まとめておいて」のような一段動詞・
# 五段動詞のテ形を取りこぼすため、「し」は要求しない。文末の「ね」「よ」
# などの終助詞も素通りできるよう、句読点と合わせて末尾で許容する。
_GOOSE_TRIGGER_RE = re.compile(r"^(.+?)ておいて[。、！!？?ねよなさ\s]*$")


def extract_goose_task(text: str):
    """マッチした場合、Gooseに渡す自然な依頼文(「〜てください」化)を返す。
    正規表現の捕捉群は「ておいて」の直前まで(テ形の「て」を含まない途中の
    語幹)になるため、そのままでは「明日の天気を調べ」のように文が
    途切れる。「てください」を補って完全な依頼文に整える。"""
    m = _GOOSE_TRIGGER_RE.match(text)
    if not m:
        return None
    return m.group(1).strip() + "てください"


def _notify_goose_result(proc: subprocess.Popen, task_text: str, out_path: str) -> None:
    """バックグラウンドスレッドでgooseプロセスの終了を待ち、完了したら
    出力をDiscordへ投稿する(別スレッドなのでreactive_loopはブロックしない)。
    Webhook URL未設定の場合は何もしない。"""
    proc.wait()
    webhook_url = config.load().get("discord_webhook_url")
    if not webhook_url:
        return
    try:
        with open(out_path) as f:
            output = f.read()
    except Exception:
        output = "(出力の読み込みに失敗しました)"
    discord_notify.post(webhook_url, f"**依頼:** {task_text}\n```\n{output}\n```")


def dispatch_goose_task(task_text: str) -> None:
    """goose run -t "<task_text>" をバックグラウンドで起動する。会話ループを
    止めないよう、完了を待たずにすぐ返る(subprocess.Popen、.runではない)。
    結果はローカルのログファイルに残しつつ、完了時にDiscordへ投稿する
    (投稿は別スレッドでプロセス完了を待つため、こちらも会話をブロックしない)。"""
    os.makedirs(GOOSE_TASK_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(GOOSE_TASK_DIR, f"{timestamp}.log")
    with open(out_path, "w") as f:
        f.write(f"[task] {task_text}\n\n")
        f.flush()
        proc = subprocess.Popen(
            [GOOSE_BIN, "run", "-t", task_text],
            stdout=f,
            stderr=subprocess.STDOUT,
            cwd=GOOSE_TASK_DIR,
        )
    print(f"[goose] dispatched -> {out_path}: {task_text}")
    threading.Thread(target=_notify_goose_result, args=(proc, task_text, out_path), daemon=True).start()


def strip_wake_word(text: str):
    for w in WAKE_WORDS:
        if text.startswith(w):
            return text[len(w):].lstrip("、, ")
    return None

CHECK_INTERVAL_SEC = 30 * 60  # 30分おきに判定
LOG_JANITOR_HOUR = 4
SILENCE_THRESHOLD_SEC = 45 * 60  # 直近45分会話が無ければ対象

speak_lock = threading.Lock()
last_spoken_text = ""
last_interaction_time = time.time()
# speak_text()が再生を終えた時刻(reactive/proactive共通)。reactive_loop側は
# この値が進んだら、マイクが拾ったかもしれない自分の声の残響をis_echo()の
# 内容一致に頼らず確実に読み捨てる。proactive発話は別スレッドで再生されるため、
# reactive_loop自身の`speak_text()`呼び出し直後にだけ読み捨てていた従来の
# 仕組みでは、proactiveの自己エコーを取りこぼすことがあった。
last_speak_end_time = 0.0


ECHO_FRAGMENT_COVERAGE = 0.75  # 聞き取った内容のうち、これ以上の割合が直前の
                                # 発言の一部として説明できれば自己エコーとみなす
ECHO_FRAGMENT_MIN_LEN = 6  # 短すぎる一致での誤判定(偶然の単語一致)を避ける下限


def _echo_fragment_coverage(text: str, reference: str) -> float:
    """「聞き取った内容の何%が、直前に自分で言った内容の一部として説明できるか」
    を返す。ratio()は比較対象2つの長さの違いに弱く、長い返答の末尾だけを正確に
    拾った短い聞き取り結果が、長さの差だけで低スコアになり素通りすることが
    実際に確認された(例:長い返答の最後の一文だけを拾ったケース)。
    get_matching_blocks()で一致箇所を全て合算することで、句読点などで一致が
    分断されていても正しくカバー率を測れるようにする。"""
    if not text:
        return 0.0
    matcher = difflib.SequenceMatcher(None, text, reference)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(text)


def is_echo(text: str) -> bool:
    if not last_spoken_text:
        return False
    if text in last_spoken_text or last_spoken_text in text:
        return True
    if len(text) >= ECHO_FRAGMENT_MIN_LEN:
        coverage = _echo_fragment_coverage(text, last_spoken_text)
        if coverage >= ECHO_FRAGMENT_COVERAGE:
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


# 生活音・独り言から拾った名詞だけの断片(「切符」「背中」「スポーツ」「ます」等)に
# 意味不明な返答をする件への対策。_MUTTER_REと同じくLLMは使わず、品詞だけで
# 「文として成り立っているか」を判定する。動詞・形容詞・助詞・感動詞の
# いずれかがあれば文(または返事・挨拶)とみなす。
_tokenizer = Tokenizer()
_SENTENCE_POS = {"動詞", "形容詞", "助詞", "感動詞"}
_FRAGMENT_POS = {"名詞", "助動詞", "接頭詞", "副詞"}
# Rilinの発話直後は「何食べたい？」→「カレー」のような1語の返事があり得るので通す
FRAGMENT_REPLY_WINDOW_SEC = 60
_CALL_NAME_RE = re.compile(r"リリ|りり")


def is_fragment(text: str) -> bool:
    if _CALL_NAME_RE.search(text) or text.rstrip().endswith(("？", "?")):
        return False
    pos = [t.part_of_speech.split(",")[0] for t in _tokenizer.tokenize(text)]
    pos = [p for p in pos if p != "記号"]
    if any(p in _SENTENCE_POS for p in pos):
        return False
    # 「天気どう」(名詞+副詞)や「いやです」(名詞+助動詞)のように複数語なら
    # 文になり得るため、副詞・助動詞は単独の時だけ断片扱いにする
    if len(pos) >= 2 and ("副詞" in pos or "助動詞" in pos):
        return False
    return all(p in _FRAGMENT_POS for p in pos)


def speak_text(text: str) -> None:
    global last_spoken_text, last_speak_end_time
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
        last_speak_end_time = time.time()


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
    seen_speak_end_time = last_speak_end_time
    echo_guard_until = 0.0
    short_fragment_guard_until = 0.0

    with open(LOG_PATH, "r") as f:
        while True:
            if last_speak_end_time != seen_speak_end_time:
                # reactive/proactiveどちらかが発話を終えた直後。マイクが拾った
                # かもしれない自分の声の残響を、内容一致の判定に頼らず確実に
                # 読み捨てる。再生終了直後は余韻が録れきっていないこともある
                # ため、少し待ってからシークする。
                seen_speak_end_time = last_speak_end_time
                echo_guard_until = last_speak_end_time + ECHO_GUARD_SEC
                short_fragment_guard_sec = min(
                    SHORT_FRAGMENT_GUARD_MAX_SEC,
                    ECHO_GUARD_SEC + SHORT_FRAGMENT_GUARD_PER_CHAR_SEC * len(last_spoken_text),
                )
                short_fragment_guard_until = last_speak_end_time + short_fragment_guard_sec
                time.sleep(0.3)
                f.seek(0, os.SEEK_END)
                continue

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
            if time.time() < echo_guard_until:
                # is_echo()の文字列一致はECHO_FRAGMENT_MIN_LEN未満の短い語
                # (「ふみくん」のような呼び名の断片など)を判定対象外にしている
                # ため、自分の発話の残響がたまたま短い単語として確定した場合
                # すり抜けてしまう実例を確認した。再生終了直後の数秒間は
                # 内容を問わず確定結果ごと捨てることで、その抜け穴を塞ぐ。
                continue
            if len(text) < ECHO_FRAGMENT_MIN_LEN and time.time() < short_fragment_guard_until:
                # 上のecho_guard_untilを過ぎても、短い断片に限っては直前の
                # 返答の長さに応じて伸びるこちらのガードでなお捨てる
                # (SHORT_FRAGMENT_GUARD_PER_CHAR_SEC参照)。is_echo()の
                # 内容一致が効かない短さの発話にだけ適用するため、通常の
                # 会話応答がこれで遅れることはない。
                continue
            if not text or is_echo(text) or in_quiet_hours() or is_mutter(text):
                continue
            if (
                time.time() - last_speak_end_time > FRAGMENT_REPLY_WINDOW_SEC
                and is_fragment(text)
            ):
                print(f"[fragment] ignored: {text}")
                continue

            # ウェイクワード必須にすると取りこぼしが増えて反応が悪くなったため無効化。
            # in_session = (time.time() - last_interaction_time) < CONVERSATION_SESSION_SEC
            # if not in_session:
            #     text = strip_wake_word(text)
            #     if not text:
            #         continue

            goose_task = extract_goose_task(text)
            try:
                if goose_task:
                    # 会話(generate())の文脈には乗せない独立した経路。
                    # 了解の一言だけ即座に返し、実行はバックグラウンドの
                    # goose run に委ねて会話ループをブロックしない。
                    print(f"[user] {text}")
                    speak_text(task_ack_phrase())
                    dispatch_goose_task(goose_task)
                else:
                    if will_use_tools(text):
                        speak_text(filler_phrase())
                    reply = generate(text)
                    print(f"[user] {text}")
                    print(f"[reply] {reply}")
                    speak_text(reply)
            except Exception as e:
                print(f"[error] reactive: {e}")
            # 発話中に溜まった分の読み捨ては、ループ先頭のlast_speak_end_time
            # チェックが次の周回で自動的に行う(proactiveと共通の仕組み)。
            last_interaction_time = time.time()


def proactive_loop() -> None:
    global last_interaction_time
    last_profile_update_time = 0.0
    last_janitor_date = None
    while True:
        time.sleep(CHECK_INTERVAL_SEC)

        # 会話の無い静かな時間帯(深夜)に1日1回、口癖化したログを退避する
        today = date.today()
        if in_quiet_hours() and datetime.now().hour >= LOG_JANITOR_HOUR and last_janitor_date != today:
            try:
                n = converse.clean_logs()
                print(f"[janitor] quarantined {n} entries")
            except Exception as e:
                print(f"[error] log janitor: {e}")
            last_janitor_date = today

        if not in_quiet_hours() and last_interaction_time > last_profile_update_time:
            try:
                update_user_profile()
            except Exception as e:
                print(f"[error] profile update: {e}")
            try:
                update_interest_scores()
            except Exception as e:
                print(f"[error] interest scoring: {e}")
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


# ログイン直後はCOEIROINKアプリ側の起動がassistant.py(launchd, RunAtLoad)
# より遅れることがあり、その場合apply()が接続エラーで失敗して辞書が
# 一度も反映されないまま1日が過ぎる不具合を実際に確認した(例:「暇」が
# 「いとま」と読まれ続けた)。起動直後の数十秒はCOEIROINK起動待ちとして
# リトライする。
PRONUNCIATION_DICT_RETRY_COUNT = 6
PRONUNCIATION_DICT_RETRY_INTERVAL_SEC = 10


def _apply_pronunciation_dict_with_retry() -> None:
    for attempt in range(PRONUNCIATION_DICT_RETRY_COUNT):
        try:
            pronunciation_dict.apply()
            return
        except Exception as e:
            if attempt == PRONUNCIATION_DICT_RETRY_COUNT - 1:
                print(f"[error] pronunciation_dict: {e}")
            else:
                time.sleep(PRONUNCIATION_DICT_RETRY_INTERVAL_SEC)


def main() -> None:
    # 同期的に待つと、COEIROINK起動待ちの間マイク認識(reactive_loop)の
    # 開始自体が遅れてしまう(最悪ケースで最大60秒)。辞書登録の完了を
    # 待たずに会話を始められるよう、別スレッドで並行して走らせる。
    threading.Thread(target=_apply_pronunciation_dict_with_retry, daemon=True).start()
    threading.Thread(target=proactive_loop, daemon=True).start()
    reactive_loop()


if __name__ == "__main__":
    main()

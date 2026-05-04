"""TTS synthesis: AivisSpeech / Voicevox (local + cloud)."""
import base64
import re
import struct
import time
from typing import Optional

import requests

TTS_QUEST_API = "https://api.tts.quest/v3/voicevox/synthesis"
LOCAL_VOICEVOX_URL = "http://localhost:50021"
AIVIS_URL = "http://localhost:10101"
NOAH_SPEAKER_ID = 888753761  # まお ふつー

_NOAH_MOOD_SPEAKER = {
    "happy": 888753762, "joy": 888753762,
    "calm": 888753763, "serious": 888753763, "think": 888753763,
    "tease": 888753764, "irony": 888753764,
    "sad": 888753765, "melancholy": 888753765,
}

_MOOD_STYLE_KEYWORDS = {
    "happy":   ["あまあま", "たのしい", "喜び", "元気", "わーい", "うきうき"],
    "angry":   ["ツンツン", "ツンギレ", "おこ", "怒り", "不機嫌", "つよつよ"],
    "sad":     ["なみだめ", "びえーん", "かなしい", "悲しみ", "かなしみ", "絶望"],
    "whisper": ["ささやき", "ヒソヒソ", "内緒話"],
    "tired":   ["ヘロヘロ", "へろへろ", "ヘロヘロ", "よわよわ"],
    "calm":    ["おちつき", "のんびり", "しっとり", "低血圧"],
    "sexy":    ["セクシー", "けだるげ"],
}


def _noah_speaker_from_mood(mood, default_id: int) -> int:
    if not mood:
        return default_id
    return _NOAH_MOOD_SPEAKER.get(mood.lower(), default_id)


def _speaker_from_mood(mood, styles_dict: dict, default_id: int) -> int:
    if not mood:
        return default_id
    keywords = _MOOD_STYLE_KEYWORDS.get(mood.lower(), [])
    for kw in keywords:
        for style_name, sid in styles_dict.items():
            if kw in style_name:
                return sid
    return default_id


def split_text_for_tts(text: str, max_len: int = 200) -> list:
    if len(text) <= max_len:
        return [text]
    chunks = []
    current = ""
    delimiters = ["。", "！", "？", "!", "?", "、", "\n"]
    i = 0
    while i < len(text):
        char = text[i]
        current += char
        if char in delimiters and len(current) >= 30:
            if len(current) <= max_len:
                chunks.append(current.strip())
                current = ""
        elif len(current) >= max_len:
            last_delim = -1
            for d in delimiters:
                pos = current.rfind(d)
                if pos > last_delim:
                    last_delim = pos
            if last_delim > 30:
                chunks.append(current[:last_delim + 1].strip())
                current = current[last_delim + 1:]
            else:
                chunks.append(current.strip())
                current = ""
        i += 1
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]


def strip_urls_for_tts(text: str) -> str:
    text = re.sub(r"https?://[^\s]+", "", text)
    text = re.sub(r"詳しくはこちら→?\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def synthesize_voice(text: str, speaker_id: int, api_key: str = "", timeout: int = 30) -> tuple:
    try:
        params = {"text": text, "speaker": speaker_id}
        if api_key:
            params["key"] = api_key
        r = requests.get(TTS_QUEST_API, params=params, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return None, f"API returned success=false: {data}"
        if "mp3Base64" in data:
            return base64.b64decode(data["mp3Base64"]), None
        status_url = data.get("audioStatusUrl")
        mp3_url = data.get("mp3DownloadUrl")
        if status_url and mp3_url:
            for _ in range(20):
                status_r = requests.get(status_url, timeout=10)
                status_data = status_r.json()
                if status_data.get("isAudioReady"):
                    mp3_r = requests.get(mp3_url, timeout=timeout)
                    mp3_r.raise_for_status()
                    return mp3_r.content, None
                if status_data.get("isAudioError"):
                    return None, f"Audio generation error: {status_data}"
                time.sleep(1)
            return None, f"Timeout after 20s polling (last status: {status_data})"
        return None, "No audioStatusUrl or mp3DownloadUrl in response"
    except Exception as e:
        return None, f"Exception: {e}"


def synthesize_voice_full(text: str, speaker_id: int, api_key: str = "", timeout: int = 30, max_retries: int = 2) -> tuple:
    chunks = split_text_for_tts(text, max_len=200)
    if not chunks:
        return None, "No text to synthesize"
    audio_parts = []
    for i, chunk in enumerate(chunks):
        if i > 0:
            time.sleep(0.5)
        audio_data = None
        last_error = None
        for attempt in range(max_retries + 1):
            if attempt > 0:
                time.sleep(1.0)
            audio_data, error = synthesize_voice(chunk, speaker_id, api_key, timeout)
            if audio_data:
                break
            last_error = error
        if not audio_data:
            return None, f"Chunk {i+1}/{len(chunks)} failed after {max_retries+1} attempts: {last_error}"
        audio_parts.append(audio_data)
    if not audio_parts:
        return None, "No audio generated"
    return b"".join(audio_parts), None


def check_local_voicevox(timeout: int = 2) -> bool:
    try:
        r = requests.get(f"{LOCAL_VOICEVOX_URL}/version", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def synthesize_voice_local(text: str, speaker_id: int, timeout: int = 60) -> tuple:
    base = AIVIS_URL if speaker_id >= 800_000_000 else LOCAL_VOICEVOX_URL
    try:
        query_r = requests.post(
            f"{base}/audio_query",
            params={"text": text, "speaker": speaker_id},
            timeout=timeout,
        )
        query_r.raise_for_status()
        synth_r = requests.post(
            f"{base}/synthesis",
            params={"speaker": speaker_id},
            json=query_r.json(),
            timeout=timeout,
        )
        synth_r.raise_for_status()
        return synth_r.content, None
    except requests.exceptions.ConnectionError:
        return None, "ローカルVOICEVOXに接続できません。VOICEVOXを起動してください。"
    except Exception as e:
        return None, f"Exception: {e}"


def synthesize_voice_local_full(text: str, speaker_id: int, timeout: int = 60) -> tuple:
    if not text.strip():
        return None, "No text to synthesize"
    max_len = 1000
    if len(text) <= max_len:
        return synthesize_voice_local(text, speaker_id, timeout)
    chunks = split_text_for_tts(text, max_len=max_len)
    audio_parts = []
    for i, chunk in enumerate(chunks):
        audio_data, error = synthesize_voice_local(chunk, speaker_id, timeout)
        if not audio_data:
            return None, f"Chunk {i+1}/{len(chunks)} failed: {error}"
        audio_parts.append(audio_data)
    if not audio_parts:
        return None, "No audio generated"
    return concat_wav_data(audio_parts), None


def resample_wav_to(wav_data: bytes, target_sr: int = 44100) -> bytes:
    import audioop
    if len(wav_data) < 44:
        return wav_data
    ch = struct.unpack('<H', wav_data[22:24])[0]
    sr = struct.unpack('<I', wav_data[24:28])[0]
    bps = struct.unpack('<H', wav_data[34:36])[0]
    if sr == target_sr:
        return wav_data
    width = bps // 8
    pcm = wav_data[44:]
    resampled, _ = audioop.ratecv(pcm, width, ch, sr, target_sr, None)
    data_size = len(resampled)
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', data_size + 36, b'WAVE', b'fmt ', 16, 1,
        ch, target_sr, target_sr * ch * width, ch * width, bps,
        b'data', data_size,
    )
    return header + resampled


def concat_wav_data(wav_parts: list) -> bytes:
    import audioop
    if len(wav_parts) == 1:
        return wav_parts[0]
    combined_data = b""
    sample_rate = num_channels = bits_per_sample = 0
    for i, wav in enumerate(wav_parts):
        if len(wav) < 44:
            continue
        ch = struct.unpack('<H', wav[22:24])[0]
        sr = struct.unpack('<I', wav[24:28])[0]
        bps = struct.unpack('<H', wav[34:36])[0]
        pcm = wav[44:]
        if i == 0:
            num_channels, sample_rate, bits_per_sample = ch, sr, bps
            combined_data += pcm
        else:
            if ch != num_channels:
                if ch == 2 and num_channels == 1:
                    pcm = audioop.tomono(pcm, bps // 8, 0.5, 0.5)
                elif ch == 1 and num_channels == 2:
                    pcm = audioop.tostereo(pcm, bps // 8, 1, 1)
            if sr != sample_rate:
                pcm, _ = audioop.ratecv(pcm, bps // 8, num_channels, sr, sample_rate, None)
            combined_data += pcm
    data_size = len(combined_data)
    byte_rate = sample_rate * num_channels * bits_per_sample // 8
    block_align = num_channels * bits_per_sample // 8
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', data_size + 36, b'WAVE', b'fmt ', 16, 1,
        num_channels, sample_rate, byte_rate, block_align, bits_per_sample,
        b'data', data_size,
    )
    return header + combined_data


def get_voicevox_user_dict() -> dict:
    try:
        r = requests.get(f"{LOCAL_VOICEVOX_URL}/user_dict", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def add_voicevox_dict_word(surface: str, pronunciation: str, accent_type: int = 0, priority: int = 5) -> Optional[str]:
    try:
        r = requests.post(
            f"{LOCAL_VOICEVOX_URL}/user_dict_word",
            params={"surface": surface, "pronunciation": pronunciation, "accent_type": accent_type, "priority": priority},
            timeout=5,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def delete_voicevox_dict_word(word_uuid: str) -> bool:
    try:
        r = requests.delete(f"{LOCAL_VOICEVOX_URL}/user_dict_word/{word_uuid}", timeout=5)
        return r.status_code == 204
    except Exception:
        return False

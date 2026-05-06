"""LM Studio / Noah / Hermes chat helpers + web extraction."""
import os
import re
import subprocess
import threading
from pathlib import Path

import requests
import trafilatura

# LM Studio への同時リクエストを1本に制限するセマフォ。
# note生成（ユーザー操作）はセマフォを取得してから _priority_request フラグを立て、
# バックグラウンドタスク（soul更新・自律会話）は _priority_request が立っていたら即スキップする。
_lmstudio_sem = threading.Semaphore(1)
_priority_request = threading.Event()  # Setされている間はバックグラウンドがスキップ

EMBEDDING_PREFIXES = ("text-embedding-", "embedding-", "nomic-embed-")
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)
NOAH_GATEWAY_URL = "http://127.0.0.1:18789/v1"
NOAH_GATEWAY_TOKEN = "6895f0f1b82148769d5191c143103f275622105e12f4acd2b18476c787429658"

_OPENCLAW_WORKSPACE = Path.home() / ".openclaw" / "workspace"
_NOAH_MEMORY_FILES = ["IDENTITY.md", "RELATIONSHIP.md", "KNOWLEDGE.md", "OBSERVATIONS.md"]
_SOUL_SECTIONS = [
    "STEP 1",
    "Affinity × Mood Behavior",
    "Agreement Behavior",
    "Detachment Quality",
    "Self-Referential Topics",
    "Conversation Style",
    "Silence Rules",
]


def is_chat_model(model_id: str) -> bool:
    lower = model_id.lower()
    return not any(lower.startswith(p) for p in EMBEDDING_PREFIXES)


def lmstudio_models(base_url: str, timeout: int = 3):
    r = requests.get(base_url.rstrip("/") + "/models", timeout=timeout)
    r.raise_for_status()
    all_models = [m["id"] for m in r.json().get("data", [])]
    return [m for m in all_models if is_chat_model(m)]


def call_lmstudio_chat_messages(base_url, model, messages, temperature, max_tokens, timeout,
                                *, background: bool = False):
    """LM Studio にチャットリクエストを送る。
    background=True のタスクは priority_request 中はスキップ（TimeoutError）し、
    それ以外はセマフォで直列化して順番に処理する。
    """
    if background and _priority_request.is_set():
        raise TimeoutError("priority request in progress, skipping background task")
    acquired = _lmstudio_sem.acquire(timeout=30)
    if not acquired:
        raise TimeoutError("LM Studio semaphore timeout")
    try:
        if background and _priority_request.is_set():
            raise TimeoutError("priority request in progress, skipping background task")
        endpoint = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        r = requests.post(endpoint, json=payload, timeout=timeout)
        if not r.ok:
            raise requests.HTTPError(f"{r.status_code} {r.reason}: {r.text[:300]}", response=r)
        msg = r.json()["choices"][0]["message"]
        # Qwen3等のThinkingモデルはcontentが空でreasoning_contentに本文が入る
        return msg.get("content") or msg.get("reasoning_content") or ""
    finally:
        _lmstudio_sem.release()


def _extract_soul_sections(text: str) -> str:
    sections = re.split(r"\n(?=## )", text)
    result = []
    for section in sections:
        for target in _SOUL_SECTIONS:
            if re.match(rf"## .*{re.escape(target)}", section):
                result.append(section.strip())
                break
    return "\n\n".join(result)


def _load_noah_workspace_memory() -> str:
    parts = []
    for fname in _NOAH_MEMORY_FILES:
        fpath = _OPENCLAW_WORKSPACE / fname
        if fpath.exists():
            content = fpath.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"### {fname}\n{content}")
    soul_path = _OPENCLAW_WORKSPACE / "SOUL.md"
    if soul_path.exists():
        soul_text = soul_path.read_text(encoding="utf-8")
        soul_extracted = _extract_soul_sections(soul_text)
        if soul_extracted:
            parts.append(f"### SOUL.md（行動ルール）\n{soul_extracted}")
    return "\n\n".join(parts)


def call_noah_chat(messages: list, timeout: int = 60) -> tuple:
    memory = _load_noah_workspace_memory()
    if memory:
        if messages and messages[0]["role"] == "system":
            messages = [{"role": "system", "content": messages[0]["content"] + f"\n\n---\n## Noahのワークスペース記憶\n{memory}"}] + messages[1:]
        else:
            messages = [{"role": "system", "content": f"## Noahのワークスペース記憶\n{memory}"}] + messages
    endpoint = NOAH_GATEWAY_URL.rstrip("/") + "/chat/completions"
    payload = {"model": "openclaw:default", "messages": messages, "stream": False, "user": "lmstudio-app"}
    r = requests.post(endpoint, json=payload, timeout=timeout,
                      headers={"Authorization": f"Bearer {NOAH_GATEWAY_TOKEN}"})
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    m = re.match(r"^\[MOOD:([^\]]+)\]\s*", raw)
    mood = m.group(1).lower() if m else None
    text = raw[m.end():] if m else raw
    return text, mood


def call_hermes_agent(prompt: str, timeout: int = 300) -> str:
    env = os.environ.copy()
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
    result = subprocess.run(
        ["hermes", "-z", prompt],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        raise RuntimeError(result.stderr.strip() or "HermesAgent returned empty response")
    return output


def call_hermes_agent_chat(messages: list, profile: str = "lmstudio-char", timeout: int = 300,
                           include_mood: bool = False) -> tuple:
    turns = [m for m in messages if m["role"] in ("user", "assistant")]
    recent = turns[-5:]
    parts = []
    for m in recent:
        if m["role"] == "user":
            parts.append(f"User: {m['content']}")
        else:
            parts.append(f"Assistant: {m['content']}")

    hist = "\n".join(parts[:-1]) if len(parts) > 1 else ""
    last = parts[-1].removeprefix("User: ") if parts else ""

    prompt_parts = []
    if hist:
        prompt_parts.append(f"【直近の会話】\n{hist}")
    if last:
        prompt_parts.append(f"【あなたへの発言】\n{last}")
    suffix = "短く（1〜3文）日本語で返答してください。"
    if include_mood:
        suffix += " 返答の冒頭に必ず [MOOD:xxx] を付けること（xxx: normal/happy/angry/sad/whisper/tired/calm/sexy）。"
    prompt_parts.append(suffix)
    full_prompt = "\n\n".join(prompt_parts)

    def _run_hermes(prof: str) -> str:
        e = os.environ.copy()
        e["PATH"] = os.path.expanduser("~/.local/bin") + ":" + e.get("PATH", "")
        e["HERMES_HOME"] = os.path.expanduser(f"~/.hermes/profiles/{prof}")
        try:
            r = subprocess.run(["hermes", "-z", full_prompt], capture_output=True, text=True, timeout=timeout, env=e)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"HermesAgent timeout ({timeout}s)")
        except FileNotFoundError:
            raise RuntimeError("hermes コマンドが見つかりません")
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError(r.stderr.strip()[:200] or "empty response")
        return r.stdout.strip()

    try:
        output = _run_hermes(profile)
    except RuntimeError:
        if profile != "lmstudio-char":
            output = _run_hermes("lmstudio-char")
        else:
            raise

    m_mood = re.match(r"^\[MOOD:([^\]]+)\]\s*", output)
    mood = m_mood.group(1).lower() if m_mood else None
    return normalize_model_output(output), mood


def call_char_chat(char_info: dict, messages: list, base_url: str, model: str,
                   temperature: float, max_tokens: int, timeout: int = 180) -> tuple:
    if char_info.get("is_noah"):
        return call_noah_chat(messages, timeout=timeout)
    if char_info.get("is_hermes_agent"):
        profile = char_info.get("hermes_profile", "lmstudio-char")
        return call_hermes_agent_chat(messages, profile=profile, timeout=timeout)
    raw = call_lmstudio_chat_messages(base_url, model, messages, temperature, max_tokens, timeout)
    m = re.match(r"^\[MOOD:([^\]]+)\]\s*", raw)
    mood = m.group(1).lower() if m else None
    text = raw[m.end():] if m else raw
    return text, mood


def fetch_html(url: str, timeout: int = 20) -> str:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": DEFAULT_UA})
    r.raise_for_status()
    return r.text


def extract_main_text(html: str) -> str:
    text = trafilatura.extract(
        html, output_format="txt", include_comments=False,
        include_tables=True, favor_precision=True,
    )
    return (text or "").strip()


def build_summary_prompt(url: str, text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        clipped = text
    else:
        head = text[: int(max_chars * 0.7)]
        tail = text[-int(max_chars * 0.3):]
        clipped = head + "\n\n...(中略)...\n\n" + tail
    return f"""次のWebページ本文を要約してください。

URL: {url}

本文:
\"\"\"\n{clipped}\n\"\"\"
"""


_SYSTEM_PROMPT_LEAK_PATTERNS = re.compile(
    r"^[-・]?\s*("
    r"一人称は|相手は「|自分のことを「|ユーザーへの|会話の相手は|"
    r"他のキャラの一人称|絶対に使わない|直前の発言から|ニュース記事のタイトル"
    r")",
)
_HEADING_LEAK_PATTERNS = re.compile(
    r"^【(会話の状況|ルール|絶対厳守|呼び名ルール|反応のルール|一緒にいる相手)】"
)


def normalize_model_output(text: str) -> str:
    if not text:
        return text
    text = text.replace("<br/>", "\n").replace("<br>", "\n").replace("&nbsp;", " ")
    text = re.sub(r"\[MOOD:[^\]]+\]\s*", "", text)
    # （話題提供）がLLMにechoされた場合は除去
    text = re.sub(r"[（(]話題提供[）)]\s*", "", text)
    text = re.sub(r"[（(]※[^（(）)]*[）)]?", "", text)
    text = re.sub(r"\s*[（(][^（(]{0,60}(?:文以内|注釈|日本語のみ|英語)[^）)]{0,40}[）)]?\s*", " ", text)
    lines = text.split("\n")
    lines = [l for l in lines if not re.match(r"^(\**)?\s*(Note|注|補足|※補足)\s*[:：]", l)]
    # システムプロンプトの指示文がleak した行を除去
    lines = [l for l in lines if not _SYSTEM_PROMPT_LEAK_PATTERNS.match(l.strip())]
    lines = [l for l in lines if not _HEADING_LEAK_PATTERNS.match(l.strip())]
    text = "\n".join(lines).strip()
    label_pattern = re.compile(r"^[^\s:：]{1,20}[:：]\s*")
    labeled_lines = [l for l in text.split("\n") if l.strip() and label_pattern.match(l)]
    if len(labeled_lines) >= 2:
        unlabeled = [l for l in text.split("\n") if l.strip() and not label_pattern.match(l)]
        if unlabeled:
            text = "\n".join(unlabeled).strip()
        else:
            last = labeled_lines[-1]
            text = label_pattern.sub("", last).strip()
    return text

import argparse
import requests
import trafilatura
import json

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)

def fetch_html(url: str, timeout: int = 20) -> str:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": DEFAULT_UA})
    r.raise_for_status()
    return r.text

def extract_main_text(html: str) -> str:
    text = trafilatura.extract(
        html,
        output_format="txt",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    return (text or "").strip()

def call_lmstudio_chat(base_url: str, model: str, prompt: str,
                       temperature: float = 0.2, max_tokens: int = 800,
                       timeout: int = 180) -> str:
    endpoint = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "あなたはプロの要約者です。日本語で簡潔に要約し、重要ポイントと注意点を整理してください。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    r = requests.post(endpoint, json=payload, timeout=timeout)
    if r.status_code >= 400:
        print("=== LM Studio 400 details ===")
        print("URL:", endpoint)
        print("Status:", r.status_code)
        try:
            print(json.dumps(r.json(), ensure_ascii=False, indent=2))
        except Exception:
            print(r.text)
        raise SystemExit("LM Studio returned an error (see details above).")

    data = r.json()
    return data["choices"][0]["message"]["content"]

def build_prompt(url: str, text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        clipped = text
    else:
        head = text[: int(max_chars * 0.7)]
        tail = text[-int(max_chars * 0.3):]
        clipped = head + "\n\n...(中略)...\n\n" + tail

    return f"""次のWebページ本文を要約してください。

制約:
- 重要ポイントを箇条書き（5〜10個）
- 数値・固有名詞・結論は落とさない
- 可能なら「意思決定の注意点」も1〜3個

URL: {url}

本文:
\"\"\"\n{clipped}\n\"\"\"
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", help="要約したいURL")
    ap.add_argument("--base-url", default="http://localhost:1234/v1", help="LM Studio base URL")
    ap.add_argument("--model", default="openai/gpt-oss-20b", help="モデルID（/v1/models で確認）")
    ap.add_argument("--timeout", type=int, default=20, help="URL取得タイムアウト（秒）")
    ap.add_argument("--max-chars", type=int, default=12000, help="本文の最大文字数（長文対策）")
    ap.add_argument("--max-tokens", type=int, default=800, help="生成トークン上限")
    ap.add_argument("--temperature", type=float, default=0.2)
    args = ap.parse_args()

    html = fetch_html(args.url, timeout=args.timeout)
    text = extract_main_text(html)

    if not text:
        raise SystemExit(
            "本文抽出に失敗しました（JS描画/ブロック/本文なしの可能性）。"
            "この場合は Playwright 版に切り替えると解決しやすいです。"
        )

    prompt = build_prompt(args.url, text, args.max_chars)
    summary = call_lmstudio_chat(
        base_url=args.base_url,
        model=args.model,
        prompt=prompt,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )
    print(summary)

if __name__ == "__main__":
    main()

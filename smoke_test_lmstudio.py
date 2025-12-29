import requests

BASE_URL = "http://localhost:1234/v1"
MODEL = "openai/gpt-oss-20b"

r = requests.post(
    f"{BASE_URL}/chat/completions",
    json={
        "model": MODEL,
        "messages": [{"role": "user", "content": "こんにちは。1行で自己紹介して"}],
        "temperature": 0.2,
        "max_tokens": 80,
    },
    timeout=120,
)

r.raise_for_status()
print(r.json()["choices"][0]["message"]["content"])
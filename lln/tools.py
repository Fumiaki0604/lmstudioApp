"""LLMに投げる前に直接叩く軽量ツール(天気など)。

キーワードで質問の種類を判定し、該当すればAPIを直接叩いて結果を
文字列で返す。LLMのラウンドトリップを増やさないための仕組み。
"""
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET

import requests

WEATHER_KEYWORDS = ("天気", "気温", "降水", "雨", "雪")
NEWS_KEYWORDS = ("ニュース", "最近の話題", "今日の話題", "世の中")
TWITTER_KEYWORDS = ("Twitter", "ツイッター", "X(旧Twitter)", "ツイート", "つぶやき", "SNS")

NEWS_FEEDS = {
    "Yahoo主要": "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
    "Yahoo経済": "https://news.yahoo.co.jp/rss/categories/business.xml",
    "WIRED": "https://wired.jp/feed/rss",
}

LLN_DIR = os.path.dirname(os.path.abspath(__file__))
VENV311_PYTHON = os.path.join(LLN_DIR, ".venv311", "bin", "python3")


def will_use_tools(text: str) -> bool:
    """generate()内でweather/news/twitter_contextのどれかが発火し、
    レスポンスが遅れそうかどうかを事前に判定する(実際の取得はしない)。"""
    return (
        any(k in text for k in WEATHER_KEYWORDS)
        or any(k in text for k in NEWS_KEYWORDS)
        or any(k in text for k in TWITTER_KEYWORDS)
    )
TWITTER_SCRIPT = os.path.join(LLN_DIR, "twitter_context.py")

# WMO weather_code -> 日本語の簡易説明
WEATHER_CODE_JA = {
    0: "快晴", 1: "晴れ", 2: "薄曇り", 3: "曇り",
    45: "霧", 48: "霧氷",
    51: "小雨", 53: "雨", 55: "強い雨",
    61: "小雨", 63: "雨", 65: "強い雨",
    71: "小雪", 73: "雪", 75: "大雪",
    80: "にわか雨", 81: "にわか雨", 82: "激しいにわか雨",
    95: "雷雨",
}

_LOCATION_RE = re.compile(r"(.+?)の(?:今日の)?(?:天気|気温|降水確率)")
_STRIP_WORDS = ("今日", "明日", "今", "現在", "って", "は")


def extract_location(text: str) -> str:
    m = _LOCATION_RE.search(text)
    if not m:
        return ""
    place = m.group(1)
    for w in _STRIP_WORDS:
        place = place.replace(w, "")
    return place.strip()


def geocode(place: str):
    if not place:
        return None
    # 「目黒区中目黒」のような複合地名は、先頭から削って通る単位まで縮める
    for i in range(len(place)):
        candidate = place[i:]
        res = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": candidate, "language": "ja", "count": 1, "country_code": "JP"},
            timeout=5,
        )
        res.raise_for_status()
        results = res.json().get("results")
        if results:
            r = results[0]
            return r["latitude"], r["longitude"], r["name"]
    return None


def get_weather(lat: float, lon: float) -> dict:
    res = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,precipitation,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "Asia/Tokyo",
            "forecast_days": 1,
        },
        timeout=5,
    )
    res.raise_for_status()
    return res.json()


def weather_context(text: str, default_location: str = "") -> str:
    if not any(k in text for k in WEATHER_KEYWORDS):
        return ""

    place = extract_location(text) or default_location
    geo = geocode(place)
    if not geo:
        return ""
    lat, lon, resolved_name = geo

    try:
        data = get_weather(lat, lon)
    except Exception:
        return ""

    current = data["current"]
    daily = data["daily"]
    condition = WEATHER_CODE_JA.get(current["weather_code"], "不明")

    return (
        f"【{resolved_name}の現在の天気】{condition}、気温{current['temperature_2m']}°C、"
        f"本日の最高{daily['temperature_2m_max'][0]}°C/最低{daily['temperature_2m_min'][0]}°C、"
        f"降水確率{daily['precipitation_probability_max'][0]}%"
    )


def fetch_rss_headlines(url: str, max_items: int = 4) -> list:
    try:
        res = requests.get(url, timeout=5)
        if res.status_code != 200:
            return []
        root = ET.fromstring(res.content)
        items = root.findall(".//item")[:max_items]
        return [item.find("title").text for item in items if item.find("title") is not None]
    except Exception:
        return []


def news_context(text: str) -> str:
    if not any(k in text for k in NEWS_KEYWORDS):
        return ""

    lines = []
    for name, url in NEWS_FEEDS.items():
        headlines = fetch_rss_headlines(url)
        if headlines:
            lines.append(f"【{name}】" + " / ".join(headlines))
    if not lines:
        return ""
    return "\n".join(lines)


def twitter_context(text: str, max_items: int = 5) -> str:
    if not any(k in text for k in TWITTER_KEYWORDS):
        return ""

    try:
        res = subprocess.run(
            [VENV311_PYTHON, TWITTER_SCRIPT],
            capture_output=True,
            text=True,
            timeout=15,
            cwd=LLN_DIR,
        )
        data = json.loads(res.stdout)
    except Exception:
        return ""

    if not data.get("ok"):
        return ""
    tweets = data.get("tweets", [])[:max_items]
    if not tweets:
        return ""

    lines = [f"@{t['user']}: {t['text'][:100]}" for t in tweets]
    return "【AI関連Xリストの最新投稿】\n" + "\n".join(lines)

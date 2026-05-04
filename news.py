"""RSS news fetching + weather."""
import xml.etree.ElementTree as ET

import requests
import streamlit as st

DEFAULT_RSS_FEEDS = {
    "GIZMODO": "https://www.gizmodo.jp/index.xml",
    "LIFEHACKER": "https://www.lifehacker.jp/feed/index.xml",
    "WIRED": "https://wired.jp/feed/rss",
    "Yahoo主要": "https://news.yahoo.co.jp/rss/topics/top-picks.xml",
    "Yahoo経済": "https://news.yahoo.co.jp/rss/categories/business.xml",
    "Bloomberg Markets": "https://feeds.bloomberg.com/markets/news.rss",
    "Bloomberg Politics": "https://feeds.bloomberg.com/politics/news.rss",
    "Bloomberg Tech": "https://feeds.bloomberg.com/technology/news.rss",
}

_CATEGORY_MAP = {
    "business": "ビジネス", "economy": "経済", "technology": "テクノロジー",
    "tech": "テクノロジー", "science": "サイエンス", "health": "健康",
    "sports": "スポーツ", "sport": "スポーツ", "entertainment": "エンタメ",
    "culture": "カルチャー", "politics": "政治", "world": "国際",
    "news": "ニュース", "opinion": "オピニオン", "lifestyle": "ライフスタイル",
    "life": "ライフスタイル", "gear": "ガジェット", "gadget": "ガジェット",
    "event": "イベント", "mobility": "モビリティ", "well-being": "健康",
    "wellbeing": "健康",
}


@st.cache_data(ttl=43200)
def fetch_rss_headlines(url: str, max_items: int = 5) -> list:
    try:
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        items = root.findall(".//item")[:max_items]
        return [item.find("title").text for item in items if item.find("title") is not None]
    except Exception:
        return []


@st.cache_data(ttl=43200)
def fetch_rss_items_with_category(url: str, source_name: str = "", max_items: int = 20) -> list:
    try:
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.content)
        result = []
        for item in root.findall(".//item")[:max_items]:
            title_el = item.find("title")
            if title_el is None:
                continue
            cat_el = item.find("category")
            link_el = item.find("link")
            desc_el = item.find("description")
            category = cat_el.text if cat_el is not None and cat_el.text else (source_name or "その他")
            result.append({
                "title": title_el.text or "",
                "category": category,
                "link": link_el.text if link_el is not None else "",
                "description": (desc_el.text or "")[:300] if desc_el is not None else "",
            })
        return result
    except Exception:
        return []


def get_rss_feeds() -> dict:
    settings = st.session_state.get("app_settings", {})
    return settings.get("rss_feeds", DEFAULT_RSS_FEEDS.copy())


def normalize_category(cat: str, source_name: str) -> str:
    lower = cat.lower().strip()
    if source_name.startswith("Bloomberg") or lower.startswith("nms") or "market" in lower:
        return "金融"
    return _CATEGORY_MAP.get(lower, cat)


def get_all_news_by_category(max_per_source: int = 10) -> dict:
    feeds = get_rss_feeds()
    by_category = {}
    for source_name, url in feeds.items():
        for item in fetch_rss_items_with_category(url, source_name, max_per_source):
            cat = normalize_category(item["category"], source_name)
            by_category.setdefault(cat, []).append({
                "title": item["title"],
                "source": source_name,
                "link": item.get("link", ""),
                "description": item.get("description", ""),
            })
    return by_category


def get_news_summary(max_per_source: int = 3) -> str:
    feeds = get_rss_feeds()
    lines = []
    for name, url in feeds.items():
        headlines = fetch_rss_headlines(url, max_per_source)
        if headlines:
            lines.append(f"【{name}】" + " / ".join(headlines))
    return "\n".join(lines) if lines else ""


def get_news_for_category(category: str, max_items: int = 5) -> str:
    items = get_all_news_by_category(max_per_source=10).get(category, [])[:max_items]
    if not items:
        return ""
    lines = []
    for item in items:
        desc = item.get("description", "").strip()[:150]
        link = item.get("link", "")
        if desc:
            lines.append(f"■ {item['title']}\n  {desc}" + (f"\n  {link}" if link else ""))
        else:
            lines.append(f"■ {item['title']}" + (f"\n  {link}" if link else ""))
    return "\n\n".join(lines)


@st.cache_data(ttl=1800)
def get_weather_meguro():
    try:
        r = requests.get(
            "https://wttr.in/Meguro,Tokyo?format=j1",
            timeout=5, headers={"Accept-Language": "ja"},
        )
        if r.status_code == 200:
            current = r.json().get("current_condition", [{}])[0]
            return {
                "temp": current.get("temp_C", "?"),
                "feel": current.get("FeelsLikeC", "?"),
                "desc": current.get("lang_ja", [{}])[0].get("value", current.get("weatherDesc", [{}])[0].get("value", "不明")),
                "humidity": current.get("humidity", "?"),
            }
    except Exception:
        pass
    return None


def get_time_period(hour: int) -> str:
    if 5 <= hour < 10:
        return "朝"
    elif 10 <= hour < 12:
        return "午前"
    elif 12 <= hour < 14:
        return "お昼"
    elif 14 <= hour < 17:
        return "午後"
    elif 17 <= hour < 19:
        return "夕方"
    elif 19 <= hour < 22:
        return "夜"
    else:
        return "深夜"

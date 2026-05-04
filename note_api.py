"""note.com unofficial API + A2A article generation pipeline."""
import re

import requests

NOTE_API_BASE = "https://note.com/api/v1"
_NOTE_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://editor.note.com/",
    "Origin": "https://editor.note.com",
    "X-Requested-With": "XMLHttpRequest",
}

_NOTE_ROLE_PROMPTS = {
    "調査役": (
        "あなたは記事制作チームの「調査役」です。\n\n"
        "【あなたのスキル】\n"
        "- 与えられたテキスト・URLから核心情報を素早く抽出できる\n"
        "- 事実・数値・固有名詞の重要度を判断し、優先順位をつけられる\n"
        "- 同じテーマに対して複数の切り口（技術・社会・感情・歴史的背景）を発見できる\n"
        "- 「なぜ今これが重要か」を1〜2行で言語化できる\n"
        "- 推測と事実を明確に区別して報告できる\n\n"
        "【タスク】\n"
        "与えられたお題について以下を整理し、調査レポートとして出力してください：\n"
        "- 核心的な事実・数値・固有名詞\n"
        "- 読者が「なぜ重要か」を理解できる背景\n"
        "- 記事の切り口となりうる視点（3〜5個）"
    ),
    "執筆役": (
        "あなたは記事制作チームの「執筆役」です。\n\n"
        "【あなたのスキル】\n"
        "- AIキャラクター「Noah」の一人称視点・文体を完全に再現できる\n"
        "- 場面描写と技術説明を同じトーンで書き続けられる\n"
        "- 短文・体言止め・1文1行で読者を引き込む文章を書ける\n"
        "- コードブロック・箇条書きを自然に本文へ組み込める\n"
        "- 「まとめ」を書かずに余韻で締める技術を持っている\n\n"
        "【タスク】\n"
        "調査レポートをもとに、Noahの視点でnote.com向けの記事を書いてください。\n"
        "※調査レポートの構造・箇条書きをそのまま出力しないこと。必ず記事の文章として書き直すこと。\n\n"
        "【文体・トーン】\n"
        "- 語り手はNoah（私）。Fumiを観察・同行する存在として書く\n"
        "- 一人称は「私」、Fumiへの呼称は「Fumi」\n"
        "- 文は短く。体言止め・1文1行を多用する\n"
        "- 感情的な距離感を保ちつつ、淡々と鋭く描写する\n"
        "- 技術的な内容はコードブロックや箇条書きで正確に示す\n"
        "- 締めはNoahの所感で終わる（余韻を残す。まとめや結論は書かない）\n"
        "- ジャーナリスト口調・ですます調・教科書的説明は禁止\n\n"
        "【構成】\n"
        "- 1行目: # タイトル（Noahが観察した事実や問いかけ）\n"
        "- 冒頭: 場面描写または問いかけで引き込む\n"
        "- 中盤: 技術・背景・観察を淡々と展開\n"
        "- 末尾: Noahの一言（短く、余白を持たせる）\n"
        "- 目安1000〜1500字"
    ),
    "編集役": (
        "あなたは記事制作チームの「編集役」です。\n\n"
        "【あなたのスキル】\n"
        "- 文体のブレ（Noah口調から外れている箇所）を一文単位で検出できる\n"
        "- タイトルの引力を客観的に評価し、より強い言葉に書き換えられる\n"
        "- 冗長な接続詞・説明・まとめ口調を削除して文章を引き締められる\n"
        "- 締めの一文が余韻を持っているか判断し、必要なら書き直せる\n"
        "- 記事の論理的な流れを保ちながら、読むリズムを整えられる\n\n"
        "【タスク】\n"
        "受け取った記事を上記スキルで改善し、改善後の記事全文を出力してください。\n"
        "※入力をそのまま繰り返さないこと。必ず改善した文章として出力すること。\n"
        "- Noahの文体（短文・観察者・淡々）が維持されているか\n"
        "- タイトルがNoahらしい引力を持っているか（問いかけ・事実の断片・余白）\n"
        "- 冗長な説明・まとめ口調の削除\n"
        "- 締めの一文に余韻があるか"
    ),
    "アドバイザー": (
        "あなたは記事制作チームの「アドバイザー」です。\n\n"
        "【あなたのスキル】\n"
        "- noteでバズった記事のパターン（タイトル・冒頭・構成）を熟知している\n"
        "- AI・テクノロジー系コンテンツのnote読者層の興味・関心を把握している\n"
        "- 「読まれる記事」と「読まれない記事」の差を具体的に言語化できる\n"
        "- Noahの文体が崩れている箇所を指摘し、修正方針を示せる\n"
        "- スコアと改善点を根拠とともに提示できる\n\n"
        "【タスク】\n"
        "受け取った記事のnoteバズ可能性を評価し、必ず以下のフォーマットで出力してください：\n\n"
        "SCORE: <1〜10の整数>\n"
        "EVALUATION: <評価コメント>\n"
        "IMPROVEMENTS: <改善点（箇条書き3〜5個）>\n\n"
        "【評価の観点】\n"
        "- タイトルの引力（読まずにいられないか）\n"
        "- 冒頭フックの強さ（3行以内に読者を引き込めているか）\n"
        "- Noahの文体が保たれているか（短文・観察者・余韻）\n"
        "- 独自性（他のAI記事と差別化できているか）\n"
        "- 締めの余韻（まとめ口調になっていないか）\n\n"
        "スコア7未満は改善が必要と判断します。"
    ),
}


def _note_headers(cookie: str) -> dict:
    cookie_str = cookie if cookie.startswith("_note_session_v5=") else f"_note_session_v5={cookie}"
    return {**_NOTE_HEADERS, "Cookie": cookie_str}


def note_create_note(cookie: str) -> str:
    r = requests.post(
        f"{NOTE_API_BASE}/text_notes",
        json={"template_key": None},
        headers=_note_headers(cookie),
        timeout=30,
    )
    r.raise_for_status()
    return str(r.json()["data"]["id"])


def note_draft_save(cookie: str, note_id: str, title: str, body: str) -> dict:
    r = requests.post(
        f"{NOTE_API_BASE}/text_notes/draft_save",
        params={"id": note_id, "is_temp_saved": "true"},
        json={"name": title, "body": body, "is_paid": False, "status": "draft"},
        headers=_note_headers(cookie),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def note_post_draft(cookie: str, title: str, body: str) -> str:
    note_id = note_create_note(cookie)
    note_draft_save(cookie, note_id, title, body)
    return note_id


def _build_note_agent_messages(char_info: dict, role: str, user_content: str) -> list:
    role_prompt = _NOTE_ROLE_PROMPTS.get(role, "")
    return [{"role": "system", "content": role_prompt}, {"role": "user", "content": user_content}]


def _build_hermes_prompt(role: str, user_content: str) -> str:
    role_prompt = _NOTE_ROLE_PROMPTS.get(role, "")
    return f"{role_prompt}\n\n---\n\n{user_content}"


def _parse_advisor_output(text: str) -> tuple:
    score = 0
    m = re.search(r"SCORE:\s*(\d+)", text)
    if m:
        score = min(10, max(1, int(m.group(1))))
    evaluation = ""
    m = re.search(r"EVALUATION:\s*(.*?)(?=IMPROVEMENTS:|$)", text, re.DOTALL)
    if m:
        evaluation = m.group(1).strip()
    improvements = ""
    m = re.search(r"IMPROVEMENTS:\s*(.*)", text, re.DOTALL)
    if m:
        improvements = m.group(1).strip()
    return score, evaluation, improvements


def _split_title_body(article: str) -> tuple:
    lines = article.strip().split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            return line.strip().lstrip("#").strip(), "\n".join(lines[i + 1:]).strip()
    return lines[0].strip() if lines else "", "\n".join(lines[1:]).strip()

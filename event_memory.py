"""Phase 3: TimeContext & EventResolution — イベント記憶と時間経過管理。"""
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from chat import call_lmstudio_chat_messages

_STORAGE_DIR = Path.home() / ".lmstudio_assistant" / "event_memory"

_PREP_TERMS_EVENT = [
    "準備", "買い出し", "材料", "リスト", "段取り", "役割分担", "行動計画",
    "予定", "明日", "明日の朝", "明日から",
]


# ─── TimeContext ──────────────────────────────────────────────────────────────

def _time_period(hour: int) -> str:
    if 5 <= hour < 11:
        return "朝"
    if 11 <= hour < 15:
        return "昼"
    if 15 <= hour < 18:
        return "夕方"
    if 18 <= hour < 22:
        return "夜"
    return "深夜"


@dataclass
class TimeContext:
    now: datetime
    time_period: str
    elapsed_since_last_turn: Optional[timedelta] = None

    @classmethod
    def from_now(cls, now: datetime, log_entries: list = None) -> "TimeContext":
        elapsed = None
        if log_entries:
            ts = log_entries[-1].get("timestamp")
            if ts:
                try:
                    last_dt = datetime.fromisoformat(ts)
                    elapsed = now - last_dt
                except (ValueError, TypeError):
                    pass
        return cls(now=now, time_period=_time_period(now.hour), elapsed_since_last_turn=elapsed)

    def to_prompt_text(self) -> str:
        lines = [f"現在時刻: {self.now.strftime('%Y-%m-%d %H:%M')} JST / 時間帯: {self.time_period}"]
        if self.time_period != "深夜":
            lines.append("過去ログに「深夜」とあっても現在はそうではありません。過去の状況として参照してください。")
        if self.elapsed_since_last_turn:
            h = self.elapsed_since_last_turn.total_seconds() / 3600
            if h >= 48:
                lines.append(f"前回会話から {int(h / 24)} 日以上が経過しています。古い話題は過去形で扱ってください。")
            elif h >= 8:
                lines.append(f"前回会話から約 {int(h)} 時間が経過しています。")
        return "\n".join(lines)


# ─── Event dataclasses ────────────────────────────────────────────────────────

@dataclass
class EventCandidate:
    title: str
    event_type: str = "activity_proposal"
    aliases: list = field(default_factory=list)
    evidence_messages: list = field(default_factory=list)
    confidence: float = 0.0
    first_seen_at: str = ""
    last_seen_at: str = ""
    mentions: int = 1
    time_hint: str = "unknown"
    participants: list = field(default_factory=list)


@dataclass
class EventMemory:
    id: str
    title: str
    event_type: str = "activity_proposal"
    aliases: list = field(default_factory=list)
    status: str = "planned"  # planned / maybe_done / assumed_done / expired / abandoned / needs_resolution
    first_seen_at: str = ""
    last_seen_at: str = ""
    participants: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    outcome_summary: Optional[str] = None
    next_hook: Optional[str] = None
    preparation_mentions: int = 0  # 準備系発話の累積カウント（>=3でneeds_resolution）


@dataclass
class ResolvedEvent:
    id: str
    title: str
    status: str
    outcome_summary: str
    resolved_at: str
    next_hook: Optional[str] = None


# ─── Storage ─────────────────────────────────────────────────────────────────

def _path(filename: str) -> Path:
    _STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    return _STORAGE_DIR / filename


def load_candidates() -> list:
    p = _path("candidates.json")
    if not p.exists():
        return []
    try:
        return [EventCandidate(**d) for d in json.loads(p.read_text(encoding="utf-8"))]
    except Exception:
        return []


def save_candidates(candidates: list):
    _path("candidates.json").write_text(
        json.dumps([c.__dict__ for c in candidates], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_events() -> list:
    p = _path("events.json")
    if not p.exists():
        return []
    try:
        return [EventMemory(**d) for d in json.loads(p.read_text(encoding="utf-8"))]
    except Exception:
        return []


def save_events(events: list):
    _path("events.json").write_text(
        json.dumps([e.__dict__ for e in events], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_resolved() -> list:
    p = _path("resolved.json")
    if not p.exists():
        return []
    try:
        return [ResolvedEvent(**d) for d in json.loads(p.read_text(encoding="utf-8"))]
    except Exception:
        return []


def save_resolved(resolved: list):
    _path("resolved.json").write_text(
        json.dumps([r.__dict__ for r in resolved], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ─── EventIntentClassifier ───────────────────────────────────────────────────

def classify_event_intent(reply: str, speaker: str, recent_context: str,
                           base_url: str, model: str) -> dict:
    """LLMでイベント意図を分類する。temperature=0.1、JSONのみ返す。"""
    prompt = f"""あなたは会話ログから「後で結果や感想に変換できる行動・企画」を抽出する分類器です。

次の発話が、キャラクターたちの世界内で何かを実行しようとしている提案・予定・役割決め・企画に該当するか判定してください。

重要:
- キーワードだけで判断しない
- 単なる感想・同意・雑談は false
- 後日「結局どうなったか？」と自然に振り返れるものだけ true
- 出力はJSONのみ（説明文なし）

発話: {speaker}: {reply}

直近の会話文脈:
{recent_context}

出力形式:
{{
  "is_event_intent": true/false,
  "event_type": "activity_proposal | role_assignment | decision | plan | none",
  "title": "短いイベント名 or null",
  "confidence": 0.0-1.0,
  "time_hint": "now | tonight | tomorrow | later | unknown",
  "participants": [],
  "should_resolve_later": true/false,
  "reason": "短い理由"
}}"""
    try:
        raw = call_lmstudio_chat_messages(
            base_url, model,
            [{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=200, timeout=30, background=True,
        )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception:
        pass
    return {"is_event_intent": False, "confidence": 0.0}


# ─── Candidate/Event management ──────────────────────────────────────────────

def _normalize_title(title: str) -> str:
    if not title:
        return ""
    title = re.sub(r"(?:をする|をやる|をみる|を見る|の話|の件)$", "", title)
    return title[:20].strip()


def _similar_title(a: str, b: str) -> bool:
    if not a or not b:
        return False
    words_a = set(re.findall(r"[一-鿿ぁ-ゟ]{2,}", a))
    words_b = set(re.findall(r"[一-鿿ぁ-ゟ]{2,}", b))
    if not words_a or not words_b:
        return a == b
    return len(words_a & words_b) / min(len(words_a), len(words_b)) >= 0.5


def _promote_candidate(candidate: EventCandidate, candidates: list,
                        events: list, now_str: str) -> tuple:
    for e in events:
        if _similar_title(e.title, candidate.title):
            return events, [c for c in candidates if c is not candidate]
    events.append(EventMemory(
        id=str(uuid.uuid4())[:8],
        title=candidate.title,
        event_type=candidate.event_type,
        aliases=list(candidate.aliases),
        status="planned",
        first_seen_at=candidate.first_seen_at,
        last_seen_at=now_str,
        participants=list(candidate.participants),
        evidence=list(candidate.evidence_messages),
    ))
    return events, [c for c in candidates if c is not candidate]


def update_event_candidates(classification: dict, reply: str, speaker: str,
                             now: datetime, candidates: list, events: list) -> tuple:
    """候補を更新し、昇格条件を満たせば EventMemory に移す。returns (candidates, events)"""
    if not classification.get("is_event_intent"):
        return candidates, events
    if classification.get("confidence", 0) < 0.6:
        return candidates, events
    title = _normalize_title(classification.get("title") or "")
    if not title:
        return candidates, events

    now_str = now.isoformat()
    matched = next((c for c in candidates if _similar_title(c.title, title) or title in c.aliases), None)

    if matched:
        matched.mentions += 1
        matched.evidence_messages.append(f"{speaker}: {reply[:60]}")
        matched.last_seen_at = now_str
        matched.confidence = max(matched.confidence, classification.get("confidence", 0))
        if title != matched.title and title not in matched.aliases:
            matched.aliases.append(title)
        if speaker and speaker not in matched.participants:
            matched.participants.append(speaker)
    else:
        matched = EventCandidate(
            title=title,
            event_type=classification.get("event_type", "activity_proposal"),
            evidence_messages=[f"{speaker}: {reply[:60]}"],
            confidence=classification.get("confidence", 0),
            first_seen_at=now_str,
            last_seen_at=now_str,
            time_hint=classification.get("time_hint", "unknown"),
            participants=[speaker] if speaker else [],
        )
        candidates.append(matched)

    has_role = classification.get("event_type") in ("role_assignment", "decision")
    has_time = matched.time_hint in ("now", "tonight", "tomorrow")
    if matched.mentions >= 2 or has_role or (has_time and matched.confidence >= 0.75):
        events, candidates = _promote_candidate(matched, candidates, events, now_str)

    return candidates, events


# ─── EventResolver ────────────────────────────────────────────────────────────

def update_preparation_mentions(reply: str, events: list) -> list:
    """準備系発言でplannedイベントの preparation_mentions をインクリメント。"""
    if not any(t in reply for t in _PREP_TERMS_EVENT):
        return events
    for e in events:
        if e.status == "planned":
            e.preparation_mentions += 1
            break
    return events


def _generate_decided_summary(event: EventMemory, base_url: str, model: str) -> str:
    """準備ループ3回で「決定済みまとめ」を生成。"""
    participants = ", ".join(event.participants) if event.participants else "不明"
    prompt = (
        f"以下のキャラクター会話イベントについて、「誰が何をするか決まった」という"
        f"形の50字以内のまとめを生成してください。\n\n"
        f"イベント: {event.title}\n"
        f"参加者: {participants}\n"
        f"内容: {event.evidence[-1] if event.evidence else ''}\n\n"
        f"ルール:\n"
        f"- 「決まった・まとまった」形で終わらせる\n"
        f"- 例: 「めたんが野菜、つむぎが麺、きりたんが手元担当で決まった」\n"
        f"- 50字以内のテキストのみ出力"
    )
    try:
        result = call_lmstudio_chat_messages(
            base_url, model,
            [{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=80, timeout=30, background=True,
        )
        return result.strip()[:100]
    except Exception:
        return f"{event.title}の役割分担がまとまった。"


def _generate_outcome(event: EventMemory, base_url: str, model: str) -> str:
    """OutcomeGenerator: 日常的な小さな結果を生成する。temperature=0.4。"""
    prompt = f"""以下のキャラクター会話イベントについて、数時間後の自然な結末を50字以内で生成してください。

イベント: {event.title}
参加者: {', '.join(event.participants) if event.participants else '不明'}
内容: {event.evidence[-1] if event.evidence else ''}

ルール:
- 小さく日常的な出来事にする（大事件・大喧嘩・全員で行動は禁止）
- 例: 「予告編だけ見て十分怖くなった」「材料候補だけメモした」「話しているうちに眠くなった」
- 50字以内のテキストのみ出力（説明・前置き不要）"""
    try:
        result = call_lmstudio_chat_messages(
            base_url, model,
            [{"role": "user", "content": prompt}],
            temperature=0.4, max_tokens=80, timeout=30, background=True,
        )
        return result.strip()[:100]
    except Exception:
        return f"{event.title}の話は一区切りついた。"


def resolve_events(events: list, resolved: list, now: datetime,
                   base_url: str, model: str) -> tuple:
    """時間経過でイベントの状態を更新する。returns (events, resolved)"""
    updated: list = []
    new_resolved: list = list(resolved)

    for event in events:
        if event.status not in ("planned", "maybe_done", "needs_resolution"):
            updated.append(event)
            continue
        try:
            last_seen = datetime.fromisoformat(event.last_seen_at)
            elapsed_h = (now - last_seen).total_seconds() / 3600
        except (ValueError, TypeError):
            updated.append(event)
            continue

        if elapsed_h >= 72:
            event.status = "expired"
            event.outcome_summary = "数日前の話題のため準備中として扱わない。触れるなら思い出話か別話題への橋渡しにする。"
            new_resolved.append(ResolvedEvent(
                id=event.id, title=event.title, status="expired",
                outcome_summary=event.outcome_summary,
                resolved_at=now.isoformat(),
            ))
        elif elapsed_h >= 8:
            if event.status != "assumed_done":
                event.status = "assumed_done"
                if not event.outcome_summary:
                    event.outcome_summary = _generate_outcome(event, base_url, model)
            updated.append(event)
        elif elapsed_h >= 2:
            event.status = "maybe_done"
            updated.append(event)
        elif event.status == "planned" and event.preparation_mentions >= 3:
            # 準備ループ3回 → 決定済みとして閉じる
            event.status = "needs_resolution"
            if not event.outcome_summary:
                event.outcome_summary = _generate_decided_summary(event, base_url, model)
            updated.append(event)
        else:
            updated.append(event)

    return updated, new_resolved


# ─── Prompt builder ───────────────────────────────────────────────────────────

def build_event_context_prompt(events: list, resolved: list,
                                time_ctx: TimeContext) -> str:
    """イベント状態とTimeContextをプロンプトブロックにまとめる。"""
    lines = ["【時間・過去の企画の状況】", time_ctx.to_prompt_text()]

    expired_or_done = [r for r in resolved[-3:] if r.status in ("expired", "assumed_done")]
    if expired_or_done:
        lines.append("消化済み・流れた企画:")
        for r in expired_or_done:
            summary = r.outcome_summary or ("流れた企画" if r.status == "expired" else "実施済み")
            lines.append(f"  ・「{r.title}」: {summary}")

    active = [e for e in events if e.status in ("planned", "maybe_done", "assumed_done", "needs_resolution")]
    if active:
        lines.append("現在話し合っている話題:")
        for e in active[:3]:
            if e.status == "assumed_done":
                lines.append(f"  ・「{e.title}」: おそらく実施済み。感想や次の展開に移ってよい。")
            elif e.status == "maybe_done":
                lines.append(f"  ・「{e.title}」: 実施した可能性あり。結果や感想に移ってよい。")
            elif e.status == "needs_resolution":
                summary = e.outcome_summary or "役割がまとまった"
                lines.append(f"  ・「{e.title}」: {summary}。これ以上準備の繰り返し不要。次の実行や結果に移ってよい。")
            else:
                lines.append(f"  ・「{e.title}」: 話し合い中。準備話の繰り返しは避ける。")

    return "\n".join(lines)

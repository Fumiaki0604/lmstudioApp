"""Phase H1: Hermes Director — 会話状態の診断と MovePlanner への重み付け。"""
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

_DIRECTOR_PROFILE = str(Path.home() / ".hermes" / "profiles" / "hermes-director")

_PROMPT_TEMPLATE = """\
以下の会話状態を診断し、MovePlannerへの処方箋をJSONで返してください。

現在時刻: {now}
会話ステージ: {topic_stage}
話題継続ターン数: {topic_age}
気遣いループスコア: {care_loop_score}
繰り返し語: {repeated_terms}
直近のmove_types: {last_move_types}
active_goal: {active_goal}

イベント記憶:
{event_memory}

直近ログ（最新8件）:
{recent_log}

出力JSON（このフォーマットのみ）:
{{
  "status": "normal | topic_loop | preparation_loop | stale_event | persona_drift",
  "problem": "短い診断（日本語）",
  "recommended_moves": [],
  "avoid_moves": [],
  "event_action": {{
    "target_event": null,
    "action": "none | assume_small_outcome | mark_expired | close_topic",
    "suggested_outcome": null
  }},
  "speaker_suggestions": [],
  "next_topic_hint": null,
  "thread_seed": null,
  "confidence": 0.0
}}"""


@dataclass
class DirectorAdvice:
    status: str = "normal"
    problem: str = ""
    recommended_moves: list = field(default_factory=list)
    avoid_moves: list = field(default_factory=list)
    event_action: dict = field(default_factory=lambda: {"target_event": None, "action": "none", "suggested_outcome": None})
    speaker_suggestions: list = field(default_factory=list)
    next_topic_hint: Optional[str] = None
    thread_seed: Optional[dict] = None
    confidence: float = 0.0


def _build_prompt(conv_state, recent_log: list, events: list, now: datetime) -> str:
    log_lines = "\n".join(
        f"【{e.get('name', '?')}】{e.get('text', '')[:60]}"
        for e in recent_log[-8:]
    )
    event_lines = "\n".join(
        f"・「{e.title}」({e.status}) {e.evidence[-1][:40] if e.evidence else ''}"
        for e in events[:5]
    ) if events else "なし"

    repeated = ", ".join(
        w for w in (conv_state.current_topic_terms or [])[:5]
    ) if conv_state else ""

    return _PROMPT_TEMPLATE.format(
        now=now.strftime("%Y-%m-%d %H:%M JST"),
        topic_stage=getattr(conv_state, "topic_stage", "active"),
        topic_age=getattr(conv_state, "topic_age", 0),
        care_loop_score=round(getattr(conv_state, "care_loop_score", 0.0), 2),
        repeated_terms=repeated or "なし",
        last_move_types=", ".join(getattr(conv_state, "last_move_types", [])[-5:]) or "なし",
        active_goal=getattr(conv_state, "active_goal", "") or "なし",
        event_memory=event_lines,
        recent_log=log_lines or "なし",
    )


def run_hermes_director(conv_state, recent_log: list, events: list,
                        now: datetime, timeout: int = 45) -> Optional[DirectorAdvice]:
    """Hermes Director を呼び出して DirectorAdvice を返す。失敗時は None。"""
    prompt = _build_prompt(conv_state, recent_log, events, now)
    env = os.environ.copy()
    env["PATH"] = os.path.expanduser("~/.local/bin") + ":" + env.get("PATH", "")
    env["HERMES_HOME"] = _DIRECTOR_PROFILE
    try:
        result = subprocess.run(
            ["hermes", "-z", prompt],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        output = result.stdout.strip()
        if not output:
            return None
        m = re.search(r"\{.*\}", output, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group())
        return DirectorAdvice(
            status=data.get("status", "normal"),
            problem=data.get("problem", ""),
            recommended_moves=[r for r in data.get("recommended_moves", []) if isinstance(r, str)],
            avoid_moves=[r for r in data.get("avoid_moves", []) if isinstance(r, str)],
            event_action=data.get("event_action") or {"target_event": None, "action": "none", "suggested_outcome": None},
            speaker_suggestions=data.get("speaker_suggestions", []),
            next_topic_hint=data.get("next_topic_hint"),
            thread_seed=data.get("thread_seed"),
            confidence=float(data.get("confidence", 0.0)),
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, json.JSONDecodeError, Exception):
        return None


def should_call_director(turn_count: int, conv_state) -> bool:
    """ディレクターを呼ぶべきか判定する。"""
    if turn_count > 0 and turn_count % 5 == 0:
        return True
    if conv_state is None:
        return False
    if getattr(conv_state, "care_loop_score", 0) >= 0.4:
        return True
    if getattr(conv_state, "topic_stage", "active") in ("aging", "closing"):
        return True
    if getattr(conv_state, "topic_age", 0) >= 6:
        return True
    return False

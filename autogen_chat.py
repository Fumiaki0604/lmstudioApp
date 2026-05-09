"""AutoGen PoC: GroupChat with LM Studio characters."""
import queue
import re
import threading
from typing import Optional

import autogen

from chat import call_hermes_agent_chat, call_noah_chat, normalize_model_output
from speakers import (
    _load_soul,
    affinity_behavior,
    extract_soul_interests,
    format_episodes_for_prompt,
    load_episodes,
    parse_soul_affinities,
    sanitize_soul_for_prompt,
    save_episode,
)

LMSTUDIO_BASE_URL = "http://localhost:1234/v1"


def _make_llm_config(model: str, temperature: float = 0.85) -> dict:
    return {
        "config_list": [
            {
                "model": model,
                "base_url": LMSTUDIO_BASE_URL,
                "api_key": "lm-studio",
                "api_type": "openai",
            }
        ],
        "temperature": temperature,
        "max_tokens": 120,
        "timeout": 120,
        "cache_seed": None,
    }


def _build_system_prompt(name: str, personality: str, first_person: str,
                          second_person: str, members: list,
                          soul_text: str = "", interests: str = "",
                          affinity_hints: list = None,
                          forbidden_first_persons: list = None,
                          recent_episodes: str = "") -> str:
    others = [m for m in members if m != name]
    fp = first_person or "私"
    parts = []

    # ① キャラ同一性（最上位・最優先）
    fp_ban = ""
    if forbidden_first_persons:
        fp_ban = f"「{'」「'.join(set(forbidden_first_persons))}」は他のキャラの一人称なので絶対に使わない。"
    parts.append(
        f"あなたは「{name}」です。他のキャラクターではありません。\n"
        f"一人称は必ず「{fp}」のみ使う。{fp_ban}\n"
        f"返答は1〜2文の日本語のみ。3文以上書かない。"
    )

    # ② 性格・口調
    parts.append(f"【{name}の性格・口調】\n{personality or '明るく親しみやすい性格。'}")

    # ③ 禁止事項
    parts.append(
        f"【禁止事項】\n"
        f"- 他のキャラ（{', '.join(others)}）の口調・語尾を使わない\n"
        f"- 造語・架空の固有名詞を使わない\n"
        f"- [MOOD:xxx] などのタグを出力しない\n"
        f"- 直前の発言に出てきた単語・フレーズをそのまま繰り返さない\n"
        f"- 他の人が言ったことへの同意だけで終わらせない\n"
        f"- 自分だけが知っていること・感じていることを必ず一つ加える"
    )

    # ④ Soul（先頭600文字に制限してキャラ設定を圧迫しない）
    if soul_text:
        parts.append(f"【{name}の記憶・自己認識】\n{soul_text[:600]}")

    # ⑤ 親密度
    if affinity_hints:
        parts.append("【他メンバーへの態度】\n" + "\n".join(affinity_hints))

    # ⑥ 関心事
    if interests:
        parts.append(f"【最近の関心】{interests}")

    # ⑦ 直近エピソード（具体的な出来事。soul.mdとは別）
    if recent_episodes:
        parts.append(f"【直近の出来事メモ】（参考程度に。会話に自然に出してもよい）\n{recent_episodes}")

    return "\n\n".join(parts)


def _clean_output(text: str) -> str:
    text = re.sub(r"\[MOOD:[^\]]+\]\s*", "", text)
    text = re.sub(r"\[\w+\]\s*", "", text)
    return normalize_model_output(text)


class StreamingGroupChat:
    """AutoGen GroupChat を別スレッドで走らせ、発言をキューに流す。"""

    def __init__(self, model: str, characters: list, max_turns: int = 20):
        self.model = model
        self.characters = characters
        self.max_turns = max_turns
        self._q: queue.Queue = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self, initial_message: str):
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, args=(initial_message,), daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def get_messages(self) -> list:
        """Non-blocking drain of the queue."""
        msgs = []
        while True:
            try:
                msgs.append(self._q.get_nowait())
            except queue.Empty:
                break
        return msgs

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self, initial_message: str):
        names = [c["name"] for c in self.characters]
        llm_config = _make_llm_config(self.model, temperature=0.85)
        q = self._q
        stop = self._stop_event

        # Load souls for all characters upfront
        souls: dict = {c["name"]: _load_soul(c["name"]) for c in self.characters}
        # Map of name → first_person for cross-contamination prevention
        fp_map: dict = {c["name"]: c.get("first_person", "私") for c in self.characters}

        agents = []
        for c in self.characters:
            soul_raw = souls[c["name"]]
            soul_clean = sanitize_soul_for_prompt(soul_raw) if soul_raw else ""
            interests = extract_soul_interests(soul_raw) if soul_raw else ""

            # Build affinity hints toward other members
            affinity_hints = []
            if soul_raw:
                affinities = parse_soul_affinities(soul_raw)
                for other in names:
                    if other == c["name"]:
                        continue
                    score = affinities.get(other)
                    if score is not None:
                        affinity_hints.append(affinity_behavior(score, other))

            # Other characters' first-person pronouns (to explicitly forbid)
            other_fps = [fp for n, fp in fp_map.items() if n != c["name"] and fp != c.get("first_person", "私")]

            # Load recent episodes for this character
            episodes = load_episodes(c["name"], limit=5)
            episodes_str = format_episodes_for_prompt(episodes)

            sys_prompt = _build_system_prompt(
                c["name"], c.get("personality", ""),
                c.get("first_person", "私"), c.get("second_person", "あなた"),
                names,
                soul_text=soul_clean,
                interests=interests,
                affinity_hints=affinity_hints,
                forbidden_first_persons=other_fps,
                recent_episodes=episodes_str,
            )
            is_noah = c.get("is_noah", False)
            is_hermes = c.get("is_hermes_agent", False)
            hermes_profile = c.get("hermes_profile", "lmstudio-char")

            if is_noah or is_hermes:
                # Noah / Hermes: LM Studio を使わず custom reply function で差し替え
                agent = autogen.ConversableAgent(
                    name=c["name"],
                    system_message=sys_prompt,
                    llm_config=False,
                    human_input_mode="NEVER",
                    max_consecutive_auto_reply=self.max_turns,
                )

                def _make_custom_reply(name, prompt, _is_noah, _profile):
                    def reply_func(recipient, messages, sender, config):
                        if stop.is_set():
                            return True, None
                        # system prompt を先頭に差し込んだ messages を構築
                        formatted = [{"role": "system", "content": prompt}]
                        for msg in (messages or []):
                            role = msg.get("role", "user")
                            content = msg.get("content", "")
                            if role in ("user", "assistant") and content:
                                formatted.append({"role": role, "content": content})
                        try:
                            if _is_noah:
                                text, _ = call_noah_chat(formatted)
                            else:
                                text, _ = call_hermes_agent_chat(formatted, profile=_profile)
                            cleaned = _clean_output(text)
                            if cleaned:
                                q.put({"name": name, "text": cleaned, "done": False})
                                try:
                                    save_episode(name, cleaned)
                                except Exception:
                                    pass
                            return True, cleaned
                        except Exception as e:
                            err = f"[{name} Error: {str(e)[:80]}]"
                            q.put({"name": name, "text": err, "done": False})
                            return True, err
                    return reply_func

                agent.register_reply(
                    [autogen.Agent, None],
                    _make_custom_reply(c["name"], sys_prompt, is_noah, hermes_profile),
                    position=0,
                )
            else:
                # LM Studio キャラ: 標準 llm_config + send パッチ
                agent = autogen.ConversableAgent(
                    name=c["name"],
                    system_message=sys_prompt,
                    llm_config=llm_config,
                    human_input_mode="NEVER",
                    max_consecutive_auto_reply=self.max_turns,
                )

                def _make_send(orig, name):
                    def patched(message, recipient, request_reply=None, silent=False):
                        if stop.is_set():
                            return
                        # dict の場合は role を確認して system/function は除外
                        if isinstance(message, dict):
                            role = message.get("role", "user")
                            if role in ("system", "function", "tool"):
                                return orig(message, recipient, request_reply=request_reply, silent=True)
                            content = message.get("content", "")
                        else:
                            content = message or ""
                        if content and content.strip():
                            cleaned = _clean_output(content)
                            if cleaned:
                                q.put({"name": name, "text": cleaned, "done": False})
                                try:
                                    save_episode(name, cleaned)
                                except Exception:
                                    pass
                        return orig(message, recipient, request_reply=request_reply, silent=True)
                    return patched

                agent.send = _make_send(agent.send, c["name"])

                # 会話履歴を直近5件に制限（前の発言に引っ張られるのを抑制）
                _orig_gen = agent.generate_reply
                def _make_windowed_gen(orig, window=5):
                    def windowed(messages=None, sender=None, **kwargs):
                        if messages and len(messages) > window:
                            messages = messages[-window:]
                        return orig(messages=messages, sender=sender, **kwargs)
                    return windowed
                agent.generate_reply = _make_windowed_gen(_orig_gen)

            agents.append(agent)

        # Balanced speaker selection: avoid last speaker, prefer least-active
        participation: dict = {a.name: 0 for a in agents}

        def balanced_select(last_speaker, groupchat):
            import random
            candidates = [a for a in groupchat.agents if a is not last_speaker]
            if not candidates:
                candidates = list(groupchat.agents)
            min_p = min(participation[a.name] for a in candidates)
            primary = [a for a in candidates if participation[a.name] == min_p]
            secondary = [a for a in candidates if participation[a.name] == min_p + 1]
            pool = primary * 3 + secondary
            chosen = random.choice(pool) if pool else random.choice(candidates)
            participation[chosen.name] += 1
            return chosen

        groupchat = autogen.GroupChat(
            agents=agents,
            messages=[],
            max_round=self.max_turns,
            speaker_selection_method=balanced_select,
            allow_repeat_speaker=False,
        )
        manager = autogen.GroupChatManager(
            groupchat=groupchat,
            llm_config=_make_llm_config(self.model, temperature=0.1),
        )

        try:
            agents[0].initiate_chat(
                manager,
                message=initial_message,
                silent=True,
            )
        except Exception as e:
            q.put({"name": "ERROR", "text": str(e)[:300], "done": False})
        finally:
            q.put({"done": True, "name": "", "text": ""})

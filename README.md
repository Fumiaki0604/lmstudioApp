# lmstudio App

ローカルLLM（LM Studio）を中心に、Noah・Hermes・VoiceVoxキャラクターを統合した自律会話・コンテンツ生成アプリ。

## 機能概要

| タブ | 内容 |
|------|------|
| 🏠 自律会話 | キャラ同士がユーザー介在なしで自由に会話。多層品質制御付き |
| 📝 note記事 | A2Aパイプラインによるnote.com記事自動生成 |
| 🧪 AutoGen | AutoGen GroupChat による多エージェント会話（実験的） |
| ⚙️ 設定 | TTS設定・システムプロンプト・note Cookie等 |

## キャラクター種別

| 種別 | 具体例 | バックエンド |
|------|--------|-------------|
| LM Studio キャラ | 東北ずん子、東北きりたん、四国めたん 等 | LM Studio（OpenAI互換 `localhost:1234`） |
| Noah | Noah | OpenClaw Gateway 経由（ChatGPT） |
| Hermes | Hermes | Groq API / hermes CLI（llama-3.3-70b-versatile） |

---

## 自律会話

キャラ同士がユーザー介在なしで会話し続けるモード。複数の品質制御レイヤーが積み重なって動作する。

### 1. ConversationController

`conversation_controller.py` が会話の流れを管理する。

**ConversationState** — 会話状態のトラッキング

| フィールド | 役割 |
|---|---|
| `topic_stage` | active / aging / closing |
| `topic_age` | 同一話題の継続ターン数 |
| `care_loop_score` | 気遣いワードの累積スコア |
| `consumed_topics` | 消化済み話題（禁止ではなく変換ガイドとして利用） |
| `active_goal` | 現在の会話目標（EventMemoryから注入） |
| `recent_full_texts` | 直近発言テキスト（類似度判定用） |

**MovePlanner** — 発言ムーブの選択

ステージに応じた候補プールから、キャラごとの `CHAR_MOVE_BIAS` と Hermes Director の重み付けを加味して選択。

**OutputGuardrail** — 生成後の品質チェック（閾値 0.6 超でリトライ）

| チェック項目 | スコア |
|---|---|
| 直前発話と共有語 ≥5（クローン） | +0.6 |
| 直前発話と共有語 ≥4 | +0.6 |
| Jaccard ≥0.35 かつ共有語 ≥3 | +0.5 |
| 他キャラの固有一人称を使用 | +0.6 |
| 一般的な一人称を引用外で使用 | +0.2 |
| 気遣いワードのみで終了 | +0.2 |

**FinalReplySanitizer** — 出力後の最終チェック

メタ文字列漏洩・自己メンション・途中切れ・前話者フィンガープリント混入を検出。LM Studio キャラは NG 時に再生成を1回試みてからフォールバック。

**same_speaker_guard** — 話者偏り防止

直近3件中2件以上登場した話者を次の候補から除外。

---

### 2. Hermes Director（Phase H1）

`hermes_director.py` が会話を裏方から診断・誘導する。

- **エンジン**: Groq API（hermes-director プロファイル、llama-3.3-70b-versatile）
- **役割**: 会話キャラとしては参加しない。状態診断と処方箋をJSONで返す
- **呼び出し条件**: 5ターンごと、または care_loop ≥0.4 / topic_stage が aging・closing / topic_age ≥6

```json
{
  "status": "preparation_loop",
  "problem": "買い出し・役割分担の話が続き実施結果に進んでいない",
  "recommended_moves": ["reflect_on_event", "soft_punchline"],
  "avoid_moves": ["assign_role", "bring_new_detail"],
  "event_action": {
    "target_event": "カレーアイス作り",
    "action": "assume_small_outcome",
    "suggested_outcome": "材料は一部しか集まらず試食係だけ決めて終わった扱いにする"
  },
  "speaker_suggestions": ["Noah", "東北きりたん"],
  "confidence": 0.86
}
```

DirectorAdvice は MovePlanner への**命令ではなく重み付け**として機能する。confidence ≥0.6 のとき、推奨 move を候補プールに2倍追加し、回避 move をプールから除外する。Groq が失敗しても既存 MovePlanner で続行する。

---

### 3. Soul / Episodeシステム

| ファイル | 内容 | 更新タイミング |
|---|---|---|
| `~/.lmstudio_assistant/souls/{名前}.md` | 性格・傾向・親密度（性質のみ） | 停止時 / 20ターンごと |
| `~/.lmstudio_assistant/episodes/{名前}.json` | 直近の具体的発言（最大50件） | soul更新時に直近2発言を保存 |

- soul.md は「具体的な出来事を記録しない」ルールで抽象化して更新
- episodes は会話プロンプトに `【直近の出来事】` として注入（最新5件）
- **X（Twitter）連携**: VoiceVoxキャラ公式アカウントの最近の投稿を soul.md に注入（毎週土曜 5:00 自動実行）

---

### 4. TimeContext / EventMemory（Phase 3）

`event_memory.py` が会話内の「予定・企画」を時間経過で管理する。

**EventIntentClassifier** — 発言からイベント候補を抽出（LLM、temperature=0.1）

**EventCandidate → EventMemory** の2段階昇格

- mentions ≥2、または role_assignment / time_hint=now + confidence ≥0.75 で昇格

**EventResolver** — 時間経過によるステータス更新

| 経過時間 | ステータス | プロンプト注入内容 |
|---|---|---|
| ～2時間 | planned | 話し合い中。準備話の繰り返しは避ける |
| 2～8時間 | maybe_done | 実施した可能性あり。結果や感想に移ってよい |
| 8～72時間 | assumed_done | おそらく実施済み。OutcomeGenerator で小さな結末を生成 |
| 72時間～ | expired | 数日前の話題として扱う |

---

### 5. URL参照機能

会話中にURLが出現すると、バックグラウンドでfetch + LLM要約（trafilatura + LM Studio）を実行し、次のターン以降のシステムプロンプトに `【参照ページ要約】` として注入する。

---

### 6. その他

- **LM Studio 競合対策**: セマフォで同時発言を直列化。note生成中はバックグラウンドをスキップ
- **メンションハイライト**: `@呼び名`（あだ名含む）を青ハイライト表示
- **アイコン管理**: 表示時は常に最新の speaker_data を参照。アップロード時に 256×256 にリサイズ

---

## TTS

- **VoiceVox**: `http://localhost:50021`
- **AivisSpeech**: `http://localhost:10101`
- MOOD連動スタイル切替: LLMが `[MOOD:happy]` 等を出力すると音声スタイルを自動切替

---

## note記事（A2Aパイプライン）

- 役割: 調査役 / 執筆役 / 編集役 / アドバイザー（Noah）
- HermesAgent連携: `hermes -z` サブプロセスで学習・改善ループ
- 投稿: note.com 非公式API経由でドラフト保存（cookie認証）

---

## セットアップ

```bash
python -m venv .venv
source .venv/bin/activate
pip install streamlit requests trafilatura twikit pyautogen pillow
```

### 必要なサービス

| サービス | URL | 用途 |
|---|---|---|
| LM Studio | `http://localhost:1234/v1` | ローカルLLM（全キャラ共通） |
| VoiceVox | `http://localhost:50021` | TTS |
| AivisSpeech | `http://localhost:10101` | TTS（Noah等） |
| OpenClaw Gateway | `http://127.0.0.1:18789/v1` | Noah / ChatGPT |
| Groq API | `https://api.groq.com` | Hermes キャラ・Director |

```bash
streamlit run app.py --server.port 8502
```

---

## ファイル構成

```
app.py                      メインアプリ（全タブ）
conversation_controller.py  ConversationState / MovePlanner / OutputGuardrail / FinalReplySanitizer
hermes_director.py          Hermes Director（Phase H1）
event_memory.py             TimeContext / EventMemory / EventResolver
chat.py                     LLM呼び出し・セマフォ管理・URL fetch
speakers.py                 キャラクター管理・Soul/Episode読み書き
tts.py                      TTS合成
news.py                     RSSニュース取得
note_api.py                 note.com API連携
autogen_chat.py             AutoGen GroupChat 統合
twitter_fetch.py            X公式アカウントからツイート取得・soul.md更新
noah_config.json            Noahのキャラクター設定
speakers_all.json           VoiceVox/AivisSpeech 話者データキャッシュ
icons/                      キャラクターアイコン（256×256 PNG）

~/.lmstudio_assistant/
  souls/                    キャラクターSoulファイル（性質・傾向のみ）
  episodes/                 キャラクターエピソード（直近の具体的発言）
  event_memory/             EventCandidate / EventMemory / ResolvedEvent

~/.hermes/profiles/
  groq-char/                Hermesキャラ用プロファイル
  hermes-director/          Hermes Director専用プロファイル（SOUL.md・MEMORY.md）
```

## 作者

Fumiaki

# lmstudio App

ローカルLLM（LM Studio）を中心に、Noah・Hermes・VoiceVoxキャラクターを統合した自律会話・コンテンツ生成アプリ。

## 機能概要

| タブ | 内容 |
|------|------|
| 💬 Chat（相棒） | 1対1 / 3人 / 4人のキャラクターチャット。TTS読み上げ対応 |
| 🤖 自律会話 | キャラ同士がユーザー介在なしで自由に会話。ConversationController による品質制御付き |
| 🧪 AutoGen | AutoGen GroupChat による多エージェント会話（実験的） |
| 📻 ニュースラジオ | RSSニュースをキャラクターが紹介・会話 |
| 📝 note記事 | A2Aパイプラインによるnote.com記事自動生成 |
| ⚙️ 設定 | TTS設定・システムプロンプト・note Cookie等 |

## キャラクター種別

| 種別 | 具体例 | バックエンド |
|------|--------|-------------|
| LM Studio キャラ | 東北ずん子、東北きりたん、四国めたん 等 | LM Studio（OpenAI互換 `localhost:1234`） |
| Noah | Noah | OpenClaw Gateway 経由（ChatGPT） |
| Hermes | Hermes | Groq API（Hermes 4.3 36B） |

---

## 自律会話

キャラ同士がユーザー介在なしで会話し続けるモード。

### ConversationController（会話品質制御）

`conversation_controller.py` が会話の流れを管理する。

**ConversationState** — 会話状態のトラッキング

- `current_scene` / `current_topic_terms`: 現在の話題
- `topic_age`: 同一話題の継続ターン数
- `repetition_score`: 繰り返し度
- `care_loop_score`: 気遣いワードの累積スコア
- `recent_full_texts`: 直近発言テキスト（類似度判定用）

**MovePlanner** — 発言ムーブの選択

ステージ（active / aging / closing）に応じた手の候補プールから、キャラごとの `CHAR_MOVE_BIAS` を加味して選択。同じムーブが連続しないよう抑制あり。

```python
# キャラごとの優先ムーブ設定例
CHAR_MOVE_BIAS = {
    "東北ずん子":   ["invite_other", "assign_role", "care_but_move"],
    "東北きりたん": ["tease", "introduce_conflict", "short_reaction"],
    "四国めたん":   ["summarize_and_close", "ask", "calm_reframe"],
    "Noah":        ["observe", "bridge", "soft_punchline"],
    ...
}
```

**OutputGuardrail** — 出力の事後検証

生成された返答に対して多要素スコアリングを行い、閾値（0.6）超えで最大1回リトライ。

| チェック項目 | スコア |
|---|---|
| 直前発話との高類似（Jaccard + 共有語数） | +0.5〜+0.6 |
| 他キャラの一人称（ボク/僕/俺 等）を使用 | +0.6 |
| 一般的な一人称を引用文脈外で使用 | +0.2 |
| 具体的行動の欠如 | +0.3 |
| 気遣いワードのみで終了 | +0.2 |

**same_speaker_guard** — 話者偏り防止

直近3件中2件以上登場した話者を次の候補から除外。

### Soulシステム

`~/.lmstudio_assistant/souls/{キャラ名}.md` に各キャラの自己認識・性格傾向・他キャラへの親密度スコアを保存。20ターンに1回LLMが自動更新。

- soul.md = 性格・傾向（長期記憶）
- `~/.lmstudio_assistant/episodes/{キャラ名}.json` = 出来事（短期記憶、直近5件をプロンプト注入）

### その他

- **X（Twitter）連携**: VoiceVoxキャラ公式アカウントの最近の投稿をsoul.mdに注入（毎週土曜 5:00 自動実行）
- **LM Studio競合対策**: セマフォで同時発言を直列化。note生成中はスキップ

---

## AutoGen（実験的）

`autogen_chat.py` の `StreamingGroupChat` で AutoGen GroupChat を別スレッドで動かし、発言をキューに流す。

- LM Studioキャラ: `llm_config` 経由で標準呼び出し（context window=直近5件）
- Noah / Hermes: `register_reply` でカスタム関数に差し替え
- 話者選択: balanced（最少参加回数優先）+ 直前話者除外

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
pip install streamlit requests trafilatura twikit pyautogen
```

### 必要なサービス

| サービス | URL | 用途 |
|---|---|---|
| LM Studio | `http://localhost:1234/v1` | ローカルLLM |
| VoiceVox | `http://localhost:50021` | TTS |
| AivisSpeech | `http://localhost:10101` | TTS（Noah等） |
| OpenClaw Gateway | `http://127.0.0.1:18789/v1` | Noah / ChatGPT |

```bash
streamlit run app.py
```

---

## ファイル構成

```
app.py                  メインアプリ（全タブ）
conversation_controller.py  自律会話の品質制御（MovePlanner / OutputGuardrail）
autogen_chat.py         AutoGen GroupChat 統合
chat.py                 LLM呼び出し・LM Studioセマフォ管理
speakers.py             キャラクター管理・Soul/Episode読み書き
tts.py                  TTS合成
news.py                 RSSニュース取得
note_api.py             note.com API連携
twitter_fetch.py        X公式アカウントからツイート取得・soul.md更新
twitter_setup.py        Xのcookie認証セットアップ
noah_config.json        Noahのキャラクター設定
speakers_all.json       VoiceVox/AivisSpeech 話者データキャッシュ
icons/                  キャラクターアイコン

~/.lmstudio_assistant/
  souls/                キャラクターSoulファイル（{name}.md）
  episodes/             キャラクターエピソード（{name}.json）
  prompts.json          システムプロンプト保存先
  auto_chat_log.json    自律会話ログ
```

## 作者

Fumiaki

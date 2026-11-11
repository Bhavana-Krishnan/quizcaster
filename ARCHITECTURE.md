# QuizCaster — Architecture

QuizCaster is an open-source, self-hosted app that runs an AI-hosted multiple-choice
quiz as a **vertical (9:16) YouTube live stream**. Viewers answer in live chat; the
first correct answer wins the question and climbs a live leaderboard.

Everything runs on the creator's own machine with their own keys:

| What                 | Whose                                   |
|----------------------|-----------------------------------------|
| Compute (rendering, encoding, agents) | Creator's computer        |
| LLM key              | Creator's Claude **or** OpenAI key      |
| YouTube access       | Creator's own Google Cloud OAuth client (ID + secret) |
| YouTube API quota    | Creator's own Google Cloud project      |

---

## 1. Components

```
                    ┌──────────────────────── Creator's computer ─────────────────────────┐
                    │                                                                     │
 Desktop dashboard ─┼─►  Local server (Starlette, 127.0.0.1:8765)                          │
 Mobile remote ─────┼─►    │  /api/*  settings, keys, questions, go-live, stop             │
  (same Wi-Fi, PIN) │      │  /events  Server-Sent Events (dashboard + overlay)            │
                    │      │                                                              │
                    │      ├── Quizmaster agent ── question loop, chat window, winners,     │
                    │      │                       "why?", leaderboard, commentary         │
                    │      ├── Broadcaster agent ─ YouTube auth, broadcast + stream,        │
                    │      │                       metadata, thumbnail, health, tech-break  │
                    │      │                                                              │
                    │      ├── Overlay page (/overlay) — HTML/CSS mascot, timer, board      │
                    │      └── Renderer: headless Chromium ─► JPEG frames ─► FFmpeg ─► RTMP ─┼─► YouTube
                    │                                                                     │
                    │  SQLite (data/quizcaster.db) — settings, encrypted keys, question    │
                    │  sets, stream history, quota ledger                                  │
                    └─────────────────────────────────────────────────────────────────────┘
```

No cloud backend exists. The mobile app is a **remote control**: it talks to the
desktop over the local network and never runs the stream itself.

## 2. Agents

There are two agents, built on a tiny state-graph engine (`quizcaster/graph.py`)
whose nodes are plain async functions. It is deliberately LangGraph-shaped
(nodes, edges, shared state) so it can later be swapped for LangGraph or any
custom graph framework without touching the node logic.

### Quizmaster
```
intro ─► question ─► options (comment window) ─► reveal ─► explain (+ "why?") ─► leaderboard ─┐
  ▲                                                                                             │
  └─────────────────────────────── next question / end ◄──────────────────────────────────────┘
```
* **options**: the comment window opens the moment options appear on our side.
  It closes at the **first correct answer** or after **30 s**, whichever is first.
* **First correct** is decided by YouTube's comment timestamp (`publishedAt`).
  Only comments posted after the options appeared count.
* Answers: a single letter A–D, case-insensitive (`b`, `B`, `B!` are fine).
  Only each viewer's **first** answer to a question counts.
* **reveal**: shows the winner's name (profanity-filtered) and a friendly,
  competitive one-liner. Nobody right → answer revealed with filler commentary.
* **explain**: the explanation is always shown. A short 10 s chat window
  listens for "why?"; if someone asks, the host calls them out and the
  explanation stays up longer. "why?" always refers to the latest revealed question.
* **leaderboard**: 1 point per first-correct answer, top 5 shown; also
  always visible as a small panel.

### Broadcaster
* Checks the channel has live streaming enabled and guides the user if not.
* Creates the broadcast + RTMP stream, binds them, sets title, description,
  tags, privacy, *made for kids = false*, and the AI-content disclosure flag
  (best effort — where the API supports it).
* Generates and uploads a template thumbnail.
* Starts the renderer, watches FFmpeg and the chat API, and switches the
  overlay to a **technical break** screen on failures while it retries.
* Ends the broadcast cleanly.

## 3. Cost & resource optimisation ("run as long as possible")

| Resource        | How it is kept low |
|-----------------|--------------------|
| LLM tokens      | Questions generated **in batches before going live** (user approves them). One call at stream start builds a pool of ~50 one-liners with a `{name}` slot. Explanations come with the questions. **Zero LLM calls during the stream** unless the user adds more questions. |
| YouTube quota   | Chat is polled **only** during the answer window and the short "why?" window, never between questions. Each API call is logged in a quota ledger; the Go Live screen estimates how many questions today's quota allows, and the stream ends gracefully before the quota runs out. |
| CPU / bandwidth | 720×1280 at 30 fps output from ~15 fps rendered frames, `veryfast` x264, ~2.5 Mbps. Resolution is configurable. |

The stream runs until approved questions run out, the user presses Stop, or
quota is nearly exhausted — whichever comes first. More questions can be
generated and approved from the dashboard while live.

## 4. Data & security

* SQLite file in `data/`. API keys and OAuth tokens are encrypted with
  Fernet (AES-128-CBC + HMAC) using a key stored in `data/secret.key`,
  created on first run with owner-only permissions.
* The server binds to `127.0.0.1` by default. Enabling the mobile remote binds
  to the LAN and requires a 6-digit PIN; remote clients can only view status,
  comments and leaderboard, start a stream from an approved set, and stop.
  They can never read or change keys or settings.

## 5. Modes

| Mode        | YouTube | Chat                      | Use |
|-------------|---------|---------------------------|-----|
| Preview     | No      | Simulated (type fake comments in the dashboard) | Check flow & look, no quota used |
| Test run    | Yes, **unlisted** | Real                | Rehearse on your real channel |
| Live        | Yes, your privacy choice | Real          | The real show |

## 6. Build plan (milestones)

1. Skeleton: config, SQLite, encryption, settings.
2. LLM: Claude/OpenAI key verification + model listing via REST, question
   generation & upload (CSV/JSON/XLSX), approval, commentary pool, moderation.
3. YouTube: OAuth (BYO client), live-enabled check, broadcast/stream lifecycle,
   chat polling, metadata, thumbnail, quota ledger.
4. Agents on the state graph.
5. Overlay + renderer + thumbnails.
6. Dashboard, Go Live wizard, preview mode, mobile remote.
7. Tests, end-to-end preview run, sample recording.

## 7. Later
Voice (TTS), background music, scheduling, Shorts clipping from the recording,
stream history analytics, native mobile packaging, multiple languages.

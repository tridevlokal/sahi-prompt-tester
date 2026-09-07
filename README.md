# AI Riya — Voice Agent Prompt Tester

Real-time voice tutor "Riya" for Indian English learners. Open the page in a browser,
pick a language and voice, press **Call**, and talk. Riya listens (STT), thinks (Gemini),
and replies out loud (TTS) with live captions, all over a single WebSocket.

Built to iterate quickly on system prompts, voices, and provider settings without
touching code: everything is editable from the admin and settings pages.

**Live demo (Cloud Run, Mumbai):** https://ai-riya-voice-tester-feyxsnvtrq-el.a.run.app

---

## Contents

1. [What it does](#what-it-does)
2. [Architecture](#architecture)
3. [Providers](#providers)
4. [Languages](#languages)
5. [Project layout](#project-layout)
6. [Run locally](#run-locally)
7. [Configuration](#configuration)
8. [HTTP and WebSocket API](#http-and-websocket-api)
9. [Deploy to Cloud Run](#deploy-to-cloud-run)
10. [How the pipeline works](#how-the-pipeline-works)
11. [Troubleshooting](#troubleshooting)

---

## What it does

- **Voice call in the browser.** Mic audio streams to the server; Riya's voice streams
  back. No app install, works on desktop and mobile Chrome over HTTPS.
- **Prompt library.** Save many system prompts, pick one per call (`/admin`).
- **Voice library.** Save voices per provider and language, pick one per call.
- **Language pre-pick.** Choose Hindi, Telugu, Tamil, Kannada, Malayalam, Bengali, or
  English before the call and Riya opens in that language and stays in it.
- **Runtime knobs.** Speed and temperature sliders on the call page; every provider and
  model setting on `/settings`, no restart needed.
- **Live captions.** Both what the learner said and what Riya is saying, in native script.
- **Turn-taking control.** Optional barge-in: the learner can interrupt Riya after a
  configurable number of words; noise and echo are ignored.

## Architecture

```
Browser (static/index.html)
  mic 16 kHz PCM ──protobuf over WebSocket──▶  FastAPI /ws  (server.py)
                                                   │
                                                   ▼  run_bot()  (bot.py)
                Pipecat pipeline:
                transport.input → Silero VAD → STT → TranscriptionGate
                → captions(user) → context → Gemini LLM → captions(bot)
                → TTS → transport.output → context
                                                   │
  speaker 24 kHz PCM ◀──protobuf over WebSocket────┘
  captions            ◀──JSON text frames (side-channel)
```

| Layer | Tech |
|---|---|
| Server | Python 3.13, FastAPI, Uvicorn, WebSocket |
| Voice framework | [Pipecat](https://github.com/pipecat-ai/pipecat) 1.1.0 |
| VAD | Silero (local, ONNX, no GPU) |
| LLM | Google Gemini (`gemini-3.1-flash-lite` default) |
| Client | Plain HTML/JS, protobuf frame serializer, no build step |
| Persistence | JSON files in `data/` (prompts, voices, settings) |
| Hosting | Docker → Google Cloud Run (`deploy_cloudrun.sh`); `fly.toml` kept for Fly.io |

## Providers

Switch providers from `/settings` (`TTS_PROVIDER`, `STT_PROVIDER`). Keys come from `.env`.

### Text-to-speech

| Provider | Setting value | Notes |
|---|---|---|
| **Google Cloud Chirp3-HD** | `google` | Native HD voices per Indian language. Voice id is composed per call as `<lang>-Chirp3-HD-<VoiceName>` (e.g. `te-IN-Chirp3-HD-Aoede`), so one voice name works across all languages. Needs a service account. |
| **ElevenLabs** | `elevenlabs` | Streaming `eleven_flash_v2_5` / `eleven_turbo_v2_5`, or `eleven_v3` over HTTP. Voice ids are per-voice, saved in the voice library. India residency endpoint supported. |

### Speech-to-text

| Provider | Setting value | Notes |
|---|---|---|
| **Sarvam saaras:v3** | `sarvam` | Indian code-mix (Hindi/Telugu + English) in native script. Best Telugu accuracy. Default mode `codemix`. |
| **ElevenLabs Scribe** | `elevenlabs` | `scribe_v2_realtime` over WebSocket. Auto-detect constrained to the supported language set so noise is not hallucinated into Russian/CJK. |
| **Google Cloud STT** | `google` | `chirp_2` (multilingual incl. Telugu, served from `us-central1`). |

### LLM

Google Gemini via the Gemini API key. Model, temperature, and max tokens are settings.

## Languages

| Picker | Code | TTS locale | Kickoff seed |
|---|---|---|---|
| हिंदी | `hin` | `hi-IN` | नमस्ते, चलिए हिंदी में बात करते हैं। |
| తెలుగు | `tel` | `te-IN` | నమస్కారం, తెలుగులో మాట్లాడదాం. |
| தமிழ் | `tam` | `ta-IN` | வணக்கம், தமிழில் பேசலாம். |
| ಕನ್ನಡ | `kan` | `kn-IN` | ನಮಸ್ಕಾರ, ಕನ್ನಡದಲ್ಲಿ ಮಾತನಾಡೋಣ. |
| മലയാളം | `mal` | `ml-IN` | നമസ്കാരം, മലയാളത്തിൽ സംസാരിക്കാം. |
| বাংলা | `ben` | `bn-IN` | নমস্কার, চলুন বাংলায় কথা বলি। |
| English | `eng` | `en-US` | Hello, let's talk in English. |

When a language is picked, three things happen (`bot.py`):

1. The TTS voice and STT language are set to that locale.
2. A hidden seed turn in that script is injected so the model locks onto the language.
3. A `[SESSION LANGUAGE]` directive is appended to the system prompt so Riya opens
   and stays in it (mixing English words for teaching is still allowed).

Without a pick, Riya asks the learner which language they prefer.

**Adding a language:** add it to `GOOGLE_LANG`, `GOOGLE_LANG_CODE`, `_ALLOWED_SCRIPT`,
`STT_SUPPORTED_LANGS`, `LANG_KICKOFF`, `LANG_NAMES` in `bot.py`, the
`ELEVENLABS_STT_LANGUAGE` choices in `settings_store.py`, and the `<select>` in
`static/index.html`. Chirp3-HD currently supports 53 locales including
`mr-IN`, `gu-IN`, `pa-IN`, `ur-IN`, `en-IN`.

## Project layout

```
sahi-prompt-tester/
├── server.py            FastAPI app: pages, prompts/voices/settings CRUD, /ws
├── bot.py               Pipecat pipeline: VAD, STT/TTS builders, gate, captions, run_bot()
├── settings_store.py    Settings schema (groups, defaults, choices) + data/settings.json I/O
├── storage.py           JSON persistence for prompts and voices
├── gemini_audio.py      Gemini audio helpers
├── static/
│   ├── index.html       Call page: prompt/voice/language pickers, sliders, captions
│   ├── admin.html       Manage saved prompts and voices
│   └── settings.html    Edit every provider/model/VAD/turn-taking setting
├── data/
│   ├── prompts.json     Saved system prompts
│   ├── voices.json      Saved voices (seeded from .env VOICE_ID_* on first run)
│   └── settings.json    Overrides on top of schema defaults
├── Dockerfile           python:3.13-slim, CPU-only, runs as uid 1000
├── deploy_cloudrun.sh   One-command Cloud Run deploy (reads .env at deploy time)
├── fly.toml             Fly.io config (legacy)
├── requirements.txt     Pinned deps
├── .env.example         Template for .env
├── GOOGLE_TTS_PRD.md    Spec for multilingual Chirp3-HD TTS
├── STAGING_PARITY.md    Notes for matching this app's behaviour in staging
└── PROJECT_DOCS.md      Older in-depth notes (partly outdated, see this README first)
```

## Run locally

Requirements: Python 3.13, a Gemini API key, and keys for whichever TTS/STT providers
you enable.

```bash
python3.13 -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env            # fill in keys
# For Google TTS/STT: drop the service-account JSON at ./gcp-service-account.json
venv/bin/python server.py       # http://127.0.0.1:7860
```

Pages:

| URL | Purpose |
|---|---|
| `/` | Call page |
| `/admin` | Prompts and voices |
| `/settings` | Provider, model, VAD, turn-taking settings |

## Configuration

### Secrets (`.env`, never committed)

| Variable | Used for |
|---|---|
| `GOOGLE_API_KEY` | Gemini LLM |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to service-account JSON for Google TTS/STT (default `gcp-service-account.json`) |
| `GCP_SERVICE_ACCOUNT_JSON` | Alternative for hosted runs: the JSON content itself; written to disk on boot |
| `ELEVENLABS_API_KEY` | ElevenLabs STT/TTS |
| `ELEVENLABS_TTS_API_KEY` | Optional global-only key for `eleven_v3` |
| `ELEVENLABS_BASE_URL` | Optional residency endpoint (e.g. India) |
| `SARVAM_API_KEY` | Sarvam STT |
| `VOICE_ID_ENGLISH/TAMIL/TELUGU/KANNADA` | Seed the voice library on first run |
| `HOST`, `PORT` | Bind address (default `127.0.0.1:7860`; Docker sets `0.0.0.0:8080`) |

### Settings (`/settings`, stored in `data/settings.json`)

| Group | Keys |
|---|---|
| LLM (Gemini) | `GEMINI_MODEL`, `GEMINI_TEMPERATURE`, `GEMINI_MAX_TOKENS` |
| Providers | `TTS_PROVIDER`, `STT_PROVIDER` |
| Google Cloud audio | `GOOGLE_TTS_VOICE` (Chirp3-HD voice name, e.g. Aoede/Kore/Charon), `GOOGLE_STT_MODEL` |
| Sarvam STT | `SARVAM_STT_MODEL`, `SARVAM_STT_MODE` (`codemix`/`transcribe`/`verbatim`) |
| ElevenLabs TTS | model, language, speed, stability, similarity, style, speaker boost, text normalisation |
| ElevenLabs STT | `ELEVENLABS_STT_MODEL`, commit strategy, language lock |
| ElevenLabs region | `ELEVENLABS_REGION` |
| VAD (Silero) | `VAD_CONFIDENCE`, `VAD_START_SECS`, `VAD_STOP_SECS`, `VAD_MIN_VOLUME` |
| Turn-taking | `ALLOW_INTERRUPTIONS`, `INTERRUPT_WORDS` |

Settings are read at the start of each call, so changes apply to the next call without
a restart. Per-call overrides from the call page: `speed` (0.7–1.2), `temperature` (0–2).

## HTTP and WebSocket API

| Method | Route | Purpose |
|---|---|---|
| GET | `/`, `/admin`, `/settings` | Pages |
| GET/POST | `/api/prompts` | List / create prompts |
| GET/PUT/DELETE | `/api/prompts/{id}` | Read / update / delete a prompt |
| GET/POST | `/api/voices` | List / create voices |
| GET/PUT/DELETE | `/api/voices/{id}` | Read / update / delete a voice |
| GET/PUT | `/api/settings` | Read / update settings |
| GET | `/api/gemini/voices` | Gemini prebuilt voice names |
| GET | `/api/smallest/voices` | Smallest.ai voice list (legacy provider) |
| WS | `/ws` | Voice call |

**`/ws` handshake.** The first client message is JSON:

```json
{
  "system_prompt": "You are Riya ...",
  "voice_id": "Aoede",
  "language": "tel",
  "speed": 1.0,
  "temperature": 0.8
}
```

`system_prompt` and `voice_id` are required. After that, binary frames are protobuf
audio in both directions; text frames are captions:
`{"type": "user" | "bot", "text": "..."}`.

## Deploy to Cloud Run

The app runs as one container on Google Cloud Run in `asia-south1` (Mumbai).
Cloud Run supports long-lived WebSockets; serverless platforms like Vercel do not.

```bash
# one-time: gcloud auth login && gcloud config set project <project-id>
bash deploy_cloudrun.sh
```

The script:

1. Reads keys from `.env` and `gcp-service-account.json` and passes them to Cloud Run
   as environment variables (a temp file, deleted after deploy).
2. Builds the image with Cloud Build from the `Dockerfile`.
3. Deploys with 1 GiB RAM, 1 vCPU, CPU boost, `min-instances 0`, 60-minute request
   timeout, session affinity, public access.

Redeploy after any code or settings change by running the script again (5–7 minutes).
Set `--min-instances 1` in the script to remove cold starts during demos.

**Cost:** billed per request time; idle costs nothing at `min-instances 0`. The public
link uses your Gemini/ElevenLabs/Sarvam/Google quotas, so share it with care.

## How the pipeline works

- **VAD (Silero)** decides when the learner starts and stops speaking. Thresholds are
  settings; `VAD_STOP_SECS` controls how long a pause ends a turn.
- **STT** streams transcripts. A script allowlist (Devanagari, Telugu, Tamil, Kannada,
  Malayalam, Bengali, Latin) drops any transcript dominated by a foreign script, which
  is how echo and noise hallucinations (Chinese, Russian, ...) are filtered without
  turning off multilingual auto-detect.
- **TranscriptionGate** is the turn manager. While Riya is silent, any transcript with
  at least one word goes to the LLM. While Riya is speaking, a transcript only
  interrupts her if `ALLOW_INTERRUPTIONS` is on and it has at least `INTERRUPT_WORDS`
  words; shorter fragments (echo, "hmm") are ignored. A short cooldown after Riya stops
  suppresses her own tail-end echo.
- **Captions side-channel** mirrors user and bot text to the browser as JSON without
  affecting the audio path.
- **LLM** gets the system prompt plus the language directive and the hidden kickoff
  seed; the context aggregator keeps the conversation history for the call.
- **TTS** streams audio back as it is generated. For Chirp3-HD, a lowercase
  standalone "us" is respelled "uss" before synthesis because Chirp reads it as
  "U.S." and supports no SSML override; captions keep the original text.

## Troubleshooting

| Symptom | Check |
|---|---|
| Riya replies in Roman script / wrong accent for Telugu, Tamil, ... | Pick the language before the call. Confirm the log line `[tts] Google Cloud TTS voice=<lang>-Chirp3-HD-...` shows the right locale. |
| Riya keeps getting interrupted by her own voice | Turn `ALLOW_INTERRUPTIONS` off, or raise `INTERRUPT_WORDS` / `VAD_MIN_VOLUME`. Use headphones. |
| Nothing transcribed | Confirm `STT_PROVIDER` key is in `.env`; check the server log for `[stt]` lines. |
| Google TTS/STT auth error | `gcp-service-account.json` present locally, or `GCP_SERVICE_ACCOUNT_JSON` set on the host. Text-to-Speech and Speech-to-Text APIs must be enabled on the project. |
| First call on the hosted link is slow | Cold start at `min-instances 0`; wait 20–30 s or set `min-instances 1`. |
| Cloud Run build fails | Run `gcloud builds list --region asia-south1` and open the log link. |

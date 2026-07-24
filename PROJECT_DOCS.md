# AI Riya — Voice Agent · Full Project Documentation

> English-teaching voice agent "Riya". Browser se mic pe baat karo, Riya
> real-time voice mein reply karti hai. Ye doc har component, config value, aur
> design-decision (khaaskar **interruption mechanism**) detail mein cover karta hai.

---

## 1. Overview

| | |
|---|---|
| **Project path** | `/Users/tridevmaurya/sahi-prompt-tester` |
| **Kya hai** | Real-time voice AI ("Riya") jo English sikhati hai, Hindi/English code-mix samajhti hai |
| **Framework** | [Pipecat](https://github.com/pipecat-ai/pipecat) **1.1.0** |
| **Python** | 3.13.12 (project ke apne `venv/` mein) |
| **Server** | FastAPI + Uvicorn, WebSocket-based |
| **Client** | Plain HTML/JS (`static/index.html`), protobuf over WebSocket |
| **Audio path** | Browser mic → WS → Pipecat pipeline → WS → browser speaker |

### Voice pipeline (high level)
```
🎤 Browser mic
   │  (16kHz PCM, protobuf frames over WebSocket)
   ▼
[Pipecat] VAD (Silero) → STT (Sarvam) → Gate → LLM (Gemini) → TTS (ElevenLabs)
   │  (24kHz audio, protobuf frames over WebSocket)
   ▼
🔊 Browser speaker   +   captions (JSON text frames, side-channel)
```

---

## 2. Tech stack & services

| Layer | Service | Model / setting | Kahan configure |
|---|---|---|---|
| **STT** (speech→text) | **Sarvam AI** | `saaras:v3`, mode `transcribe`, lang auto-detect | `bot.py` |
| **LLM** (brain) | **Google Gemini** | `gemini-2.5-flash` | `bot.py` (`GEMINI_MODEL`) |
| **TTS** (text→speech) | **ElevenLabs** | per-voice ID, India residency | `bot.py` |
| **VAD** (voice detect) | **Silero** (local ML) | custom `VADParams` | `bot.py` |
| **Transport** | FastAPI WebSocket | protobuf serializer | `bot.py` / `server.py` |

> **History note:** STT pehle **Deepgram** (`nova-3`) tha, baad mein **Sarvam** pe
> switch kiya gaya. `DEEPGRAM_API_KEY` `.env` mein abhi bhi hai par use nahi hota.

---

## 3. File-by-file

```
sahi-prompt-tester/
├── bot.py            ← Pipecat voice pipeline (CORE — sab logic yahan)
├── server.py         ← FastAPI app: static pages, prompts/voices CRUD, /ws
├── storage.py        ← JSON file persistence (prompts.json, voices.json)
├── .env              ← API keys + voice IDs + host/port
├── .env.example      ← template
├── data/
│   ├── prompts.json  ← saved system-prompts
│   └── voices.json   ← saved voices (first run pe .env se seed)
├── static/
│   ├── index.html    ← test UI + browser audio client (mic/speaker/protobuf)
│   └── admin.html    ← prompts/voices manage karne ka admin UI
├── venv/             ← project virtualenv (Pipecat 1.1.0, sarvamai, etc.)
└── PROJECT_DOCS.md   ← ye file
```

### `server.py` — routes
| Method | Route | Kaam |
|---|---|---|
| GET | `/` | `static/index.html` (test page) |
| GET | `/admin` | `static/admin.html` |
| GET/POST/GET/PUT/DELETE | `/api/prompts[/{id}]` | prompts CRUD |
| GET/POST/GET/PUT/DELETE | `/api/voices[/{id}]` | voices CRUD |
| WS | `/ws` | voice call → `run_bot()` |

**`/ws` flow:** client connect → pehla JSON message bhejta hai
`{system_prompt, voice_id, speed, temperature}` → server validate + clamp karta hai
(`speed` 0.7–1.2, `temperature` 0.0–1.5) → `run_bot(...)` call hota hai.

### `storage.py`
- `data/prompts.json` & `data/voices.json` — auto-create on first read.
- `voices.json` pehli baar `.env` ke `VOICE_ID_*` se seed hota hai (english/tamil/telugu/kannada).
- Har entry: `id, name, ...fields, created_at, updated_at`.

---

## 4. `bot.py` — the pipeline (line-by-line intent)

### 4.1 Constants
```python
GEMINI_MODEL = "gemini-2.5-flash"
INPUT_SAMPLE_RATE  = 16000   # mic se aane wala audio
OUTPUT_SAMPLE_RATE = 24000   # Riya ki awaaz (ElevenLabs)
```

### 4.2 VAD — Silero Voice Activity Detection
```python
VADParams(
    confidence = 0.7,   # Silero NN ka speech-probability threshold.
                        #   non-speech (fan, AC, taps) reject karta hai.
    start_secs = 0.4,   # 400ms+ continuous speech = tab "speech start". Short
                        #   blips / cough / single-word ignore.
    stop_secs  = 0.8,   # 0.8s silence ke baad "speech stop" (turn end trigger).
                        #   kam rakha (1.2 se) → kam latency, Riya jaldi respond.
    min_volume = 0.5,   # isse dheemi awaaz = silence maan lo. faint/distant/
                        #   echo reject, par normal bolne wali awaaz register ho.
)
```
**Kahan use hota hai:** dono jagah — `transport` ke `vad_analyzer` mein AUR
explicit `VADProcessor` mein (pipeline mein pehla processor).

> ⚙️ **Tuning:** zyada strict chahiye (background bhi cut ho) → `confidence` 0.8,
> `min_volume` 0.6. Riya late lage → `stop_secs` 0.6.

### 4.3 STT — Sarvam
```python
SarvamSTTService(
    api_key=os.getenv("SARVAM_API_KEY"),
    mode="transcribe",
    sample_rate=16000,
    settings=SarvamSTTService.Settings(model="saaras:v3"),
)
```
- `saaras:v3` → Indian languages + Hindi/English code-switching.
- language auto-detect (set nahi kiya).
- **Sarvam sirf FINAL transcript deta hai** (interim/partial nahi) → live
  partial captions nahi dikhte, poori baat khatam hone pe hi transcript aata hai.

### 4.4 LLM — Gemini
```python
GoogleLLMService(
    api_key=os.getenv("GOOGLE_API_KEY"),
    settings=GoogleLLMSettings(
        model="gemini-2.5-flash",
        system_instruction=system_prompt,   # /ws config se aata hai
        temperature=temperature,            # default 0.7, browser slider se
    ),
)
```

### 4.5 TTS — ElevenLabs
```python
ElevenLabsTTSService(
    api_key=os.getenv("ELEVENLABS_API_KEY"),
    url=os.getenv("ELEVENLABS_BASE_URL", "wss://api.elevenlabs.io"),
    settings=ElevenLabsTTSSettings(voice=voice_id, speed=speed),
)
```
- `voice_id` aur `speed` browser se aate hain (`/ws` config).
- `.env` mein India residency URL: `wss://api.in.residency.elevenlabs.io`.

### 4.6 Pipeline order (frame ka raasta)
```python
Pipeline([
    transport.input(),          # mic audio in
    vad_processor,              # Silero VAD: speech start/stop detect
    stt,                        # Sarvam: audio → text (TranscriptionFrame)
    TranscriptionGate(),        # ⭐ noise/echo drop + word-gated interrupt
    TranscriptionLogger(),      # console pe [stt] '...' log
    captions_user,              # user transcript → browser (JSON side-channel)
    context_aggregator.user(),  # user turn → LLM context; turn-taking yahin
    llm,                        # Gemini: context → reply text (LLMTextFrame)
    captions_bot,               # bot text → browser (JSON side-channel)
    tts,                        # ElevenLabs: text → audio
    transport.output(),         # audio out → speaker; BotStarted/Stopped frames
    context_aggregator.assistant(),  # bot reply → context (history)
])
```

---

## 5. ⭐ TURN-TAKING & INTERRUPTION — full detail

Ye project ka sabse important (aur tricky) part hai. Teen layers milke kaam karti hain.

### 5.1 Layer A — Turn-start / turn-stop strategies
```python
UserTurnStrategies(
    start=[
        VADUserTurnStartStrategy(enable_interruptions=False),
        TranscriptionUserTurnStartStrategy(use_interim=False,
                                           enable_interruptions=False),
    ],
    stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
)
```

**"Turn start" kya hai:** Pipecat ko batana ki "user ne bolna shuru kiya" → ek
naya user-turn khulta hai (jismein transcript collect hota hai).

- **`VADUserTurnStartStrategy`** — jab Silero VAD bole "speech started" → turn start.
- **`TranscriptionUserTurnStartStrategy`** — jab STT se transcript aaye → turn start.
  Ye **reliable fallback** hai: testing mein paaya ki Silero VAD akele consistently
  fire nahi ho raha tha (browser audio level pe depend), to transcript se bhi turn
  start hota hai — warna Riya kabhi reply hi nahi deti thi.

**"Turn stop":** `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)` —
0.6s tak koi nayi speech nahi → turn complete → LLM ko bhejo.

#### 🔑 `enable_interruptions=False` — sabse important seekh
Pipecat **1.1.0** mein `PipelineParams(allow_interruptions=...)` ka **koi asar
nahi** hai (us version mein wo field hi nahi hai — silently ignore hota hai).
Interruption **poori tarah** har turn-start strategy ke `enable_interruptions`
flag se control hoti hai, jiska **default `True`** hai.

- Default (`True`): jaise hi turn start ho → strategy `broadcast_interruption()`
  call karti hai → Riya turant ruk jaati hai. **Isi wajah se** "Riya har chhoti
  baat/noise pe cut ho jaati thi".
- Humne dono pe `False` set kiya → **strategy khud kabhi interrupt nahi karti**.
  Interruption ab **sirf manually** `TranscriptionGate` se hoti hai (niche).

> Matlab: turn to start hote hain (taaki reply aaye), par automatic interruption
> band hai — interrupt ka decision humare gate ke haath mein hai.

### 5.2 Layer B — `PipelineTask`
```python
PipelineTask(pipeline, params=PipelineParams(allow_interruptions=False))
```
> `allow_interruptions=False` yahan **1.1.0 mein effectively no-op hai** (field
> ignore hoti hai). Code mein documentation/clarity ke liye chhoda hai. Asli
> control Layer A (`enable_interruptions`) + Layer C (gate) mein hai.

### 5.3 Layer C — `TranscriptionGate` (custom processor) — asli dimaag
Position: pipeline mein STT ke **turant baad**, taaki har transcript turn banne
se pehle yahan se filter ho.

**State track karta hai:**
- `_bot_speaking` — `BotStartedSpeakingFrame` / `BotStoppedSpeakingFrame` se
  (ye frames output-transport se **upstream bhi push** hote hain, isliye gate ko
  mil jaate hain).
- `_bot_stopped_at` — Riya kab ruki (echo cooldown ke liye).

**Tunable thresholds:**
```python
INTERRUPT_WORDS   = 5     # "4 words se zyada" → 5 ya usse zyada
MIN_TURN_WORDS    = 2     # Riya chup ho to: isse kam words = noise → drop
ECHO_COOLDOWN_SECS = 0.8  # Riya ruकne ke baad itni der short-transcripts drop
```

**Decision logic (har `TranscriptionFrame` pe):**

```
w = transcript mein words ki ginti

┌─ Riya BOL rahi hai (_bot_speaking == True)?
│    ├─ w >= 5  → 🔴 INTERRUPT: broadcast_interruption() → Riya rukti hai,
│    │            phir transcript aage → wo iska reply deti hai.
│    └─ w <= 4  → 🔇 ignore (drop): Riya apni baat jaari rakhti hai.
│                 (chhote words, "hmm", "okay", noise, echo)
│
└─ Riya CHUP hai (user ki baari)?
     ├─ w <  2  → drop (single-word noise / trailing echo)
     └─ w >= 2  → ✅ aage bhejo → normal reply
```

**Manual interruption kaise hoti hai:** gate `self.broadcast_interruption()`
call karta hai → ye ek `InterruptionFrame` upstream + downstream push karta hai.
Output transport ise **unconditionally** handle karke Riya ki current TTS
playback turant rok deta hai. (1.1.0 mein InterruptionFrame pe koi
`allow_interruptions` gate nahi — hamesha rukta hai.)

### 5.4 Net behaviour (user ke liye)
| Situation | Result |
|---|---|
| Riya bol rahi + aap **5+ words** bole | Riya **ruk jaati hai**, aapko reply deti hai |
| Riya bol rahi + aap **≤4 words / "hmm" / noise** | Riya **ignore** karke bolti rehti hai |
| Riya chup + aap proper baat (2+ words) | reply |
| Riya chup + single-word noise | ignore |

---

## 6. ⚠️ Echo problem & HEADPHONE (zaroori)

**Problem:** Jab Riya bol rahi hoti hai, uski awaaz speaker se nikal ke **wapas
mic mein** aa sakti hai. Sarvam usko transcribe kar deta hai — kabhi-kabhi 5+
words ka (aur garble ho ke unrelated English words ban jaata hai, jaise Hindi
TTS → `"spots each with their own individual"`). Isse:
- Riya **khud ko interrupt** kar sakti hai (5+ word echo).
- Reply-loop ban sakta hai (Riya bole → echo → reply → echo → ...).

**Kyun software se 100% fix nahi:** echo aur asli user-speech dono Riya ke
bolte waqt aate hain. Content-match se echo pakadna reliable nahi (STT garble
kar deta hai). Sirf "Riya ke bolte waqt aaya" reliable signal hai — par usse
drop karein to **asli interruption bhi block** ho jaaye.

**✅ Pakka hal: HEADPHONE use karo.** Tab Riya ki awaaz mic tak pahunchti hi
nahi → echo zero → sab clean. Browser-side mitigations (niche) help karte hain
par headphone best hai.

**Bina headphone ke options (`bot.py` mein):**
- `INTERRUPT_WORDS` 7–8 kar do → chhote echo se na ruke (lekin tab interrupt ke
  liye lambi baat bolni padegi).
- `ECHO_COOLDOWN_SECS` badha do.

---

## 7. Browser client — `static/index.html`

### 7.1 Mic capture (`getUserMedia`)
```js
navigator.mediaDevices.getUserMedia({
  audio: {
    channelCount: 1,
    echoCancellation: true,   // Riya ki awaaz mic se wapas na aaye (echo kam)
    noiseSuppression: true,   // fan/AC/traffic background kaate
    autoGainControl: true,    // audio loud rakhe — warna VAD/STT trigger hi nahi hote
    sampleRate: 16000,
  },
});
```
> ⚠️ **Note:** `autoGainControl` ko `false` karne ki koshish ki thi (sirf loud
> awaaz ke liye), par usse audio itna dheema ho gaya ki **VAD/STT trigger hi
> nahi hote the** → Riya reply nahi deti thi. Isliye `true` rakhna zaroori hai.

### 7.2 Audio processing chain
```
micSource → micGain (gain = 2.0) → ScriptProcessor(2048) → 16kHz PCM → protobuf → WS
```
- **`micGain = 2.0`** → audio boost, taaki Silero VAD ko awaaz dikhe. (1.0 = no
  boost rakha tha to VAD fire nahi hua.)
- ScriptProcessorNode buffer 2048 samples (~43ms @ 48kHz).
- Peak-level meter har ~2s console pe (`SILENT` / `quiet` / `audio detected`).

### 7.3 Wire protocol
- **Audio:** protobuf-encoded frames (binary) — schema `index.html` mein inline
  (`pipecat/frames/frames.proto` mirror), protobufjs CDN se load.
- **Captions:** JSON text frames (side-channel) — `{type, text}`:
  `user_interim` / `user` / `bot`. Browser binary (audio) vs string (caption)
  se distinguish karta hai.
- **Playback:** incoming audio frame → `playPCM(int16, 24000)` → Web Audio se bajta hai.

### 7.4 Connect flow
1. Prompt + voice select (sliders: speed 0.7–1.2, temperature).
2. **Start** → `startMic()` (permission + AudioContext) → `new WebSocket('/ws')`.
3. On open → bhejta hai: `{system_prompt, voice_id, speed, temperature}`.
4. Server `run_bot()` chalu → Riya `LLMRunFrame` se opening line bolti hai.

---

## 8. `.env` reference

```bash
GOOGLE_API_KEY=...          # Gemini (working key; AQ. format bhi valid hai)
SARVAM_API_KEY=...          # Sarvam STT
ELEVENLABS_API_KEY=...      # ElevenLabs TTS (residency key)
ELEVENLABS_BASE_URL=wss://api.in.residency.elevenlabs.io   # India residency
DEEPGRAM_API_KEY=...        # ab use nahi (Sarvam pe switch kiya)

VOICE_ID_ENGLISH=...        # ElevenLabs voice IDs (voices.json seed)
VOICE_ID_TAMIL=...
VOICE_ID_TELUGU=...
VOICE_ID_KANNADA=...

HOST=127.0.0.1
PORT=7860
```

> **Gemini billing gotcha:** `429 RESOURCE_EXHAUSTED "prepayment credits
> depleted"` aaye → key valid hai par us project ke credits khatam. AI Studio
> (https://ai.studio/projects) pe recharge karo ya dusre project ka key lo.

---

## 9. Run karna

```bash
cd /Users/tridevmaurya/sahi-prompt-tester
venv/bin/python server.py
```
Phir browser: **http://127.0.0.1:7860**

> ⚠️ **HAMESHA `venv/bin/python`** — plain `python` (pyenv) alag Pipecat
> version (0.0.107) hai aur usme `sarvamai` install nahi → crash.

**Port busy** (`address already in use`):
```bash
lsof -ti:7860 | xargs kill -9
```
**Code change ke baad** server **restart** zaroori (`Ctrl+C` + dobara run) —
Python startup pe hi `bot.py` padhta hai. Browser bhi **hard refresh**
(`Cmd+Shift+R`) jab `index.html` change ho.

---

## 10. Tuning cheat-sheet

| Chahiye | Badlo (`bot.py`) |
|---|---|
| Interrupt ke liye zyada/kam words | `TranscriptionGate.INTERRUPT_WORDS` (abhi 5) |
| Idle pe chhoti baat bhi sune | `TranscriptionGate.MIN_TURN_WORDS` (abhi 2) |
| Echo se khud interrupt ho rahi | `INTERRUPT_WORDS` 7–8 ya **headphone** |
| Background noise reject (aur strict) | `VADParams.confidence` 0.8, `min_volume` 0.6 |
| Riya late respond karti hai | `VADParams.stop_secs` 0.6, `user_speech_timeout` 0.4 |
| Riya aapki awaaz miss karti hai | `index.html` `micGain` 2.5, `VADParams.min_volume` 0.4 |

**Logs mein gate ka behaviour:**
```
[gate] INTERRUPT (6w): '...'                 → 5+ words, Riya ruki
[gate] ignored while Riya speaking (2w): '...'→ chhota, Riya bolti rahi
[gate] dropped (short noise, 1w): '...'       → idle noise drop
[stt] '...'                                   → accepted transcript
```

---

## 11. Design decisions / history (kyun)

1. **Deepgram → Sarvam STT:** Indian-language accuracy ke liye Sarvam `saaras:v3`.
   Trade-off: Sarvam interim transcripts nahi deta → live partial captions gaye.
2. **Interruption ka asli switch:** `PipelineParams.allow_interruptions` 1.1.0
   mein no-op nikla; asli control turn-start strategies ka `enable_interruptions`
   hai → dono pe `False`.
3. **VAD over-tightening:** "sirf loud voice" ke chakkar mein `autoGainControl
   off` + `min_volume 0.7` kiya → VAD trigger hi nahi hua, no reply. Wapas
   balance kiya (`AGC on`, `gain 2.0`, `min_volume 0.5`).
4. **TranscriptionGate:** random-noise pe series-replies aur echo loop rok ne ke
   liye custom processor — word-count + bot-speaking state se filter + manual
   word-gated interruption (`broadcast_interruption`).
5. **Echo:** software se fully solvable nahi (STT echo ko garble karta hai) →
   headphone recommended, gate thresholds tunable.

---

*Last updated: 2026-06-05*

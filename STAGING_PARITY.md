# Staging Parity Spec — make staging behave like local "Riya"

> Goal: replicate the local laptop app's behaviour in staging.
> Two layers: **(A) env values** (config form) and **(B) code/behaviour** (custom logic in `bot.py`).
> The env values are easy. The **code/behaviour** part (especially the word-gated interruption)
> is what actually makes local feel good — env alone will NOT get you there.

---

## A. ENV VALUES (set these in the staging config form)

| Field | Value | Notes |
|---|---|---|
| `GEMINI_MODEL` | `gemini-2.5-flash` | Full flash, NOT flash-lite. Better reasoning/quality. |
| `GEMINI_TEMPERATURE` | `0.7` | Local default. (Clamp range 0.0–1.5.) |
| `GEMINI_MAX_TOKENS` | `500` | Local does not set it explicitly; 500 is a safe default. |
| `SARVAM_MODEL_ID` | `saaras:v3` | Same as local. |
| `SARVAM_STT_MODE` | `transcribe` | Local mode (NOT codemix). |
| `ELEVENLABS_WS_URL` | `wss://api.in.residency.elevenlabs.io` | India residency. |
| `ELEVENLABS_API_URL` | `https://api.in.residency.elevenlabs.io/v1` | Must match WS region. |
| `ELEVENLABS_MODEL_ID` | `eleven_flash_v2_5` | Lowest latency; local relies on this default. |
| `ELEVENLABS_TTS_SPEED` | `1.0` | Local default. Use `0.85` only if you deliberately want a slower tutor. |
| `ELEVENLABS_TTS_STABILITY` | `0.5` | Local doesn't set it → ElevenLabs default ≈ fine. |
| `ELEVENLABS_TTS_SIMILARITY_BOOST` | `0.8` | Local doesn't set it → fine. |
| `ELEVENLABS_TTS_STYLE` | `0.0` | Local doesn't set it. |
| `ELEVENLABS_TTS_USE_SPEAKER_BOOST` | on | Local doesn't set it. |
| `ELEVENLABS_TTS_APPLY_TEXT_NORMALIZATION` | `on` | Good for Hindi-English code-mix. |
| `ELEVENLABS_VOICE_ID` | English: `vGtqOwQ6rqrfUPTqvAGX` / Tamil: `RDWdsTU6N02BFftbIEAp` | Pick per the language you want. |
| `VOICE_AGENT_VAD_CONFIDENCE` | `0.8` | = local `VADParams.confidence`. Raised to reject faint/non-speech noise. |
| `VOICE_AGENT_VAD_HOLD_SECS` | `0.8` | = local `VADParams.stop_secs`. |
| `VOICE_AGENT_VAD_MIN_VOLUME` | `0.7` | = local `VADParams.min_volume`. Raised so low-audible sounds never reach STT. |
| `VOICE_AGENT_VAD_START_FRAMES` | `19` | = local `start_secs=0.6` (600 ms ÷ ~32 ms/window ≈ 19 frames). Staging default 3 is far too twitchy. |
| `VOICE_AGENT_USER_TURN_STOP_TIMEOUT_SECS` | `2.5` | = local `user_turn_stop_timeout`. |
| `VOICE_AGENT_SMART_TURN_CPU_COUNT` | `2` | N/A in local (no SmartTurn). Leave default. |

**VAD source of truth in local (`bot.py`):**
```python
vad_params = VADParams(
    confidence=0.8,    # speech probability — reject background/non-speech (raised 0.7→0.8)
    start_secs=0.6,    # 600ms sustained → ignore faint blips/cough/echo (raised 0.4→0.6)
    stop_secs=0.8,     # 0.8s silence → turn end
    min_volume=0.7,    # reject faint/distant/low-audible sounds before STT (raised 0.5→0.7)
)
```

---

## B. CODE / BEHAVIOUR (port these into staging — NOT in the env form)

This is the important part. These four pieces are custom logic in local `bot.py`.

### B1. Word-gated interruption (`TranscriptionGate`) — THE key feature

**Design idea:** the framework's automatic interruption is fully DISABLED so that noise,
echo, coughs and filler never cut Riya off. A custom gate then decides *manually* when an
interruption is warranted, based on **word count**.

Current tuned values:
| Constant | Value | Meaning |
|---|---|---|
| `INTERRUPT_WORDS` | `2` | While Riya is speaking, a user transcript of **2+ words** interrupts her. 1 word is ignored. |
| `MIN_TURN_WORDS` | `1` | While idle (user's turn), **1+ word** triggers a reply. Only empty/0-word is dropped (so single words ARE heard). |
| `ECHO_COOLDOWN_SECS` | `0.8` | For 0.8 s after Riya stops, sub-threshold transcripts are treated as trailing echo and dropped. |

**Exact behaviour:**
- **Riya speaking + user says ≥ 2 words** → `broadcast_interruption()` + forward transcript → she stops and replies.
- **Riya speaking + user says 1 word / noise / echo** → ignored, she keeps talking.
- **Riya idle + user says ≥ 1 word** → forward → reply (single words now heard).
- **Riya idle + 0 words** → dropped.

**Full code to port (`TranscriptionGate`):**
```python
class TranscriptionGate(FrameProcessor):
    INTERRUPT_WORDS = 2      # 2+ words while bot speaking → interrupt
    MIN_TURN_WORDS = 1       # idle: 1+ word triggers reply (only empty dropped)
    ECHO_COOLDOWN_SECS = 0.8 # post-stop window where short transcripts = echo → drop

    def __init__(self):
        super().__init__()
        self._bot_speaking = False
        self._bot_stopped_at = 0.0  # monotonic seconds

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._bot_stopped_at = time.monotonic()

        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            w = len(text.split())

            if self._bot_speaking:
                if w >= self.INTERRUPT_WORDS:
                    await self.broadcast_interruption()
                    await self.push_frame(frame, direction)
                    return
                return  # ignore short speech while Riya talks

            recently_stopped = (
                time.monotonic() - self._bot_stopped_at < self.ECHO_COOLDOWN_SECS
            )
            if w < self.MIN_TURN_WORDS:
                return  # drop empty / trailing echo

        await self.push_frame(frame, direction)
```

> ⚠️ Staging uses **SmartTurn** for turn/interruption decisions. SmartTurn will interrupt on
> raw turn-detection and gets tripped by noise. To get local behaviour you must EITHER disable
> SmartTurn's free interruption and insert this gate, OR replicate the same word-gate decision
> on top of SmartTurn's signal.

### B2. Interruptions disabled at the framework level

Local disables every automatic interruption path; the gate above is the ONLY thing allowed to
interrupt. Three switches:
```python
turn_strategies = UserTurnStrategies(
    start=[
        VADUserTurnStartStrategy(enable_interruptions=False),
        TranscriptionUserTurnStartStrategy(use_interim=False, enable_interruptions=False),
    ],
    stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)],
)
...
task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=False))
```
- `allow_interruptions=False` at the pipeline level.
- `enable_interruptions=False` on BOTH turn-start strategies (this is the real flag — pipeline
  flag alone is not enough; each strategy defaults to `True`).

### B3. Turn-taking mechanism (VAD + transcription, no SmartTurn)

- **Start of user turn:** VAD strategy (clean case) + Transcription-fallback strategy (Silero
  alone wasn't firing reliably, so Sarvam transcript also starts a turn — otherwise Riya
  sometimes never replied).
- **Stop of user turn:** `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.6)`.
- **Safety bound:** `user_turn_stop_timeout=2.5` (gives STT headroom for short utterances so the
  turn doesn't end with `strategy=None` and orphan the transcript).

```python
context_aggregator = LLMContextAggregatorPair(
    context,
    user_params=LLMUserAggregatorParams(
        user_turn_strategies=turn_strategies,
        user_turn_stop_timeout=2.5,
    ),
)
```

### B4. Riya greets first (LLM run on connect)

Context is seeded with a dummy user message and the LLM is kicked off on connect, so Riya
speaks the opening line instead of waiting for the user:
```python
context = LLMContext(messages=[{"role": "user", "content": "."}])
...
@transport.event_handler("on_client_connected")
async def on_client_connected(transport, client):
    await task.queue_frames([LLMRunFrame()])
```

### B4b. Sarvam server-side VAD — STOP phantom transcripts (CRITICAL)

**Root cause:** Pipecat's Silero VAD only emits turn events; it does NOT gate audio.
Every audio frame goes straight to Sarvam (`stt_service.py` calls `run_stt` per frame),
and Sarvam's own server-side VAD transcribes continuous background noise → phantom
transcripts ("Side", "So, okay.", random Tamil/Marathi/Punjabi) even when nobody spoke.
Raising Pipecat `min_volume`/`confidence` does NOT fix this — it doesn't control Sarvam.

**Fix:** set Sarvam's fine-grained VAD params (saaras:v3 only). Especially
`start_speech_volume_threshold` — by default Sarvam applies NO volume filtering at all.

```python
stt = SarvamSTTService(
    api_key=os.getenv("SARVAM_API_KEY"),
    mode="transcribe",
    sample_rate=16000,
    settings=SarvamSTTService.Settings(
        model="saaras:v3",
        start_speech_volume_threshold=-40.0,  # MAIN knob: ignore audio quieter than ~-40 dB
        positive_speech_threshold=0.75,       # higher speech-probability required
        negative_speech_threshold=0.35,
        min_speech_frames=6,                  # sustained speech needed → ignore blips
        first_turn_min_speech_frames=6,
        pre_speech_pad_frames=8,              # don't clip real-speech onset
        num_initial_ignored_frames=10,        # skip connection-start noise
    ),
)
```

Calibration: if it now misses your real (soft) speech, raise the volume threshold toward
`-45`/`-50` (less filtering); if noise still leaks, raise toward `-35` and bump
`positive_speech_threshold` to `0.8`. These are server-VAD knobs, independent of Pipecat VAD.

### B5. Audio sample rates
- Input: **16000 Hz** (`INPUT_SAMPLE_RATE`)
- Output: **24000 Hz** (`OUTPUT_SAMPLE_RATE`)

### B6. Pipeline order (for reference)
```
transport.input()
→ vad_processor
→ stt (Sarvam saaras:v3, transcribe)
→ TranscriptionGate          # noise/echo drop + manual interruption
→ TranscriptionLogger
→ captions_user
→ context_aggregator.user()
→ llm (Gemini 2.5-flash)
→ captions_bot
→ tts (ElevenLabs)
→ transport.output()
→ context_aggregator.assistant()
```

---

## Priority order for implementing in staging
1. **B1 — port the `TranscriptionGate` word-gate** (biggest quality win; kills noise/echo false interrupts; gives the 2-words-to-interrupt / 1-word-to-reply behaviour).
2. **B2 — disable framework auto-interruptions** so only the gate interrupts.
3. **A — set all env values** above.
4. **B3 — turn-taking** (VAD + transcription instead of SmartTurn) and **B4 — greeting on connect**.
5. **B5/B6 — sample rates + pipeline order** sanity check.

## Known trade-off (echo)
With `INTERRUPT_WORDS = 2` and `MIN_TURN_WORDS = 1`, without headphones Riya's own voice can
echo back through the mic and look like user speech, causing accidental self-interrupts.
Fix: use headphones (cleanest), rely on browser `echoCancellation`, or raise
`ECHO_COOLDOWN_SECS` / `INTERRUPT_WORDS` if it becomes a problem.

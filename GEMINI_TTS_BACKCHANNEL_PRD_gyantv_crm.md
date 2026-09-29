# PRD — Gemini 3.8 TTS + Backchannel for GyanTV CRM ("Genie" calling)

**Target repo:** `/Users/tridevmaurya/Desktop/gyan-tv-crm` (baseline: commit `4cdbc69`, 22 Sep 2026, plus the uncommitted fillers/campaign work in the working tree — commit that first)
**Reference implementation (already working):** `sahi-prompt-tester` → `gemini_audio.py` (`GeminiInteractionsTTSService`, `WholeResponseAggregator`), `bot.py` (`BackchannelProcessor`, `_synth_backchannel_clips`, `_load_custom_hums`, `_trim_silence`, `CostMeter.add_tts_usage`), `settings_store.py` (Gemini/backchannel/cost keys). Live demo: https://tinyurl.com/sahi-riya
**Date:** 25 Sep 2026
**Status:** mandatory spec. Where this document and the current code disagree, this document wins.

---

## 0. Summary

Two features, shipped as two PRs in this order:

| PR | Feature | Why |
|---|---|---|
| A | **Gemini 3.8 Flash / Flash-Lite TTS** as a selectable provider next to Chirp3-HD, with **one TTS request per reply** and **per-token cost tracking** | 100+ languages incl. Bengali/Marathi/Odia, style control, and at ₹1.2–1.8 per talk-minute it is in the same band as Chirp (₹1.45) with better prosody on Indic text |
| B | **Backchannel v2**: soft listening sounds (recorded hums + occasional word) while the caller talks, on a 4–6 s jittered timer **and** at micro-pauses | Today Genie is silent while the caller talks for 10 s; a real listener hums. v1 exists but fires only at micro-pauses, uses word clips only, and is coupled to turn-end fillers |

Both are per-agent YAML flags with per-call overrides, default **off** for Gemini TTS (Chirp stays default until A/B), default **on** for backchannel v2 on both Genie agents.

---

## 1. Current state (what the code does today)

- **Stack:** FastAPI + stock `pipecat-ai==1.1.0` (`requirements.txt:1`, not vendored). Exotel Voicebot WS `/ws/<agent>` (8 kHz on the wire via `ExotelFrameSerializer`) and browser WS `/ws/browser/<agent>` (protobuf). Pipeline synthesises at 24 kHz and the serializer downsamples for the phone (`app/pipeline.py:68-69`, `app/main.py:317-327`).
- **Pipeline order** (`app/pipeline.py:328-342`): `transport.input → VADProcessor → [SpeechWatcher] → SarvamSTT → TranscriptionGate → UserTurnRecorder → aggregators.user → [FillerPlayer] → GoogleLLM → BotTurnRecorder → ChirpTTS → transport.output → aggregators.assistant`.
- **TTS today:** `ChirpTTS(GoogleTTSService)` (`pipeline.py:173-187, 277, 295-300`): voice `f"{code}-Chirp3-HD-{voice}"`, `speaking_rate` from agent/param, service-account file, 24 kHz. `run_tts` strips `<END_CALL>`, skips punctuation-only fragments, respells standalone "us" → "uss". Aggregation is pipecat default **per sentence** (one Chirp request per sentence).
- **VAD:** Silero `confidence=0.6, start_secs=0.2, stop_secs=0.4, min_volume=0.3` (`pipeline.py:392-394`). Turn stop: `SpeechTimeoutUserTurnStopStrategy(0.6)`, `user_turn_stop_timeout=2.5`. **No barge-in** (`allow_interruptions=False`).
- **Backchannel v1** (`app/fillers.py`, uncommitted): `SpeechWatcher` after VAD computes RMS per frame and calls `FillerPlayer.backchannel()` on a ≥0.18 s micro-pause; guards: speech ≥1.0 s, ≥4.0 s since last, ≤3 per turn, not while `session.bot_speaking`. Clips are the short **word** clips (`hmm, haan_ji, achha, hmm_haan` for hin; `hmm, avuna, sare` for tel) from `fillers/<lang>/*.wav` (24 kHz mono, silence-trimmed). Pushed as plain `OutputAudioRawFrame` from `FillerPlayer`'s slot, so they are **not** bot speech (pipecat only raises bot-speaking for `TTSAudioRawFrame`/`SpeechOutputAudioRawFrame`, `base_output.py:718-731`). Enabled only when `fillers.enabled` is also true (`pipeline.py:320-326`).
- **Cost:** `CallObserver` (`pipeline.py:190-227`) sums `TTSUsageMetricsData` chars and LLM tokens; `cost()` prices TTS as chars × `RATE_CHIRP_USD_PER_1M_CHARS`; stored in `calls.cost_inr` / `cost_detail` (`store.py:134-142`).
- **Config:** `.env` + per-agent YAML in `agents/` re-read per call (`app/agents.py:38-49`), per-call params (`voice, speed, temperature, language, fillers`). No admin UI for voice.
- **Secrets on Fly:** pushed by `scripts/deploy_fly.sh:15-19`.

---

## 2. PR A — Gemini 3.8 TTS provider

### 2.1 API contract (verified 25 Sep 2026)

```
POST https://generativelanguage.googleapis.com/v1beta/interactions
headers: x-goog-api-key: <GOOGLE_API_KEY>, Content-Type: application/json
{
  "model": "gemini-3.8-flash-lite-tts" | "gemini-3.8-flash-tts",
  "stream": true,
  "input": [{"type": "user_input", "content": [{
      "type": "text", "text": "<reply text>",
      "annotations": [{"type": "speech_metadata", "style": "<style prompt>"}]   // optional
  }]}],
  "response_format": {"type": "audio", "mime_type": "audio/l16", "sample_rate": 24000},
  "generation_config": {"speech_config": [{"voice": "Aoede"}]}
}
```

Response is SSE: lines `event: <name>` then `data: <json>`.

| event | use |
|---|---|
| `step.delta` with `delta.mime_type` starting `audio` | `base64(delta.data)` = headerless PCM16 mono at the requested rate. Yield as `TTSAudioRawFrame` |
| `interaction.completed` | `interaction.usage.total_input_tokens` (text) and `total_output_tokens` (**audio tokens, what is billed**). ~40 audio tokens per second of speech |
| `step.stop`, `interaction.status_update`, `interaction.created` | ignore |

- Language is **auto-detected from the text**; there is no locale field. Native-script text selects the accent. (Genie's prompts already force native script.)
- Voices: same 30 names as Chirp3-HD (Aoede, Sulafat, Achird, Kore …), so the agent YAML `voice` value carries over unchanged.
- Measured TTFB per request: **~1.4 s (Lite), ~1.9 s (Flash)**, versus ~0.3 s for Chirp streaming. Generation runs ~1.5× realtime. This is why 2.3 below is mandatory.
- Price (till 31 Dec 2026): Lite **$6** / Flash **$9** per 1M audio tokens, $0.50 per 1M text tokens; doubles from 1 Jan 2027. At ~40 tok/s → Lite ≈ ₹1.2, Flash ≈ ₹1.8 per minute of Genie speech (USD_INR 88).

### 2.2 New file `app/gemini_tts.py`

Port `GeminiInteractionsTTSService` and `WholeResponseAggregator` from the reference `gemini_audio.py` (≈120 lines) with these CRM-specific changes:

1. Subclass name `GeminiTTS(TTSService)`; constructor `(session: CallSession, api_key, aiohttp_session, model, voice, style, chunking="response", sample_rate=24000)`.
2. **Keep ChirpTTS's text preprocessing** in `run_tts` before the request: strip `<END_CALL>` (the hang-up sentinel must still be detected upstream exactly as today), skip `_PUNCT_ONLY` fragments. The "us → uss" respell is Chirp-specific; do not apply.
3. Pass `settings=TTSSettings(model=model, voice=voice, language=None)` to `super().__init__` (pipecat 1.1 logs a `NOT_GIVEN` error otherwise).
4. Report usage: on `interaction.completed`, call `session.add_tts_usage(text_tokens, audio_tokens)` (new method, 2.5).
5. Non-200 or empty stream → `ErrorFrame`; log the first 200 chars of the body. No retry inside the service.

### 2.3 One request per reply (mandatory for Gemini)

Pipecat's default splits the reply at every `. ? ! ।` and issues one request per sentence. With a 1.4 s TTFB that puts a **1.5 s hole after every sentence** (measured: 15 stalls in a 10 s Hindi reply vs 3 with whole-reply mode). Implementation: `WholeResponseAggregator(SimpleTextAggregator)` whose `aggregate()` only buffers; pipecat's `TTSService` calls `flush()` on `LLMFullResponseEndFrame` and sends the whole reply. Set `self._text_aggregator = WholeResponseAggregator()` in `GeminiTTS.__init__` when `chunking == "response"`.

Consequence: first audio of a reply arrives after LLM completion + ~1.4 s instead of after the first sentence + ~0.3 s. Genie's replies are 1–3 sentences (`max_tokens=300`), so the expected voice-to-voice goes from ~2.1 s to **~3.0–3.3 s**. Accept for the A/B; if it is judged too slow, keep Chirp. Do **not** ship Gemini in sentence mode.

### 2.4 Config

Agent YAML (`agents/*.yaml`), new top-level block, defaults merged in `app/agents.py:49`:

```yaml
tts:
  provider: chirp          # chirp | gemini_lite | gemini_flash
  style: "warm, friendly, natural conversational pace"   # gemini only; speech_metadata.style
```

- `voice` and `speed` stay where they are. `speed` applies to Chirp only (Gemini has no rate field; put pace words in `style`).
- Per-call param `tts=chirp|gemini_lite|gemini_flash` overrides, same precedence as `voice`/`speed` (README §context precedence).
- Builder in `CallSession.run` (`pipeline.py:~277-300`): dispatch on provider; Gemini models `gemini-3.8-flash-lite-tts` / `gemini-3.8-flash-tts`.
- Key: reuse `GOOGLE_API_KEY` (already on Fly). No new secret.
- HTTP session: create `aiohttp.ClientSession()` in `CallSession.run` before the pipeline, pass to `GeminiTTS`, close in the existing `finally` (`pipeline.py:~380`). The reference service also self-creates one if `None` is passed; keep that fallback.

### 2.5 Cost tracking

- `CallObserver`: add `tts_in_tokens`, `tts_out_tokens`; `CallSession.add_tts_usage(inp, out)` increments them.
- `cost()`: if provider is `gemini_*`, `tts_inr = (out × RATE_GEMINI_{LITE|FLASH}_TTS_USD_PER_1M_AUDIO_TOK + in × RATE_GEMINI_TTS_USD_PER_1M_TEXT_TOK) / 1e6 × USD_INR`; else the existing chars formula. Env defaults: `6.0`, `9.0`, `0.5`.
- `cost_detail` JSON gains `tts_provider`, `tts_out_tokens`. Dashboard call page shows "tts ₹x (N audio tok)".

### 2.6 Acceptance (PR A)

- [ ] Browser call with `tts=gemini_lite`, Hindi: reply text in Devanagari, audio in Aoede, no gap > 0.5 s inside a reply (measure with `scripts/e2e_browser_call.py` timing of output chunks).
- [ ] Same on a real Exotel call: audible quality on the phone equal or better than Chirp; `<END_CALL>` still hangs up.
- [ ] `calls.cost_detail` shows `tts_out_tokens > 0` and `tts_inr` within ±10% of `tokens/1e6 × rate × USD_INR`.
- [ ] `tts=chirp` path byte-for-byte unchanged (regression).
- [ ] Gemini API error mid-call → `ErrorFrame` logged, call continues (next reply retries naturally); no crash.

---

## 3. PR B — Backchannel v2

### 3.1 Behaviour spec

While the caller is talking and Genie is silent:

1. **Timer trigger:** every `gap` seconds of continuous caller speech, `gap ~ uniform(min_gap, max_gap)` re-drawn after each play (defaults **4.0–6.0 s**, i.e. every 2–3 "beats" of ~2 s; never metronomic).
2. **Micro-pause trigger (keep v1):** on a ≥0.18 s pause with speech ≥ `0.6 × min_gap` since the last play.
3. **Pool:** two pools per language. **Hums** (language-neutral recorded clips in `fillers/hums/*.wav`: `hum_attentive.wav` 0.66 s, `hum_warm.wav` 0.33 s, from the two ElevenLabs recordings; add more later) and **words** (existing `fillers/<lang>/` word clips: hin `achha, okay, theek_hai`; tel `sare, okay, avuna`; generate tam/kan/mal/ben/eng with `scripts/make_fillers.py`, the phrases are already defined there). Pick hum with probability `hum_ratio` (default **0.7**), never the same clip twice in a row.
4. **Gain:** multiply PCM by `gain` (default **0.5**) before pushing. Fillers must sit under the caller's voice, not compete with it.
5. **Never** while `session.bot_speaking`; stop the timer on `BotStartedSpeakingFrame`; `max_per_turn` (default 3) unchanged.
6. Pushed as plain `OutputAudioRawFrame` (as v1), so no `BotStartedSpeakingFrame`, no `<END_CALL>` interaction, no latency-observer impact. Do **not** set `session.filler_until` for backchannel (v1 already doesn't).
7. Decouple from turn-end fillers: backchannel runs when `backchannel.enabled` is true regardless of `fillers.enabled`.

### 3.2 Code changes

- `app/fillers.py`
  - `load_hums()` (lru_cache): read `fillers/hums/*.wav` (accept mp3 too via PyAV if present), resample to 24 kHz mono, `_trim_silence` (threshold 2 %, 60 ms pad) — port from reference `bot.py`.
  - `FillerPlayer.backchannel()` → choose pool by `hum_ratio`, apply gain, keep the existing guards.
  - `SpeechWatcher`: add the timer. On VAD start, spawn an asyncio task polling every 0.2 s; when `now − since_last ≥ target` and not bot speaking → `player.backchannel()`; re-draw `target`. Cancel on VAD stop / bot start. Keep the RMS micro-pause path. Reference: `BackchannelProcessor._run` in `bot.py`.
- `app/agents.py:49`: new block with defaults
  ```yaml
  backchannel:
    enabled: true
    min_gap: 4.0
    max_gap: 6.0
    hum_ratio: 0.7
    max_per_turn: 3
    gain: 0.5
  ```
  and keep reading the legacy `fillers.backchannel` as an alias for one release.
- `app/pipeline.py:320-326`: add `SpeechWatcher` when `backchannel.enabled`, independent of `fillers.enabled`. Per-call param `backchannel=0|1`.
- `Dockerfile`: `fillers/` is already copied; ensure `fillers/hums/` is included.
- `agents/genie-*.yaml`: add the block, `enabled: true`.

### 3.3 Phone-specific risk: echo of the hum into STT

Browser calls have echo cancellation; **Exotel phone audio does not**. A hum played into the caller's earpiece can leak back through their handset mic and reach Sarvam as "hmm" or "अच्छा".

Mitigations, in order, keep what is needed after a real-call test:

1. Gain 0.5 and short clips (≤0.7 s) — the leak is far below speech level.
2. `TranscriptionGate`: drop a transcript if it arrives within `clip_len + 0.6 s` of a backchannel play **and** its text, lowercased, is one of the filler words / a bare hum (`hmm`, `हम्म`, `अच्छा`, `ओके`, `ठीक है`, `సరే`, `అవును`, …). Do not drop otherwise; the caller's real words must survive (v1's design intent).
3. The LLM note `_FILLER_NOTE` (`agents.py:96-102`) already tells Genie not to open with fillers; extend it with "ignore stray 'hmm/achha' in the user's transcript".

### 3.4 Acceptance (PR B)

- [ ] Browser call, Hindi, caller talks 20 s continuously: 3–5 fillers heard, ~70 % hums, none identical back-to-back, none while Genie talks.
- [ ] Caller talks with pauses: a filler lands on a pause only when ≥2.4 s of speech preceded it.
- [ ] Real Exotel call: fillers audible but clearly quieter than Genie's voice; no filler text in `turns` for the caller; Genie never says "hmm" as the first word of a reply.
- [ ] `backchannel.enabled: false` → zero `OutputAudioRawFrame` outside TTS (assert in `e2e_browser_call.py`).
- [ ] With Gemini TTS on (PR A), backchannel still plays during the ~1.4 s TTFB gap only if the caller is still speaking; never during Genie's own reply.

---

## 4. Rollout

1. Commit the working tree (fillers v1, campaign) as the baseline.
2. PR A behind `tts.provider` (default `chirp`). Deploy. A/B on `genie-content-feedback` with per-call `tts=gemini_lite` for 20 calls; compare `latency_p50`, `cost_inr`, and post-call analysis sentiment.
3. PR B with `backchannel.enabled: true` on both agents. Listen to 5 real calls each in Hindi and Telugu before enabling for campaigns.
4. Decide Gemini default per agent after the A/B; if adopted, add `GEMINI_TTS_*` rate envs to `scripts/deploy_fly.sh`.

## 5. Out of scope

Barge-in (still off), voice cloning / voice design on Gemini, STT changes, dashboard voice editor.

# PRD — Gemini 3.8 Flash / Flash-Lite TTS provider for sahi-english (Django voice agent)

**Target repo:** `/Users/tridevmaurya/Desktop/sahi english clone/sahi-english`, branch `prototype/talk-to-expert`, HEAD `59596ae` (ahead of production `main` @ `8d67c22`; every line number below is from `59596ae`)
**Reference implementation (working, deployed):** `sahi-prompt-tester` → `gemini_audio.py` (`GeminiInteractionsTTSService`, `WholeResponseAggregator`), `bot.py` (`CostMeter.add_tts_usage`), `settings_store.py` (Gemini keys + rates). Live: https://tinyurl.com/sahi-riya
**Scope:** TTS provider only. **No backchannel** in this repo (that feature ships in the GyanTV CRM PRD).
**Date:** 25 Sep 2026
**Status:** mandatory spec. Where this document and the current code disagree, this document wins.

---

## 0. Summary

Add two selectable TTS providers, `gemini_lite` (`gemini-3.8-flash-lite-tts`) and `gemini_flash` (`gemini-3.8-flash-tts`), next to `smallestai`, `google` (Chirp3-HD) and `elevenlabs`, following `docs/guides/add-new-tts-provider.md`, with three non-negotiables:

1. **One TTS request per LLM reply** (whole-reply aggregation). Gemini's time-to-first-byte is ~1.4 s per request; per-sentence requests put a 1.5 s hole after every sentence.
2. **Request audio at the pipeline rate** (`response_format.sample_rate = 16000` today) so no resampling is needed and Android is untouched.
3. **Bill by audio tokens** from `interaction.completed`, not by characters.

Default stays ElevenLabs. Gemini is enabled per user via the existing GrowthBook feature `sahi_english_tts_provider` (variant value `gemini_lite` / `gemini_flash`) or globally via admin `VOICE_AGENT_TTS_PROVIDER`.

---

## 1. API contract (verified live 25 Sep 2026)

```
POST https://generativelanguage.googleapis.com/v1beta/interactions
x-goog-api-key: <GEMINI_API_KEY>
{
  "model": "gemini-3.8-flash-lite-tts",            // or gemini-3.8-flash-tts
  "stream": true,
  "input": [{"type":"user_input","content":[{
      "type":"text","text":"<whole reply>",
      "annotations":[{"type":"speech_metadata","style":"<style prompt>"}]   // optional
  }]}],
  "response_format": {"type":"audio","mime_type":"audio/l16","sample_rate":16000},
  "generation_config": {"speech_config":[{"voice":"Sulafat"}]}
}
```

SSE stream, `event: <name>` line then `data: <json>` line. Parse **tolerantly**, keyed on payload shape, not event name: the live REST API emits `step.delta` / `interaction.completed`, while `google-genai` 1.73's typed client names them `content.delta` / `interaction.complete`.

| payload | action |
|---|---|
| `delta.mime_type` starts with `audio` and `delta.data` present | `base64decode` → headerless PCM16 mono at the requested rate → `TTSAudioRawFrame` |
| `interaction.usage` present | `total_input_tokens` (text), `total_output_tokens` (**audio tokens = billed unit**, ~40 per second of speech) |
| anything else | ignore |

Facts that shape the design:

- Language is auto-detected from the text; **no locale field exists**. The LANGUAGE LOCK in the prompts (native script) is what selects the accent. Roman-script drift (F7 in the audit) therefore hurts Gemini exactly as it hurts Chirp.
- Voice names are the same 30 as Chirp3-HD (Sulafat, Aoede, Achird, Kore…). No per-language voice id composition is needed.
- No `speaking_rate`; pace goes in the `style` string ("natural pace", "slightly slower").
- Measured TTFB: Lite ~1.4 s, Flash ~1.9 s per request; generation ≈1.5× realtime. Chirp streaming is ~0.3 s.
- Price till 31 Dec 2026: Lite $6 / Flash $9 per 1M audio tokens, $0.50 per 1M text tokens; **doubles from 1 Jan 2027**. ≈ ₹1.2 (Lite) / ₹1.8 (Flash) per minute of Riya speech at USD_INR 88.
- Use `raw REST + aiohttp`, not the `google-genai` interactions client, so the vendored pipecat service owns cancellation and chunking.

---

## 2. Changes, in the order of `docs/guides/add-new-tts-provider.md`

### 2.1 Registry — `apps/common/constants.py:168-194`

```python
TTS_PROVIDER_GEMINI_LITE = "gemini_lite"
TTS_PROVIDER_GEMINI_FLASH = "gemini_flash"
TTS_PROVIDER_REGISTRY = (
    (TTS_PROVIDER_SMALLEST, "..."),
    (TTS_PROVIDER_GOOGLE, "..."),
    (TTS_PROVIDER_GEMINI_LITE, "gemini_lite — Gemini 3.8 Flash-Lite TTS (API key; 100+ languages; $6/1M audio tok)"),
    (TTS_PROVIDER_GEMINI_FLASH, "gemini_flash — Gemini 3.8 Flash TTS (API key; best quality; $9/1M audio tok)"),
    (TTS_PROVIDER_ELEVENLABS, "..."),
)
```

`TTS_PROVIDER_FALLBACK_ORDER`, `TTS_PROVIDERS`, the admin dropdown (`apps/common/admin.py:87`), `validate_tts_provider` (`apps/common/validators.py:137-161`) and GrowthBook acceptance (`growthbook.py:483,498`) all derive from this tuple; no duplication.

### 2.2 Settings — `configurations/settings/base.py`

- Reuse `GEMINI_API_KEY` (L360-362; already the Vertex fallback key). No new secret on AWS.
- Module defaults next to the Google block (L429-433):
  ```python
  GEMINI_TTS_VOICE_NAME = "Sulafat"     # same persona as Chirp so A/B compares the model, not the voice
  GEMINI_TTS_STYLE = "warm, friendly Indian English teacher; natural conversational pace"
  ```
- `EXTRA_SETTINGS_DEFAULTS` (L441-883): add `GEMINI_TTS_VOICE_NAME` (validator: membership in a static set of the 30 prebuilt names; do **not** reuse `validate_google_tts_persona`, it calls the Cloud TTS voice list) and `GEMINI_TTS_STYLE` (string, may be empty). Update the `VOICE_AGENT_TTS_PROVIDER` description (L606-618) and fix the stale chain comment at ~L600.
- `apps/voice_agent/types.py` `ExtraCfg` (L140-148): add `gemini_tts_voice_name`, `gemini_tts_style`.
- `runtime.py:_load_extra_settings` (L353-388): read both.

### 2.3 New service — `apps/voice_agent/services/gemini_tts.py` (outside the vendored fork)

Port `GeminiInteractionsTTSService` + `WholeResponseAggregator` from the reference `gemini_audio.py`, adapted to the vendored base (`apps/pipecat/services/tts_service.py`):

```python
class GeminiTTSSettings(TTSSettings): pass   # model, voice, language=None

class GeminiInteractionsTTSService(TTSService):
    Settings = GeminiTTSSettings
    def __init__(self, *, api_key, model, voice, style="", sample_rate=16000, **kw):
        super().__init__(sample_rate=sample_rate, push_text_frames=False,
                         push_stop_frames=True, supports_word_timestamps=False,
                         pause_frame_processing=False, **kw)
        self._settings = GeminiTTSSettings(model=model, voice=voice, language=None)
        self._text_aggregator = WholeResponseAggregator()      # §2.4
        self._tts_cancel_event = asyncio.Event()               # §2.5
        self._session: aiohttp.ClientSession | None = None     # own it: start()/stop()/cancel()
```

- `run_tts(text, context_id)`: `start_ttfb_metrics` → POST (SSE) → on non-200 `yield ErrorFrame(error=f"Gemini TTS {status}: {body[:200]}", processor=self)` → `start_tts_usage_metrics(text)` → `yield TTSStartedFrame(context_id)` → per audio delta: `stop_ttfb_metrics` once, `yield TTSAudioRawFrame(pcm, self.sample_rate, 1, context_id=context_id)` → on usage payload call `self._report_usage(in, out)` → `yield TTSStoppedFrame(context_id)`.
- Request `response_format.sample_rate = self.sample_rate` (16000 today). When P0 of the voice-improvement plan (24 kHz output) lands, this follows the constant automatically; **do not hardcode 16000 or 24000 in this file**.
- `can_generate_metrics()` → True; `_sync_model_name_to_metrics()` like the Google service.
- Error markers: add `"gemini tts"` to `VOICE_AGENT_TTS_PROCESSOR_ERROR_MARKERS` (`apps/voice_agent/constants.py:256-264`); the permanent list (L205-251) already covers 401/403/quota/resource_exhausted.

### 2.4 Whole-reply aggregation (mandatory)

`WholeResponseAggregator(SimpleTextAggregator)`: `aggregate()` appends and never yields; the base `TTSService` calls `flush()` on `LLMFullResponseEndFrame` (`tts_service.py:520-540`) and issues **one** `run_tts` per reply. Measured on the reference: Hindi reply stalls dropped from 15 (max 2.3 s) to 3 (max 0.9 s).

Caveats from the vendored base, decided here:

- `_maybe_pause_frame_processing` runs before `flush()` and only pauses when `_processing_text` is already True; with a buffer-only aggregator it will not pause. That is intended: keep `pause_frame_processing=False`.
- `run_tts` is driven synchronously inside `_push_tts_frames`; the TTS processor blocks while the SSE stream is read. Acceptable (same as the HTTP ElevenLabs service).
- The direct-speech bypass `TTSSpeakFrame` (`runtime.py:1638-1640`, lesson resume lines) skips the aggregator and still works: one request per line.
- **No server-side pacing.** The Chirp fork paces chunks to realtime because Android overlapped consecutive per-sentence requests. With one request per reply there is nothing to overlap inside a reply. If QA on Android shows overlap between two back-to-back replies (e.g. greeting seed + first reply), reuse `GoogleBaseTTSService._pace_audio_chunk`; do not add it pre-emptively.
- Word timestamps: emit none. `TTSWordObserver` gets no `tts_word` events for Gemini; the UI already commits the bubble on `tts_stopped` and shows `llm_text`. (The Chirp estimator's 4 words/s is wrong for Indic anyway, audit G3.)

### 2.5 Interruption

Copy the `GoogleBaseTTSService` pattern (`google/tts.py:902-919, 1021-1025`): `_handle_interruption` sets `_tts_cancel_event` before calling super; `run_tts` checks it on every SSE chunk and, when set, closes the response and returns without `TTSStoppedFrame` duplication. `WholeResponseAggregator.handle_interruption()` clears the buffer (inherited).

### 2.6 Builder and dispatcher — `runtime.py`

- `_build_gemini_tts(self, *, extra_cfg, provider)` (next to `_build_google_tts`, L938-1017): raise `ValueError("GEMINI_API_KEY is not set …")` when empty (drives init-time fallback); model by provider id; voice/style from `extra_cfg`; `sample_rate=16000`.
- `_build_tts` (L1019-1061): two guards `case provider if provider == TTS_PROVIDER_GEMINI_LITE:` / `..._FLASH:` (guard form, not bare `case CONST`). Widen the return-type unions here and in `_build_tts_with_fallback` (L1063-1126).
- `_get_next_tts_provider` (L1128-1188): **replace the hardcoded `modern_providers` set with registry order.** Today a new id would fall into the `else` branch and, after Smallest+Google, ElevenLabs is returned and Gemini never tried. This also fixes audit F9 (three contradictory fallback descriptions). Ring becomes: smallestai → google → gemini_lite → gemini_flash → elevenlabs, skipping attempted ones.
- `_resolve_tts_provider` (`consumers/live.py:79-102`) and GrowthBook (`growthbook.py:452-506`) need no code change; update the docstrings that enumerate providers (`live.py:97-98`, `growthbook.py:455-472`).

### 2.7 Usage / cost

Production has no cost meter (`MetricsFrame` is only debug-logged, `runtime.py:2554-2555`). Minimum viable:

- New dataclass `TTSTokenUsageMetricsData(provider, tokens_in, tokens_out)` in `apps/pipecat/metrics/metrics.py` (next to `TTSUsageMetricsData`, L80); the service pushes it inside a `MetricsFrame`.
- `_on_pipeline_frame`: on that data, accumulate on the runtime (`self._tts_tokens_in/out`) and at session end write a structured log line `[tts-usage] session=… provider=… in=… out=… est_usd=…` and, if `apps/usage` has a per-session record, a column pair there (follow its migration rules in `CLAUDE.md`).
- Rates as settings: `GEMINI_TTS_USD_PER_1M_AUDIO_TOK_LITE=6.0`, `_FLASH=9.0`, `GEMINI_TTS_USD_PER_1M_TEXT_TOK=0.5` (env-readable; they change on 1 Jan 2027).

### 2.8 Client (Android / web)

No change. Chunks arrive at the same 16 kHz `sample_rate` field per chunk (`runtime.py:2557-2578`). The `connected` event's `tts_provider` (`live.py:495-504`) will read `gemini_lite`/`gemini_flash`; the web client forwards it to Android via `safeSetUserProperties` unchanged.

### 2.9 Tests (new; none exist today)

- `apps/voice_agent/tests/test_gemini_tts.py`: SSE parser against a recorded fixture (both event-name dialects); usage extraction; cancel mid-stream closes the response.
- `apps/voice_agent/tests/test_whole_reply_aggregator.py`: tokens in → nothing yielded → `flush()` returns the joined text; `handle_interruption()` clears.
- `apps/voice_agent/tests/test_tts_dispatch.py`: `_build_tts` dispatches both ids; missing key raises; `_get_next_tts_provider` walks registry order.
- Manual: `scripts/test_voice_agent.py --port 8000` with admin `VOICE_AGENT_TTS_PROVIDER=gemini_lite`, Telugu session, listen for gaps; break the key and confirm fallback to `google`.

---

## 3. Acceptance

- [ ] Telugu session with `gemini_lite`: on-screen text in Telugu script, voice Sulafat, no audible hole inside a reply; voice-to-voice ≤ 3.3 s p50 (vs ~2.1 s Chirp) — accept for A/B only.
- [ ] Hindi session with `gemini_flash`: same, plus quality judged ≥ Chirp by two listeners.
- [ ] `GEMINI_API_KEY` blank → init falls back to the next registry provider with a warning; runtime 401 → permanent-error path, no retry storm.
- [ ] Usage log line present per session; tokens ≈ 40 × seconds of TTS audio ± 20 %.
- [ ] Lesson-practice resume line (`TTSSpeakFrame`) still spoken.
- [ ] Interrupt button mid-reply stops Gemini audio within 300 ms.
- [ ] `smallestai`, `google`, `elevenlabs` paths unchanged (regression on one call each).

## 4. Rollout

1. Merge behind the registry; default provider unchanged.
2. GrowthBook `sahi_english_tts_provider`: 10 % `gemini_lite` on Hindi + Telugu users for a week; compare latency p50, session length, and the "voice quality" survey if present.
3. Decide Lite vs Flash vs Chirp per language; Gemini's advantage is languages Chirp lacks (Bengali/Marathi/Odia for future expansion) and style control; its cost is the ~1.1 s extra first-word latency.
4. Independently of this PRD, apply the production voice-improvement plan P0 (24 kHz output) and P4 (STT `secondary_languages`); both benefit Gemini as much as Chirp.

## 5. Out of scope

Backchannel / listening fillers (CRM PRD), voice cloning or voice design, Gemini as STT, changes to the Android player.

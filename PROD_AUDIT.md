# Production voice stack audit — sahi-english (Django) + gyantv-sahi-english-web

Read-only audit, 7 Sep 2026. Repos cloned to `~/sahi-english` (main @ 8d67c22) and
`~/gyantv-sahi-english-web` (staging @ 6d7953e). Line numbers refer to those checkouts.
Compared against the reference implementation in this repo (`bot.py`).

---

## 1. What production actually is

The "app" is three pieces:

| Piece | Repo | Role |
|---|---|---|
| Android app | not in scope | Native shell. Hosts the web app in a WebView, injects the GyanTV token cookie. |
| Web app | `gyantv-sahi-english-web` (React + Express/Node) | UI incl. the Call Riya screen (`client/src/components/VoiceInterface.tsx`, 2871 lines). Node backend is being decommissioned. |
| Backend | `sahi-english` (Django 5.1 + Channels + Postgres + Redis) | The real voice agent: `ws /api/voice-agent/live`, `apps/voice_agent/services/runtime.py` (3186 lines). Ships a **vendored, modified fork of Pipecat** under `apps/pipecat/`. |

Voice call flow: browser mic → 16 kHz PCM over WebSocket → Django Channels →
Silero gate → STT → SmartTurn v3 → Vertex Gemini → TTS → base64 PCM back over the same socket.

## 2. Production pipeline vs. this repo

| Concern | Production (`runtime.py`) | This repo (`bot.py`) |
|---|---|---|
| Transport | Custom Channels WS, JSON events, base64 PCM | Pipecat WS transport, protobuf frames |
| Sample rate | 16 kHz everywhere (in, STT, TTS, out) | 16 kHz in, 24 kHz out |
| VAD | Custom `SileroAudioGateProcessor`, conf 0.65, vol 0.6, hold 0.4 s, 3 start frames, 1.5 s bot-speaking grace | Pipecat Silero VAD, conf 0.6, start 0.2 s, stop 0.5 s |
| Turn end | SmartTurn v3 ONNX + 5 s stop timeout | VAD stop only |
| Interruption | Gate + explicit UI "interrupt" command | Word-count gate (`INTERRUPT_WORDS`) |
| STT default | ElevenLabs `scribe_v2_realtime`, `language_code=<hi/te/ta/kn>` | Sarvam `saaras:v3` codemix (settings) |
| STT alt | Sarvam `saaras:v3` codemix, pinned per language | ElevenLabs Scribe, Google chirp_2 |
| Inbound filter | Language-tag filter (`SESSION_LANGUAGE_ACCEPT_PREFIXES`), ASCII escape hatch, Sarvam never filtered | Unicode script allowlist (drops CJK/Cyrillic) |
| LLM | Vertex Gemini `gemini-2.5-flash-lite`, temp 0.8, max 500, thinking off, Gemini-API fallback | Gemini API `gemini-3.1-flash-lite`, temp 0.8, max 500 |
| TTS default | **ElevenLabs** `eleven_flash_v2_5`, one global voice, no language param | Google Chirp3-HD per language (settings) |
| TTS options | Smallest `lightning_v3.1_pro` voice `meher`, Google Chirp3-HD persona `Sulafat` | ElevenLabs, Google |
| TTS selection | GrowthBook `sahi_english_tts_provider` → DB extra_setting → hardcoded; per-user, never per-language | Settings page |
| Google voice id | `f"{GOOGLE_LANG_CODE[lang]}-Chirp3-HD-{persona}"`, fallback `hi-IN` | Same pattern, fallback `en-US` |
| Languages | Exactly 4: `hi te ta kn` (`apps/common/constants.py:3-11`) | 7: hin tel tam kan mal ben eng |
| Language source | GyanTV user profile, request body ignored, re-read from Session row | Picker on call page |
| Language enforcement | Prompt only: per-language block (native script) + "LANGUAGE LOCK" self-check | Prompt directive + native-script kickoff seed |
| Outbound text sanitising | None on the voice path | "us"→"uss" respell for Chirp |

## 3. Findings (fixes to consider), by severity

### 🔴 F1. Smallest.ai synthesises every language as English with a Hindi voice
`runtime.py:837-847` builds `SmallestTTSService.Settings(model, voice, speed)` with the comment
"Language is handled by the LLM prompt". But the vendored service seeds `language=Language.EN`
(`apps/pipecat/services/smallest/tts.py:168`) and sends it on every request. Voice is the fixed
`meher`. Smallest's own language map has **no Telugu at all**. Smallest is labelled "(primary)" in
`TTS_PROVIDER_REGISTRY` and is a live GrowthBook variant, so Telugu/Tamil/Kannada users in that
bucket get mangled speech.
**Fix:** pass `language=` from the session language; disable Smallest for `te`; or drop Smallest from
the ring for non-Hindi sessions.

### 🟠 F2. ElevenLabs: one voice, no `language_code`, for all four languages
`runtime.py:855-936` never sets `params.language`, so the WS URL carries no locale. Voice
`m7GHBtY0UEqljrKQw2JH` is global. This is the default provider. Likely the source of
"English accent reading Telugu" complaints.
**Fix:** per-language voice map + pass `language_code`; or make Google the default for non-Hindi.

### 🟠 F3. Unmapped language silently falls back to a Hindi voice (Google)
`runtime.py:984-988`: `GOOGLE_LANG_CODE.get(language, "hi-IN")`. The WS `?language=` defaults to
`"en"` (`consumers/live.py:170`) and `self._language` is the fallback when the Session row fails to
load, so a Tamil learner can get `hi-IN-Chirp3-HD-Sulafat` with no warning log.
**Fix:** log a warning on fallback; fall back to the session's own locale or refuse to start.

### 🟠 F4. `GOOGLE_TTS_MODEL` admin setting does nothing
`runtime.py:1004` passes `model=` into Settings, but `GoogleTTSService.run_tts`
(`apps/pipecat/services/google/tts.py:1300-1348`) never reads it. Model is implied by the
hardcoded `-Chirp3-HD-` substring at `runtime.py:989`.
**Fix:** remove the setting or make the voice-id template use it.

### 🟠 F5. Malayalam is a phantom language
`ml-IN` exists in `GOOGLE_LANG_CODE`, `validators.py:26` and admin help text, but `Language` has no
`ml`, so it is unreachable. Worse, `validate_google_tts_persona` (`validators.py:91-104`) rejects any
persona not available in `ml-IN`, narrowing valid personas for a language nobody uses.
**Fix:** either add Malayalam properly (enum, onboarding, prompt blocks, STT maps) or delete `ml`.

### 🟡 F6. Fixed English resume lines read by a native-language voice
`VOICE_AGENT_LESSON_RESUME_LINES` ("Sorry, I lost you for a moment there!") are pushed as raw
`TTSSpeakFrame` (`runtime.py:1627-1631`), so with Google active a Tamil voice reads English.
**Fix:** per-language resume lines, or route through the LLM.

### 🟡 F7. No server-side guard against Roman-script native text reaching TTS
Only the prompt's "LANGUAGE LOCK" prevents Gemini from writing romanised Hindi/Telugu.
`VOICE_AGENT_TEXT_FILTER_LOOKAHEAD_CHARS` is defined but never used. When the model drifts, TTS
reads "ela unnaru" as English.
**Fix:** a lightweight outbound script check (like this repo's inbound `_ALLOWED_SCRIPT`) that at least
logs/metrics drift, optionally re-asks the model.

### 🟡 F8. Voice path has zero text sanitising
The REST TTS path (`apps/tts/services/tts.py:19-52`) strips bilingual separators, `<emotion>` tags
and `[laughter]`; the voice pipeline strips nothing. Markdown or `-----` from Gemini is spoken.
**Fix:** reuse the REST sanitiser as a FrameProcessor before TTS; add the Chirp "us" fix from `bot.py`.

### 🟡 F9. Three contradictory descriptions of the TTS fallback order
Registry says Smallest primary / ElevenLabs last (`apps/common/constants.py:174-188`);
`base.py:578-583` comments "elevenlabs → smallestai → google"; `_get_next_tts_provider`
(`runtime.py:1128-1187`) implements a third order. Actual default is ElevenLabs
(`base.py:377`, a bare literal, the only provider setting not env-readable).
**Fix:** one source of truth, make `VOICE_AGENT_TTS_PROVIDER` env-readable.

### 🟡 F10. Sarvam loses its language pin for unmapped codes
`runtime.py:765`: `SESSION_LANGUAGE_TO_SARVAM.get(language)` → `None` → auto-detect across 100+
languages, the exact behaviour the surrounding comment says was removed. Sarvam frames carry no
language tag so `TranscriptionObserver` cannot catch it either.
**Fix:** default to `hi-IN` (or session locale) instead of `None`, log on miss.

### 🟡 F11. Web client: pure web users cannot start a call
`getAuthToken()` reads only the `sahienglish_access_token` cookie, which the web OTP login never
sets (only Android WebView injects it). Empty token → no auth frame → Django closes 4001. Works on
`*.gyantv.in` hosts only because of the parent-domain cookie.

### 🟡 F12. Stale config in both repos
- Web: `VITE_PIPECAT_WS_BASE_URL`/`VITE_PIPECAT_API_BASE_URL` in `.env.example` are unused; the
  client derives the WS host from `VITE_USER_API_BASE`. `npm run db:up` references a `postgres`
  compose service that no longer exists.
- Django: `WS_AUTH_DISABLED` is defined (`base.py:44`) but never read; `scripts/test_voice_agent.py`
  relies on it and defaults to port 5003 (nothing listens there). `PIPECAT_WS_HOST/PORT`,
  `VOICE_SERVICE_TOKEN` are vestigial. Celery is a dependency but nothing is wired.

## 4. Adding Bengali (or any 5th language) to production — touch list

1. `apps/common/constants.py` `Language` enum (+ `LEARN_HIDDEN_LANGUAGES` decision).
2. `apps/onboarding/constants.py` option list + `MIN_APP_VERSION_FOR_ALL_LANGUAGES` gate.
3. `apps/voice_agent/services/runtime.py`: `GOOGLE_LANG_CODE`, `SESSION_LANGUAGE_TO_SARVAM`.
4. `apps/voice_agent/constants.py`: `SESSION_LANGUAGE_ACCEPT_PREFIXES`, `SESSION_LANGUAGE_GREETING_EXAMPLE`, `VOICE_AGENT_SUPPORTED_LANGUAGE_PREFIXES`.
5. `apps/prompts/constants.py` `LANGUAGE_BLOCK_PROMPT_TYPE` + `language_blocks.py` (CALL native-script block, CHAT romanised block) + `seed_prompts`.
6. `apps/sessions/ai/gemini.py` `LANGUAGE_NAMES`.
7. `apps/common/validators.py` Google TTS supported languages / persona check.
8. ElevenLabs: per-language voice (see F2). Smallest: Bengali exists in its map, Telugu does not.
9. Web: `client/src/components/LanguageSelector.tsx` (4 entries) and `server/routes.ts:366` accepted codes.
10. GyanTV side must return the new `language_code` in `user_details`, since the session language comes from there, not from the request.

## 5. Replicating production locally

### What is needed
| Item | Status |
|---|---|
| Python 3.13 + `uv` | installable |
| Postgres + Redis | `brew install postgresql@16 redis`; no docker-compose in the Django repo |
| Model files (Silero, SmartTurn ONNX, NLTK punkt) | committed in repo, no download |
| `DEPLOYMENT=local_dev` env var | mandatory, else KeyError at import |
| `ELEVENLABS_API_KEY` | needed (default STT and TTS); we have one in this repo's `.env` |
| Vertex creds (`GOOGLE_CLOUD_PROJECT`, `GCP_CLIENT_EMAIL`, `GCP_PRIVATE_KEY`) or `GEMINI_API_KEY` | we have a Gemini key and a GCP service account |
| `SARVAM_API_KEY` | we have one |
| **GyanTV access token** | **blocker**: WS auth calls the external GyanTV API; no local bypass exists |
| DB schema | shared with Drizzle; fresh local Postgres works but diverges from prod columns |

### Two viable paths
- **A. Team-assisted (faithful):** get a GyanTV test token + dev DB creds from the team. Then:
  `uv sync --dev`, `migrate` with the `--fake` baselines from `docs/engineering-practices.md §11`,
  `seed_prompts`, `runserver 8000`, create a session via `POST /api/v1/sessions/`, drive it with
  `scripts/test_voice_agent.py --port 8000` after adding an auth frame to the script.
- **B. Local-only (fast, for TTS/STT work):** fresh Postgres, plus a small local patch that honours
  `WS_AUTH_DISABLED` in `GyanTVWebSocketMiddleware` and stubs `request.user.language`. Enough to
  exercise the full STT→LLM→TTS pipeline and test F1–F10 without GyanTV. Never commit the patch.

### Bring-up (path B)
```
brew install postgresql@16 redis portaudio && brew services start postgresql@16 redis
cd ~/sahi-english && uv sync --dev && uv add websockets
export DEPLOYMENT=local_dev
# fill ELEVENLABS_API_KEY, SARVAM_API_KEY, GEMINI_API_KEY into envs/local_dev.env (git-tracked: do not commit)
uv run python manage.py migrate && uv run python manage.py seed_prompts
uv run python manage.py runserver 0.0.0.0:8000
```

---

## 6. Why the reference (`bot.py`) sounds better than production — verified diff

All values checked in both checkouts.

| # | Reference (`bot.py` / `settings_store.py`) | Production (`runtime.py` / `base.py` / `constants.py`) | Effect |
|---|---|---|---|
| 1 | TTS output **24000 Hz** (`OUTPUT_SAMPLE_RATE`) | **16000 Hz** hardcoded at `runtime.py:837, 902, 1001` and `AudioBufferProcessor` `:1215` | Everything above 8 kHz is thrown away: sibilants and Indian retroflex/aspirated consonants go dull. Chirp3-HD and ElevenLabs both generate 24 kHz natively. Own PRD (`GOOGLE_TTS_PRD.md` §5) mandates 24 kHz end to end. |
| 2 | ElevenLabs stability **0.5**, speed **1.0** | stability **0.90**, speed **0.85** (`base.py:391-394`) | Flat prosody, 15% slower: the "tired robot" feel. Admin-editable extra_settings, no deploy needed. |
| 3 | `aggregate_sentences` default (on) | `aggregate_sentences=False` (`runtime.py:908`) for 200–500 ms faster TTFA | ElevenLabs never sees a full sentence, so no question rise / comma pause. Latency vs prosody trade-off, A/B it. |
| 4 | ElevenLabs STT `language_code=<primary>&secondary_languages=eng` (`bot.py:317-346`) | bare `language_code=language` (`runtime.py:793`) | Primary is a hint, not a lock. Spoken Telugu can come back in Devanagari and the Telugu voice then reads garbage. Reference verified the repeated-param form works and the comma-joined form silently resets to auto-detect. |
| 5 | `VAD_MIN_VOLUME` **0.3**, confidence 0.6 | `VOICE_AGENT_VAD_MIN_VOLUME` **0.6**, confidence 0.65 (`constants.py:161-170`) | Quiet speakers lose their first word before the gate opens, STT gets a truncated utterance. Admin-editable. |
| 6 | Chirp "us"→"uss" respell, punctuation-only fragment skip, Smallest forced language code | none | "you-ess", dead gaps, Smallest always `language="en"`. |
| 7 | Native-script kickoff seed turn + prompt directive | greeting word name only (`runtime.py:602-618`) | At temperature 0.8 a bare instruction drifts; the seed turn locks script from turn one. |

Items 1, 2 and 5 are config-level and likely account for most of the audible gap.

### Keep from production (do not regress)
SmartTurn v3 end-of-turn detection, TTS fallback ring with permanent-error markers, STT fallback,
DB-backed per-language prompt blocks with GrowthBook variants, Vertex + API-key dual-path LLM,
call recording, `user_turn_stop_timeout=5.0` (raised deliberately for Hindi/mixed sentences).

### Suggested order
1. **Day 1, admin only:** ElevenLabs stability 0.90→0.5, speed 0.85→1.0; `VOICE_AGENT_VAD_MIN_VOLUME` 0.6→0.3. Record one Hindi and one Telugu call before/after.
2. **Week 1, small code:** output sample rate 24 kHz (3 TTS builders + AudioBufferProcessor + confirm Android playback honours the per-chunk `sample_rate` field it already receives); `"en": "en-IN"` in `GOOGLE_LANG_CODE` + warning log on fallback; Smallest real language code or disable for Telugu; ElevenLabs language via `settings=` not `params=`.
3. **Week 2, structural:** `secondary_languages` for ElevenLabs STT; `_US_FIX` + `_PUNCT_ONLY` TTS wrappers; script allowlist filter both directions; `aggregate_sentences=True` A/B; native-script kickoff seed.

---

## 7. Google TTS choppiness: root cause is in the vendored fork, not voice selection

Verified: production's `apps/pipecat/services/google/tts.py` is 1607 lines vs 1458 upstream
(pipecat-ai 1.1.0). The extra ~150 lines are in the streaming loop and are absent upstream.
Voice-id composition (`te` → `te-IN-Chirp3-HD-<persona>`) is already correct, so the original PRD bug
is fixed; what remains is the audio delivery path.

| # | Cause | Where | Effect |
|---|---|---|---|
| G1 | **Realtime pacing**: `_pace_audio_chunk()` sleeps so each chunk leaves at wall-clock time | vendored `tts.py:1077-1123` | Audio is metered out at 1× speed instead of dumped to the client buffer |
| G2 | **`pause_frame_processing=True`** on the service | `tts.py:1264-1271` | Next sentence's RPC cannot start until the paced audio has finished, so every sentence boundary adds one TTFB gap. G1 + G2 together is the "ruk-ruk kar bolna". Worse in te/ta/kn where clauses are short. |
| G3 | **Word timestamps estimated at 4.0 words/s** (Latin assumption) and the loop at `tts.py:1136-1142` sleeps until the last estimated word before `TTSStoppedFrame` | `tts.py:488, 513` | Agglutinative Telugu/Tamil/Kannada have few whitespace tokens per second of audio; English-heavy code-mix over-estimates and the service idles past the end of audio, stalling the next sentence via G2 |
| G4 | `sample_rate=16000` | `runtime.py:1001` | See §6 row 1; PRD §5 requires 24 kHz |
| G5 | Persona default **`Sulafat`** vs reference **`Aoede`** | `base.py:409` vs `settings_store.py` | If local tests ran Aoede, part of the perceived gap is simply a different voice. Admin `GOOGLE_TTS_VOICE_NAME`, 1 minute to test. |
| G6 | `speaking_rate=speed` always sent, even at 1.0 | `runtime.py:1005` | Reference never sends it (field absent). PRD §3 flags Chirp3-HD rate support as limited. Unconfirmed impact; send only when `speed != 1.0`. |
| G7 | No `_US_FIX` respell | — | "us" read as "you-ess" in code-mix lines |

Upstream's loop is simply: receive chunk → yield `TTSAudioRawFrame`; the client's playback buffer
handles timing. The fork's pacing was added because the Android client overlapped sentences when it
buffered a whole utterance (comment at `tts.py:1105-1109`). The right fix is a playback queue on the
client, not server-side metering.

### Fix order for te/ta/kn
1. Admin: `GOOGLE_TTS_VOICE_NAME` = `Aoede`, listen. Isolates G5.
2. `sample_rate` 24000 in the Google builder (+ recorder + Android check). Isolates G4.
3. `pause_frame_processing=False` **or** remove pacing (restore upstream loop) and fix the Android playback queue. Kills G1/G2.
4. Decouple `tts_word` events from audio: never let the word loop delay `TTSStoppedFrame`; per-language words/s if estimates are kept. Kills G3.
5. `speaking_rate` only when `!= 1.0`; `_US_FIX` wrapper.

Expectation: 1+2 remove most of the audible gap, 3 the choppiness.

---

## 8. Listening test result (7 Sep) and revised priority

| Setup | Verdict |
|---|---|
| Local, Chirp3-HD Aoede, 24 kHz | best |
| Local, 16 kHz | slightly worse, still acceptable |
| Production, Sulafat, 16 kHz | clearly bad |

Conclusion: sample rate is real but explains only a small part. Something else, present only in
production, is the main factor. Candidates, in order of likelihood:

1. **Wrong script reaching TTS.** Production STT is ElevenLabs Scribe with a bare `language_code="te"`
   (`runtime.py:793`) and no `secondary_languages` constraint; spoken Telugu can be transcribed in
   Devanagari, the LLM then answers in Devanagari/Roman, and the Telugu Chirp voice reads it badly.
   Local never hits this because it uses Sarvam `saaras:v3` codemix. **Check first:** in a production
   Telugu call, read Riya's on-screen text. If it is not Telugu script, fix F2/P4 before anything else.
2. **Vendored pacing + `pause_frame_processing=True`** (section 7, G1–G3): sentence gaps and stalls.
3. Persona Sulafat vs Aoede, `speaking_rate` always sent.

### Production change list
| Prio | Change | Where |
|---|---|---|
| P0 | `sample_rate` 16000 → 24000 for Google TTS, plus `AudioBufferProcessor` and the `queue_audio` 16 kHz warning; verify Android honours per-chunk `sample_rate` | `runtime.py:1001, 1215, 1719-1747` |
| P1 | `pause_frame_processing=True` → `False` (try alone first), then no-op `_pace_audio_chunk`, then drop the trailing word-wait loop | vendored `google/tts.py:1271, 1077-1090, 1135-1145` |
| P2 | send `speaking_rate` only when `speed != 1.0` | `runtime.py:1005` |
| P3 | admin `GOOGLE_TTS_VOICE_NAME` = `Aoede` | extra_settings |
| P4 | ElevenLabs STT: `secondary_languages=["eng"]` (reference `_multilang_language_value`, `bot.py:317-346`); becomes P0 if the script check fails | `runtime.py:793` |

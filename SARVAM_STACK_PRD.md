# PRD — "Cheap Stack": Sarvam Bulbul TTS + Groq Whisper STT + Gemini Flash-Lite

**Branch:** `feat/sarvam-cheap-stack` · **Status:** Phase 1 built, first measurements in §10 · **Date:** 10 Sep 2026
**Owner:** Tridev · **Repo:** sahi-prompt-tester (this app is Phase 1 test bench; production bridge is Phase 2)

---

## 1. Why (context)

Riya's production voice stack (ElevenLabs Conversational AI) costs ~₹7–9 per talk-minute.
At the current campaign volume (~34,000 talk-min/month, measured from `leads.db` Sep 1–10)
that is ₹2.4–3L/month. A Sarvam + Groq + Flash-Lite stack prices out at **~₹0.80/min
(~₹27K/month)** — a ~90% cut — *if* voice quality and latency hold up.

This PRD covers **Phase 1: prove it in the prompt tester** — the browser test bench this
repo already is — so we can A/B the voice and measure latency/cost before touching the
production Exotel bridge.

### Target stack and rates (verified Sep 2026)

| Layer | Provider / model | Rate | Per talk-min |
|---|---|---|---|
| TTS | Sarvam **Bulbul** (`bulbul:v2`, try `bulbul:v3`) | ₹15/10K chars (v3: ₹30/10K) | ~₹0.60 (~400 chars) |
| STT | Groq **Whisper large-v3-turbo** | $0.04/hour | ~₹0.06 |
| LLM | Gemini **Flash-Lite** (already the repo default) | $0.10/$0.40 per 1M tok | ~₹0.13 |
| | | **Total** | **~₹0.80/min** |

Note: `gemini-2.5-flash-lite` retires 16 Oct 2026 → repo already defaults to
`gemini-3.1-flash-lite`; budget with 3.1 rates ($0.25/$1.50 → total ~₹1.0/min).

> **Superseded by §10 (10 Sep test run):** Sarvam has retired `bulbul:v2` — its API
> rejects v2 requests — so the ₹15/10K rate is gone; v3 is ₹30/10K. Groq turbo garbles
> Telugu, so the default is `whisper-large-v3` ($0.111/hr). Measured cost on short test
> calls was ₹1.2–2.1/min, not ₹0.80.

---

## 2. Goals & success criteria

| Metric | Target | How measured |
|---|---|---|
| Cost per talk-minute | ≤ ₹1.0 | new per-call cost meter (§4.5), verified against provider dashboards |
| Voice-to-voice latency (user stops → first Riya audio) | ≤ 1.5 s (stretch 1.2 s) | existing `[tts]`/`[stt]` log timestamps + new TTFB log line |
| Language coverage | hi, te, ta, kn all natural in native script | manual scripted conversations (§6) |
| Quality (subjective) | Team blind A/B: Bulbul ≥ "acceptable vs ElevenLabs" on 10-sample panel | `/` call page, two saved voice configs |
| STT accuracy | No regressions vs Sarvam saaras (already best for Telugu); zero ghost text on silence | scripted tests incl. silence/noise |

**No-go criteria (kill the idea cheaply):** Bulbul voice rated clearly worse by team on
Hindi *and* Telugu, or latency consistently > 2 s after tuning. Then we stop at Phase 1
having spent ~₹100 of credits and a few days.

---

## 3. What already exists (do not rebuild)

- **Pipeline:** Pipecat 1.1.0 — Silero VAD, `TranscriptionGate` turn-taking/barge-in,
  captions side-channel, script-allowlist hallucination filter (`bot.py`). All reused as-is.
- **LLM:** `GoogleLLMService`, default `gemini-3.1-flash-lite` — **zero work**.
- **Sarvam STT** (`saaras:v3`) already integrated — stays available as the STT fallback/
  comparison option.
- **Provider switching** via `/settings` (`TTS_PROVIDER` / `STT_PROVIDER` in
  `settings_store.py`), voice library, prompt library — reused.
- **Pipecat ships the services we need** (verified in this venv):
  `pipecat.services.sarvam.tts.SarvamTTSService` (WebSocket streaming) /
  `SarvamHttpTTSService`, and `pipecat.services.groq.stt.GroqSTTService`
  (Whisper, segmented — plugs into our VAD).

**Net new work = 2 provider builders + settings keys + voice seeds + cost meter.**

---

## 4. Scope — changes by file

### 4.1 `settings_store.py`

- `TTS_PROVIDER` choices: add `"sarvam"`. `STT_PROVIDER` choices: add `"groq"`.
- New group **Sarvam TTS**:
  - `SARVAM_TTS_MODEL`: `bulbul:v2` (default) | `bulbul:v3`
  - `SARVAM_TTS_VOICE`: fallback speaker (default `anushka`); voice library entry wins
  - `SARVAM_TTS_PACE` (0.5–2.0, default 1.0), `SARVAM_TTS_PITCH`, `SARVAM_TTS_LOUDNESS`
- New group **Groq STT**:
  - `GROQ_STT_MODEL`: `whisper-large-v3-turbo` (default) | `whisper-large-v3`
  - `GROQ_STT_LANGUAGE`: `auto` | hard lock (`hi`/`te`/`ta`/`kn`/...) — same semantics
    as the ElevenLabs lock; when the user picks a language on the call page we pass its
    ISO code to Whisper (Whisper takes a single language hint, unlike the multilang set)
- New group **Cost rates** (editable so price changes don't need code):
  `RATE_SARVAM_TTS_INR_PER_10K_CHARS` (15), `RATE_GROQ_STT_USD_PER_HOUR` (0.04),
  `RATE_LLM_USD_IN_PER_1M` (0.25), `RATE_LLM_USD_OUT_PER_1M` (1.50), `USD_INR` (88).

### 4.2 `bot.py` — TTS builder

```python
def _build_sarvam_tts(cfg, voice_id, speed, language):
    # SarvamTTSService — WebSocket streaming; target_language_code from the
    # existing GOOGLE_LANG map (hi-IN / te-IN / ta-IN / kn-IN ...).
    # speaker = picked voice (library) or SARVAM_TTS_VOICE fallback.
    # pace = per-call slider speed (wins) else SARVAM_TTS_PACE.
    # sample_rate = OUTPUT_SAMPLE_RATE (Sarvam supports 8k/16k/22.05k/24k —
    # 8k support is exactly what Phase 2 telephony needs).
```

- Wire into `_build_tts()` factory: `if cfg["TTS_PROVIDER"] == "sarvam": ...`
- The picked language **must** set `target_language_code` — Bulbul needs the locale
  (unlike Chirp there is no per-voice locale suffix; same voice name serves all langs).
- Keep the existing "us"→"uss" respell? **No** — that hack is Chirp-specific; verify
  Bulbul reads English loanwords sanely first, add respells only if needed.

### 4.3 `bot.py` — STT builder

```python
def _build_groq_stt(cfg, language):
    # GroqSTTService (BaseWhisperSTTService) — SEGMENTED: it transcribes the
    # VAD-cut utterance, which our pipeline already produces. No realtime WS.
    # model = GROQ_STT_MODEL; api_key = GROQ_API_KEY;
    # language = picked language's ISO code, else GROQ_STT_LANGUAGE, else None.
```

- Wire into `_build_stt()` factory as `"groq"`.
- **Hallucination guard:** Whisper invents text on silence/noise. Two existing defenses
  already cover us — Silero VAD gates what audio reaches STT, and the script allowlist
  drops foreign-script output. Add one more: drop transcripts matching a small ghost-text
  blocklist (e.g. "subscribe", "thank you for watching" patterns) — cheap and effective.
- Latency note: segmented STT adds (utterance length ÷ 220x) + network ≈ 100–300 ms
  after turn end — acceptable inside the 1.5 s budget; measure in §6.

### 4.4 Voice library seeding (`storage.py` / `data/voices.json`)

Seed Bulbul speakers on first run (same pattern as `VOICE_ID_*` env seeding):
`anushka`, `manisha`, `vidya`, `arya` (F) / `abhilash`, `karun`, `hitesh` (M) — tagged
per language so the call-page picker shows them. Riya persona → shortlist 2 female
voices after a quick listen, mark the rest hidden.

### 4.5 Per-call cost meter (new, small)

- Counters per call: TTS characters sent, STT audio seconds (sum of segment lengths —
  **count billed minimum 10 s/request**, that is Groq's billing floor), LLM tokens
  (Gemini responses carry usage metadata; else estimate chars/4).
- On call end: log line + caption-channel summary
  `cost: ₹X.XX (tts ₹a · stt ₹b · llm ₹c) · N.N min · ₹/min`.
- Implementation: tiny `FrameProcessor` after TTS + hook in the gate; rates from §4.1.
- This is the artefact that turns "~₹0.80/min" from a claim into a measurement.

### 4.6 `.env` / `.env.example`

- Add `GROQ_API_KEY` (new — free tier enough for testing; dev tier for load).
- `SARVAM_API_KEY` already exists (used by saaras STT).

### 4.7 `static/settings.html` + `static/index.html`

- Settings page renders from the schema — new groups appear automatically; verify only.
- No call-page changes: language picker, sliders, voice picker all reused.

---

## 5. Non-goals (Phase 1)

- **No production/bridge changes.** `exotel-elevenlabs-bridge` untouched.
- No Sarvam LLM, no Sarvam STT changes, no removal of any existing provider.
- No barge-in/VAD redesign — current gate is provider-agnostic.
- No auto language detection changes (Whisper hint follows the picker, like today).

---

## 6. Test plan

1. **Smoke (local):** one call per provider pair — (sarvam TTS × groq STT), and mixed
   pairs vs existing providers to isolate any issue.
2. **Language matrix:** scripted 10-turn conversation in hi, te, ta, kn each — check
   native script captions, accent, number/English-loanword pronunciation ("app",
   "trial", "₹99", "English").
3. **Latency:** 20 turns per language; record TTFB p50/p95 from logs; compare against
   ElevenLabs config on the same machine/network.
4. **Robustness:** 60 s silence, background TV noise, "hmm" fragments — expect zero
   ghost transcripts (VAD + allowlist + blocklist).
5. **Cost verification:** 30-min test session; cost meter total vs Sarvam + Groq
   dashboard billing — must agree within ~10%.
6. **Team blind A/B:** same 6 Riya lines rendered via ElevenLabs (current voice) and
   Bulbul (2 shortlisted voices), Hindi + Telugu; 4–5 team members rank; also share
   the Cloud Run link for live calls.

---

## 7. Rollout

1. Implement on this branch → local testing (D1–D2).
2. Deploy to Cloud Run via existing `deploy_cloudrun.sh` (needs `GROQ_API_KEY` added
   to the deploy env) — share link with team for A/B (D3–D4).
3. **Decision gate:** metrics from §2. If pass → write Phase 2 PRD (port winning
   config into the Exotel bridge as a Pipecat pipeline; Bulbul at 8 kHz; A/B 500
   production calls measuring talk-rate + trial conversion vs ElevenLabs before any
   full switch). If fail → document findings, keep branch for reference.

## 8. Risks

| Risk | Mitigation |
|---|---|
| Bulbul voice hurts conversion (the big one) | Phase 1 A/B is exactly this test; Phase 2 gates on real-call conversion, 500-call sample |
| Whisper ghost text reaches LLM | VAD gating + script allowlist (existing) + blocklist (§4.3) |
| Groq/Sarvam rate limits during load | test tier fine for Phase 1; note prod tiers in Phase 2 PRD |
| Flash-Lite 2.5 retirement 16 Oct | already on 3.1 default; budget uses 3.1 rates |
| 3 vendors = 3 failure points | Phase 2 concern (fallback chains); out of scope here |
| Pipecat Sarvam/Groq services less battle-tested than ElevenLabs ones | pin pipecat 1.1.0; smoke tests first; HTTP fallback (`SarvamHttpTTSService`) if WS flaky |

## 9. Milestones

| Day | Deliverable |
|---|---|
| D1 | §4.1 settings + §4.2/4.3 builders wired; local smoke call works |
| D2 | Voice seeds + language matrix pass on hi/te; ghost-text guard |
| D3 | ta/kn tuning + cost meter + latency numbers |
| D4 | Cloud Run deploy, team A/B, decision-gate readout |

---

## 10. Findings — first build + test run (10 Sep 2026)

**What was built:** `TTS_PROVIDER=sarvam` (Bulbul v3, streaming WS) and `STT_PROVIDER=groq`
(Whisper, segmented) in `bot.py`; Sarvam voice picker on the call page; Whisper ghost-text
filter in the gate; per-call cost + latency meter (shown under the call, logged as
`[cost]` / `[latency]` / `[ttfb]`); `/voice-lab` page (render one line across voices, blind
A/B rating, mic → Whisper). Tests: automated end-to-end WebSocket calls (Sarvam-rendered
"user" speech in, Riya audio out) plus direct API probes.

### Changes from the original plan

| Plan said | Reality | Change made |
|---|---|---|
| `bulbul:v2` at ₹15/10K chars | v2 retired: API returns 400 "use bulbul:v3"; v3 = ₹30/10K ([pricing](https://docs.sarvam.ai/api-reference-docs/pricing)) | v3 only; pitch/loudness settings removed; one rate key |
| Groq turbo is enough | Telugu clip: turbo garbled most words, and with no language hint wrote it in **Gujarati script** (dropped by the script gate → user speech lost). large-v3 ≈ saaras | Default `whisper-large-v3` |
| Groq STT adds 100–300 ms | Direct call from Mac: turbo ~250 ms warm (3 s Hindi), large-v3 ~490 ms (4 s Telugu); first call in a call ~600 ms cold | — |
| Bulbul reads mixed text fine | Pure English and codemix are fine, but on the WebSocket Sarvam re-chunks quoted lines ("బోలిఏ: 'I go…'") and rejects the leftover `'` → "must contain at least one character from the allowed languages", one error per quoted line (direct repro: 1 error quoted, 0 with quotes stripped; no clear audio loss) | Strip quotes at word boundaries before sending (apostrophes in I'm/don't kept) |

### Telugu STT accuracy (same clip, truth: నా పేరు రాహుల్. నేను రోజూ ఉదయం తొమ్మిది గంటలకు office కి వెళ్తాను.)

| Engine | Output |
|---|---|
| Groq turbo, hint te | నా పేరు రాహుల నేను రోజు ఉదేయం తొమ్ిది గంటలకు అఉఫిస్ కి వల్తాను |
| Groq turbo, auto | ના પેરુ રાહુલ … (Gujarati script) |
| Groq large-v3, hint te | నా పేరు రాహుల్. నేను రోజు ఉదేయం తొమ్మిది గంటలకు ఓఫిస్ కి వేళ్తాను. |
| Sarvam saaras:v3 | నా పేరు రాహుల్, నేను రోజు ఉదయం తొమ్మిది గంటలకు ఆఫీస్కి వెళ్తాను. |

### End-to-end calls (Mac → local server; Gemini 3.1 flash-lite, 3.4K-token prompt)

| Run | Latency (VAD end → Riya audio) | Breakdown | Cost | Ghost text in 6 s silence |
|---|---|---|---|---|
| Hindi, Groq turbo | 3.18 s (cold) | STT 1.66 · LLM 0.99 · TTS 0.26 | ₹2.09/min | 0 (test-harness miscount fixed) |
| Telugu, Groq turbo | 2.41 s | STT 0.88 · LLM 0.95 · TTS 0.25 | ₹1.23/min | 0 |
| Telugu, Sarvam saaras | 2.50 s | STT 0.73 · LLM 1.01 · TTS 0.31 | STT not priced | 0 |

STT TTFB includes the 0.5 s `VAD_STOP_SECS` wait. Samples are single short calls — directional, not a measurement.

### What this means for the decision gate

- **Cost:** TTS at v3 runs ~₹1.5–1.8/min when Riya talks ~500–600 chars/min, and LLM is
  ~₹0.08 **per turn** because the 3.4K-token prompt is re-sent each turn. Realistic total
  ≈ **₹1.5–2/min** — misses the ≤₹1 target, but is still ~75–80% below ₹7–9.
- **Latency:** ~2.4 s, not ≤1.5 s. Groq and Saaras land within 0.1 s of each other; the big
  levers are provider-agnostic: `VAD_STOP_SECS` 0.5 → 0.3, and Gemini TTFB (~1 s; prompt
  size / context caching).
- **STT choice:** at large-v3 pricing plus the 10 s billing floor, Groq's cost edge over
  Saaras mostly disappears, and Saaras is already integrated and best on Telugu.
- **Open:** voice quality (team blind A/B in `/voice-lab`) is untested. The
  ElevenLabs keys in `.env` are rejected by the global, India and EU endpoints, so the
  ElevenLabs side of the A/B needs a working key.
- **Needs an owner decision:** keep ≤₹1/min as the gate (→ likely no-go), or re-set it
  to ~₹2/min.

---

## 11. Direction change — TTS on Google Chirp3-HD (22 Sep 2026)

Owner decision: build the cheaper stack on **Google Cloud TTS Chirp3-HD** (`TTS_PROVIDER=google`,
already in the repo) instead of Sarvam Bulbul. Sarvam stays wired as an option.

**Changes:** cost meter prices Chirp3-HD ($30 / 1M chars) and Sarvam saaras STT (₹30/hr, on
call length — upper bound, since the mic is streamed the whole call); Voice Lab renders
Chirp3-HD for A/B; fixed picker voice `Callirhoe` → `Callirrhoe` (the old spelling failed
the call). All 30 Chirp3-HD voices exist for hi-IN, te-IN, ta-IN, kn-IN and en-IN.

**End-to-end calls (Chirp3-HD Aoede + Sarvam saaras STT, Gemini 3.1 flash-lite):**

| Run | STT transcript | Latency | Breakdown | Cost | Ghost / TTS errors |
|---|---|---|---|---|---|
| Telugu | exact | 2.33 s | STT 0.75 · LLM 0.84 · TTS 0.17 | ₹1.97/min | 0 / 0 |
| Hindi | exact | 2.55 s | STT 0.70 · LLM 1.11 · TTS 0.14 | ₹2.61/min | 0 / 0 |

Estimated 1-min call at 550 chars + 5 turns: **₹2.37/min** (TTS ₹1.45 · STT ≤₹0.50 · LLM ₹0.41).
Chirp costs about the same as Bulbul v3 (≈₹26 vs ₹30 per 10K chars), reads quoted English
without errors, and is ~0.1 s faster to first audio.

**Next levers (provider-agnostic):** shorter Riya replies (TTS cost scales with chars — the
Hindi reply ran ~680 chars/min); `VAD_STOP_SECS` 0.5 → 0.3; Gemini context caching for the
3.4K-token prompt; confirm saaras billing on the Sarvam dashboard.

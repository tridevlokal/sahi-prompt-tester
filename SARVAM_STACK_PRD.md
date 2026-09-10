# PRD — "Cheap Stack": Sarvam Bulbul TTS + Groq Whisper STT + Gemini Flash-Lite

**Branch:** `feat/sarvam-cheap-stack` · **Status:** Draft · **Date:** 10 Sep 2026
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

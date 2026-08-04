# Google TTS (Chirp3-HD) — Implementation Spec (MANDATORY)

**To:** Voice/backend dev
**From:** Tridev
**Status:** The current production implementation is **incorrect and must be replaced**
with the design below. This design is running and verified in the `sahi-prompt-tester`
repo (branch `feat/gemini-stt-tts`, file `bot.py`) — Hindi, Telugu, Tamil, Kannada,
Malayalam and English all sound native there. Implement it **exactly as specified**.
Do not keep the current settings structure.

---

## 1. What you did wrong (and must remove)

Your current settings:

```
GOOGLE_TTS_LANGUAGE_CODE = hi-IN
GOOGLE_TTS_VOICE         = hi-IN-Chirp3-HD-Sulafat
GOOGLE_TTS_PITCH         = 0.0
```

Three mistakes:

1. **You hardcoded a full voice id (`hi-IN-Chirp3-HD-Sulafat`).** Chirp3-HD voices
   are **language-locked** — this is a *Hindi* voice. Every Telugu/Tamil/Kannada
   session is being spoken by a Hindi voice. That is exactly why Telugu output is
   garbage. A fixed voice id can only ever be correct for one language.
2. **You made language a global admin setting (`GOOGLE_TTS_LANGUAGE_CODE`).**
   Language is a **per-session property** (the learner picks it). A global language
   code guarantees every non-matching session is wrong.
3. **You exposed `GOOGLE_TTS_PITCH`.** Chirp3-HD does **not support pitch or SSML**.
   The setting does nothing except mislead whoever edits it.

**Delete all three settings.** They must not exist after this change.

---

## 2. The required design (non-negotiable)

### Rule

> Admin stores ONLY a voice **persona name** (e.g. `Sulafat`). At **session start**,
> the runtime composes the full voice id from the session's language:
>
> ```
> voice_id = f"{LANG_CODE[session.language]}-Chirp3-HD-{persona}"
> ```

Chirp3-HD ships the **same persona names in every Indian language** — so one
persona setting gives the same "Riya sound" in every language, and every session
automatically gets the voice built for its language (native phonetics + accent).

### Language map (implement verbatim)

```python
GOOGLE_LANG_CODE = {
    "hin": "hi-IN", "tel": "te-IN", "tam": "ta-IN",
    "kan": "kn-IN", "mal": "ml-IN", "eng": "en-IN",
}
```

| Session language | Composed voice (persona = Sulafat) |
|---|---|
| Hindi | `hi-IN-Chirp3-HD-Sulafat` |
| Telugu | `te-IN-Chirp3-HD-Sulafat` |
| Tamil | `ta-IN-Chirp3-HD-Sulafat` |
| Kannada | `kn-IN-Chirp3-HD-Sulafat` |
| Malayalam | `ml-IN-Chirp3-HD-Sulafat` |
| English | `en-IN-Chirp3-HD-Sulafat` (Indian accent — do NOT use en-US) |

Verified on our GCP project via `list_voices()`: hi-IN has 46 Chirp3-HD voices,
te-IN 34, ta-IN 38, kn-IN 38, ml-IN 38 — personas exist across all of them.

### Auto-selection flow (how the voice "just works")

```
learner picks session language ("tel")
        │
        ├─► LLM gets language directive  → Riya's TEXT is Telugu script
        │
        └─► GOOGLE_LANG_CODE["tel"] = "te-IN"
                    │
                    ▼
        voice_id = "te-IN-Chirp3-HD-" + persona   (composed at session start)
                    │
                    ▼
        GoogleTTSService(voice_id=..., language=te-IN, sample_rate=24000)
```

The learner and the admin never see or set a language code for TTS. It follows the
session automatically.

---

## 3. Settings — final state after your change

| Setting | Type | Default | Purpose |
|---|---|---|---|
| `GOOGLE_TTS_VOICE_NAME` | dropdown (list in §4) | `Sulafat` | Persona name ONLY. Never a full voice id. |
| `GOOGLE_TTS_MODEL` | dropdown | `chirp3-hd` | unchanged |
| `GOOGLE_TTS_SPEED` | float 0.25–2.0 | `1.0` | Chirp3-HD speaking-rate support is limited — verify before relying on non-1.0 values. |
| `GOOGLE_TTS_FALLBACK_LANGUAGE` | dropdown | `hi-IN` | Used ONLY if a session has no language set. |

Removed: `GOOGLE_TTS_LANGUAGE_CODE`, `GOOGLE_TTS_VOICE` (full id), `GOOGLE_TTS_PITCH`.

Dropdown help text (use verbatim):

> Chirp3-HD voice persona. The same persona exists in every Indian language; the
> runtime builds the final voice as `<session-language>-Chirp3-HD-<persona>`
> (e.g. a Telugu session with persona `Sulafat` uses `te-IN-Chirp3-HD-Sulafat`).
> Do NOT enter a full voice id here.

---

## 4. Persona dropdown choices

All personas exist in hi-IN / te-IN / ta-IN / kn-IN / ml-IN / en-IN.
Tutor-friendly first; vibe labels from Google docs:

```
Achird — Friendly        Sulafat — Warm          Aoede — Breezy
Leda — Youthful          Callirhoe — Easy-going  Vindemiatrix — Gentle
Kore — Firm              Puck — Upbeat           Autonoe — Bright
Zephyr — Bright          Charon — Informative    Sadachbia — Lively
Laomedeia — Upbeat       Achernar — Soft         Despina — Smooth
Algieba — Smooth         Iapetus — Clear         Erinome — Clear
Umbriel — Easy-going     Enceladus — Breathy     Schedar — Even
Gacrux — Mature          Orus — Firm             Fenrir — Excitable
Rasalgethi — Informative Alnilam — Firm          Pulcherrima — Forward
Zubenelgenubi — Casual   Sadaltager — Knowledgeable  Algenib — Gravelly
```

Default for Riya: **`Sulafat`** (Warm). A/B candidate: **`Aoede`** (Breezy).

On save, validate the persona with `texttospeech.list_voices()` against every
supported language and reject names missing in any of them.

---

## 5. Implementation steps (do in this order)

1. **Delete** `GOOGLE_TTS_LANGUAGE_CODE`, `GOOGLE_TTS_VOICE`, `GOOGLE_TTS_PITCH`
   from settings + all code paths that read them.
2. **Add** `GOOGLE_TTS_VOICE_NAME` (dropdown, §4) and `GOOGLE_TTS_FALLBACK_LANGUAGE`.
3. **Add** the `GOOGLE_LANG_CODE` map (§2) to the voice pipeline module.
4. **At session start**, compose the voice id and construct the TTS service:

```python
lang_key = session.language                       # "tel", "hin", ...
code     = GOOGLE_LANG_CODE.get(lang_key) or settings.GOOGLE_TTS_FALLBACK_LANGUAGE
persona  = settings.GOOGLE_TTS_VOICE_NAME         # "Sulafat"
voice_id = f"{code}-Chirp3-HD-{persona}"          # e.g. te-IN-Chirp3-HD-Sulafat

tts = GoogleTTSService(                            # pipecat STREAMING service
    credentials_path=GCP_SERVICE_ACCOUNT_JSON,
    voice_id=voice_id,
    params=GoogleTTSService.InputParams(language=LANG_ENUM[lang_key]),
    sample_rate=24000,                             # Chirp3-HD native output
)
```

5. **Requirements:**
   - Use the **streaming** `GoogleTTSService` (measured ~0.2s time-to-first-audio).
     The HTTP service is only for Neural2 — not for Chirp3-HD.
   - `sample_rate=24000` end-to-end. A mismatch distorts audio.
   - Keep the TTS stop-frame idle timeout **≥ 3.0s** (same reasoning as
     `ELEVENLABS_TTS_STOP_FRAME_TIMEOUT=3.0`) — a lower value fires
     `TTSStoppedFrame` between sentence fragments and breaks the voice
     mid-sentence (the "choppy" bug).
   - Do not split text into sub-sentence fragments (quotes/ellipses) before TTS.

---

## 6. Acceptance tests (all must pass before merge)

1. Telugu session → log shows voice id starting `te-IN-` → Telugu sounds native.
   *(This is the regression test for the current bug: today a Telugu session
   speaks through `hi-IN-Chirp3-HD-Sulafat`.)*
2. Hindi / Tamil / Kannada / Malayalam sessions → `hi-IN-` / `ta-IN-` / `kn-IN-` /
   `ml-IN-` prefixed voices respectively.
3. Changing `GOOGLE_TTS_VOICE_NAME` changes the persona in ALL languages at once.
4. Code-mix line ("మీ **job interview** కోసమా?") is spoken by the same
   session-language voice, embedded English pronounced with Indian accent.
5. Grep the codebase: `GOOGLE_TTS_LANGUAGE_CODE` and `GOOGLE_TTS_PITCH` return
   zero matches.

---

## 7. Cost (for planning)

Chirp3-HD: **$30 / 1M characters** (≈ ₹28.5 per 10k chars @ ₹95/USD), first
**1M chars/month free**. ≈ ₹2.6 per minute of speech — cheaper than ElevenLabs
Flash (~₹4.7/min) and v3 (~₹9.5/min); comparable to Smallest (~₹1.9/min).

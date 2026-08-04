# PRD: Google Cloud TTS (Chirp3-HD) — Correct Multilingual Implementation

**For:** Sahi English voice pipeline (Riya)
**Problem owner:** Voice/backend dev
**Reference implementation:** `sahi-prompt-tester` repo, branch `feat/gemini-stt-tts` (`bot.py`) — verified working locally for Hindi, Telugu, Tamil, Kannada, Malayalam, English.

---

## 1. The bug we are fixing (read this first)

Current production settings:

```
GOOGLE_TTS_LANGUAGE_CODE = hi-IN
GOOGLE_TTS_VOICE         = hi-IN-Chirp3-HD-Sulafat   ← full voice id, hardcoded
```

**This is wrong, and it is exactly why Telugu (and Tamil/Kannada) sound pathetic.**

Chirp3-HD voices are **language-locked**. `hi-IN-Chirp3-HD-Sulafat` is a *Hindi* voice.
When a learner runs a **Telugu** session, Riya's text is in Telugu script — but the
pipeline still synthesises it with the *Hindi* voice. A Hindi voice reading Telugu
text produces wrong phonetics/accent → garbage-sounding Telugu.

A static `GOOGLE_TTS_VOICE` can only ever be right for ONE language. Every other
language a user picks will sound broken. **The voice id must be composed per
session, from the session's language.**

---

## 2. The correct design (what works in our local tester)

### Core rule

> **Store only the voice PERSONA NAME (e.g. `Sulafat`). Compose the full voice id
> at session start:**
>
> `voice_id = f"{session_lang_code}-Chirp3-HD-{persona_name}"`

Chirp3-HD ships the **same persona names in every Indian language**. `Sulafat`
exists as `hi-IN-Chirp3-HD-Sulafat`, `te-IN-Chirp3-HD-Sulafat`,
`ta-IN-Chirp3-HD-Sulafat`, … So one persona setting gives a consistent "Riya
sound" across all languages, and each session automatically gets the voice built
for its language — native phonetics, native accent.

### Language map (session language → BCP-47 code)

| App language | BCP-47 code | Example composed voice |
|---|---|---|
| Hindi   | `hi-IN` | `hi-IN-Chirp3-HD-Sulafat` |
| Telugu  | `te-IN` | `te-IN-Chirp3-HD-Sulafat` |
| Tamil   | `ta-IN` | `ta-IN-Chirp3-HD-Sulafat` |
| Kannada | `kn-IN` | `kn-IN-Chirp3-HD-Sulafat` |
| Malayalam | `ml-IN` | `ml-IN-Chirp3-HD-Sulafat` |
| English | `en-IN` (Indian accent) or `en-US` | `en-IN-Chirp3-HD-Sulafat` |

Verified voice counts per language on our GCP project (via `list_voices()`):
hi-IN: 46, te-IN: 34, ta-IN: 38, kn-IN: 38, ml-IN: 38 Chirp3-HD voices.

### Where the session language comes from (auto-selection flow)

```
user picks language on app/screen (or session default)
        │
        ▼
session.language  ─►  LANG_CODE map  ─►  "te-IN"
        │                                   │
        ▼                                   ▼
LLM directive ("session is Telugu")   voice_id = "te-IN-Chirp3-HD-" + persona
        │                                   │
        ▼                                   ▼
Riya's text comes out in Telugu  ─►  GoogleTTSService(voice_id=..., language=te-IN)
```

No user-facing "language code" setting exists anywhere. The ONLY admin setting is
the persona name. Language always follows the session.

---

## 3. Settings spec (what Django admin should become)

### Remove
- `GOOGLE_TTS_LANGUAGE_CODE` — **delete.** Language is a session property, not a
  global setting. Keeping it invites exactly the bug we just had.
- `GOOGLE_TTS_VOICE` (full id) — **delete / replace** with persona name below.
- `GOOGLE_TTS_PITCH` — Chirp3-HD does **not** support pitch (or SSML). This
  setting silently does nothing (or errors) on Chirp3-HD; it only applies to
  Neural2/WaveNet voices. Remove it to avoid confusion.

### Add / keep

| Setting | Type | Default | Notes |
|---|---|---|---|
| `GOOGLE_TTS_VOICE_NAME` | **dropdown** (see §4) | `Sulafat` | Persona name ONLY. Full id is composed per session. |
| `GOOGLE_TTS_MODEL` | dropdown | `chirp3-hd` | Keep as-is. |
| `GOOGLE_TTS_SPEED` | float 0.25–2.0 | `1.0` | Speaking-rate support on Chirp3-HD is limited — verify on current API before relying on it; prefer 1.0. |
| `GOOGLE_TTS_FALLBACK_LANGUAGE` | dropdown | `hi-IN` | Used ONLY if a session somehow has no language. |

### Dropdown description text (paste-ready)

> **GOOGLE_TTS_VOICE_NAME** — Chirp3-HD voice persona. The same persona exists in
> every Indian language; the runtime builds the final voice as
> `<session-language>-Chirp3-HD-<persona>` (e.g. a Telugu session with persona
> `Sulafat` uses `te-IN-Chirp3-HD-Sulafat`). Do NOT enter a full voice id here.

---

## 4. Voice persona dropdown (choices)

All personas below exist in hi-IN / te-IN / ta-IN / kn-IN / ml-IN / en-IN.
Vibe labels are from Google's docs. Tutor-friendly picks first:

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

Recommended default for Riya (female, warm tutor): **`Sulafat`** (Warm) or
**`Aoede`** (Breezy). A/B these two.

> Optional hardening: on save, validate the persona against
> `texttospeech.list_voices()` for each supported language and reject names that
> are missing in any of them.

---

## 5. Reference implementation (working code, from our tester)

```python
# --- language map (module level) ---
GOOGLE_LANG_CODE = {
    "hin": "hi-IN", "tel": "te-IN", "tam": "ta-IN",
    "kan": "kn-IN", "mal": "ml-IN", "eng": "en-IN",
}
GOOGLE_LANG = {   # pipecat Language enums for the `language` param
    "hin": Language.HI_IN, "tel": Language.TE_IN, "tam": Language.TA_IN,
    "kan": Language.KN_IN, "mal": Language.ML_IN, "eng": Language.EN_IN,
}

# --- at session/call start ---
lang_key  = session.language                    # "tel", "hin", ...
code      = GOOGLE_LANG_CODE.get(lang_key, settings.GOOGLE_TTS_FALLBACK_LANGUAGE)
persona   = settings.GOOGLE_TTS_VOICE_NAME      # "Sulafat"
voice_id  = f"{code}-Chirp3-HD-{persona}"       # e.g. te-IN-Chirp3-HD-Sulafat

tts = GoogleTTSService(                          # pipecat streaming service
    credentials_path=GCP_SERVICE_ACCOUNT_JSON,   # service-account auth
    voice_id=voice_id,
    params=GoogleTTSService.InputParams(language=GOOGLE_LANG.get(lang_key)),
    sample_rate=24000,                           # Chirp3-HD native output
)
```

Notes:
- **Auth:** GCP service-account JSON (`GOOGLE_APPLICATION_CREDENTIALS` or
  `credentials_path`). The Cloud Text-to-Speech API must be enabled on the project.
- **Sample rate:** Chirp3-HD outputs 24 kHz — set `sample_rate=24000` end-to-end
  (mismatch = chipmunk/slow audio).
- **Use the streaming `GoogleTTSService`** (not the HTTP one) — measured
  **~0.2s time-to-first-audio** in our tester. Neural2 voices need the HTTP
  service; Chirp3-HD needs streaming.

---

## 6. Behaviour requirements (acceptance criteria)

1. **Telugu session** → voice id starts `te-IN-` → spoken Telugu sounds native.
   Same for Hindi / Tamil / Kannada / Malayalam. *(This is the bug-fix test:
   currently a Telugu session speaks through a hi-IN voice.)*
2. Changing the session language mid-product (new session) changes ONLY the
   language prefix; the persona stays the same → Riya "sounds like the same
   person" in every language.
3. Changing `GOOGLE_TTS_VOICE_NAME` in admin changes the persona for ALL
   languages at once — no per-language voice settings needed.
4. English words embedded in an Indian-language sentence (code-mix, e.g.
   "మీ **job interview** కోసమా?") are spoken by the same language voice —
   Chirp3-HD handles embedded English with an Indian accent. No special handling.
5. No admin setting can lock the pipeline to one language (i.e.
   `GOOGLE_TTS_LANGUAGE_CODE` no longer exists).

---

## 7. Known gotchas (learned the hard way)

| Gotcha | Detail |
|---|---|
| **Full voice id in settings** | The root cause of bad Telugu. Never store `hi-IN-Chirp3-HD-X` — store `X`. |
| **Chirp3-HD ≠ SSML/pitch** | No SSML, no pitch. Speaking-rate support is limited — verify before exposing a speed knob. |
| **Choppy mid-sentence audio** | If the TTS service pushes `TTSStoppedFrame` between sentence fragments (idle timeout too low), the voice breaks mid-sentence. Keep the stop-frame idle timeout ≥ 3.0s (same reasoning as `ELEVENLABS_TTS_STOP_FRAME_TIMEOUT=3.0`). Also avoid splitting text into sub-sentence fragments (quotes/ellipses) before TTS. |
| **Wrong sample rate** | Chirp3-HD = 24 kHz. Mismatched pipeline sample rate distorts audio. |
| **en-IN vs en-US** | For an Indian-audience tutor, prefer `en-IN` voices for English sessions (Indian accent, consistent with code-mix pronunciation). |

---

## 8. Cost note

Chirp3-HD = **$30 / 1M characters** (≈ ₹28.5 per 10k chars @ ₹95/USD),
**first 1M chars/month free**. ≈ ₹2.6/min of speech — cheaper than ElevenLabs
(Flash ~₹4.7/min, v3 ~₹9.5/min), comparable to Smallest (~₹1.9/min).

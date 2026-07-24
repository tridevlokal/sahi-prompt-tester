"""
settings_store.py — configurable voice-agent settings (Django-admin-style).

A flat list of tunable settings (LLM / TTS / STT / VAD / turn-taking), each with
a name, type, default, dropdown choices, and a human description. Values persist
to data/settings.json; anything not overridden falls back to its schema default.

Everything here is ElevenLabs-only (STT + TTS) — Sarvam is intentionally gone.

Public API:
  SCHEMA                 — ordered list of setting definitions (for the UI)
  list_settings()        — schema merged with current values (for the UI)
  get_all()              — {key: typed value} with defaults applied (for bot.py)
  update(values: dict)   — bulk-set + persist; returns the fresh get_all()
  elevenlabs_urls(cfg)   — (http_base, ws_stream_url, ws_stt_host) for a region
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"

# ElevenLabs endpoints per data-residency region.
REGION_HOSTS = {
    "global": "api.elevenlabs.io",
    "india": "api.in.residency.elevenlabs.io",
    "eu": "api.eu.residency.elevenlabs.io",
}

# Curated Smallest.ai voices (lightning-v3.1) for the test-page picker.
# The "full" group speaks the whole Indian set (hi/en/ta/te/kn/ml) — use these
# for Telugu/Tamil/Kannada/Malayalam. The "hi" group is Hindi/English only.
# Each carries its language codes so the picker can show them.
_FULL = ["hi", "en", "ta", "te", "kn", "ml"]
_HI = ["hi", "en"]
SMALLEST_VOICES = [
    # --- Female · full Indian set (Telugu-capable) ---
    {"voice_id": "anitha",     "name": "Anitha (F)",     "gender": "female", "languages": _FULL},
    {"voice_id": "padmaja",    "name": "Padmaja (F)",    "gender": "female", "languages": _FULL},
    {"voice_id": "deepashri",  "name": "Deepashri (F)",  "gender": "female", "languages": _FULL},
    {"voice_id": "rajeshwari", "name": "Rajeshwari (F)", "gender": "female", "languages": _FULL},
    {"voice_id": "chandana",   "name": "Chandana (F)",   "gender": "female", "languages": _FULL},
    {"voice_id": "sandhya",    "name": "Sandhya (F)",    "gender": "female", "languages": _FULL},
    {"voice_id": "lavanya",    "name": "Lavanya (F)",    "gender": "female", "languages": _FULL},
    {"voice_id": "nandini",    "name": "Nandini (F)",    "gender": "female", "languages": _FULL},
    {"voice_id": "shruthi",    "name": "Shruthi (F)",    "gender": "female", "languages": _FULL},
    # --- Female · Hindi/English ---
    {"voice_id": "maithili",   "name": "Maithili (F)",   "gender": "female", "languages": _HI},
    {"voice_id": "niharika",   "name": "Niharika (F)",   "gender": "female", "languages": _HI},
    {"voice_id": "aditi",      "name": "Aditi (F)",      "gender": "female", "languages": _HI},
    {"voice_id": "radhika",    "name": "Radhika (F)",    "gender": "female", "languages": _HI},
    {"voice_id": "nikita",     "name": "Nikita (F)",     "gender": "female", "languages": _HI},
    {"voice_id": "meera",      "name": "Meera (F)",      "gender": "female", "languages": _HI},
    {"voice_id": "tanvi",      "name": "Tanvi (F)",      "gender": "female", "languages": _HI},
    {"voice_id": "kiara",      "name": "Kiara (F)",      "gender": "female", "languages": _HI},
    {"voice_id": "khushi",     "name": "Khushi (F)",     "gender": "female", "languages": _HI},
    {"voice_id": "sana",       "name": "Sana (F)",       "gender": "female", "languages": _HI},
    {"voice_id": "divya",      "name": "Divya (F)",      "gender": "female", "languages": _HI},
    {"voice_id": "kavya",      "name": "Kavya (F)",      "gender": "female", "languages": _HI},
    {"voice_id": "aanya",      "name": "Aanya (F)",      "gender": "female", "languages": _HI},
    {"voice_id": "sakshi",     "name": "Sakshi (F)",     "gender": "female", "languages": _HI},
    {"voice_id": "sunidhi",    "name": "Sunidhi (F)",    "gender": "female", "languages": _HI},
    # --- Male · full Indian set (Telugu-capable) ---
    {"voice_id": "raju",       "name": "Raju (M)",       "gender": "male",   "languages": _FULL},
    {"voice_id": "jeevan",     "name": "Jeevan (M)",     "gender": "male",   "languages": _FULL},
    {"voice_id": "girish",     "name": "Girish (M)",     "gender": "male",   "languages": _FULL},
    {"voice_id": "shrihari",   "name": "Shrihari (M)",   "gender": "male",   "languages": _FULL},
]


def smallest_voices() -> list[dict]:
    """Curated Smallest.ai voices for the test-page picker."""
    return [dict(v) for v in SMALLEST_VOICES]


# --- Schema -----------------------------------------------------------------
# type: "select" | "number" | "int" | "bool" | "text"
# select entries carry `choices` = [{"value", "label"}]
SCHEMA: list[dict[str, Any]] = [
    # ---------- LLM (Google Gemini) ----------
    {
        "key": "GEMINI_MODEL",
        "label": "GEMINI_MODEL",
        "group": "LLM (Gemini)",
        "type": "select",
        "default": "gemini-3.1-flash-lite",
        "choices": [
            {"value": "gemini-3.1-flash-lite",
             "label": "gemini-3.1-flash-lite — latest gen, lowest cost/latency (recommended)"},
            {"value": "gemini-2.5-flash-lite",
             "label": "gemini-2.5-flash-lite — low cost and latency"},
            {"value": "gemini-2.5-flash",
             "label": "gemini-2.5-flash — better reasoning, higher cost/latency"},
            {"value": "gemini-2.0-flash",
             "label": "gemini-2.0-flash — previous generation"},
        ],
        "description": "Gemini model for Riya's responses. gemini-3.1-flash-lite is the "
                       "latest generation with the lowest cost and latency (recommended).",
    },
    {
        "key": "GEMINI_TEMPERATURE",
        "label": "GEMINI_TEMPERATURE",
        "group": "LLM (Gemini)",
        "type": "number", "min": 0.0, "max": 2.0, "step": 0.05,
        "default": 0.8,
        "description": "Sampling temperature. Range 0.0-2.0. Lower = more deterministic, "
                       "higher = more varied. 0.8 gives natural conversational variety.",
    },
    {
        "key": "GEMINI_MAX_TOKENS",
        "label": "GEMINI_MAX_TOKENS",
        "group": "LLM (Gemini)",
        "type": "int", "min": 100, "max": 2000, "step": 50,
        "default": 500,
        "description": "Maximum LLM output tokens per turn. Range 100-2000. 500 fits one "
                       "English sentence plus a translation. Raise if replies get cut off.",
    },

    # ---------- TTS provider ----------
    {
        "key": "TTS_PROVIDER",
        "label": "TTS_PROVIDER",
        "group": "TTS provider",
        "type": "select",
        "default": "elevenlabs",
        "choices": [
            {"value": "elevenlabs", "label": "elevenlabs — ElevenLabs (streaming + eleven_v3 HTTP)"},
            {"value": "smallest", "label": "smallest — Smallest.ai Waves (native WebSocket, low latency, Indian langs)"},
        ],
        "description": "Which TTS engine synthesises Riya's voice. 'smallest' uses Smallest.ai's "
                       "WebSocket streaming (lightning-v3.1) — natural Hindi/Tamil/Telugu/Kannada/"
                       "Malayalam at low latency. 'elevenlabs' uses the ElevenLabs settings below.",
    },

    # ---------- TTS (Smallest.ai) ----------
    {
        "key": "SMALLEST_MODEL",
        "label": "SMALLEST_MODEL",
        "group": "TTS (Smallest.ai)",
        "type": "select",
        "default": "lightning-v3.1",
        "choices": [
            {"value": "lightning-v3.1", "label": "lightning-v3.1 — latest conversational, 15 languages (recommended)"},
            {"value": "lightning-v2", "label": "lightning-v2 — previous gen (supports consistency/similarity/enhancement)"},
        ],
        "description": "Smallest.ai Waves model (used only when TTS_PROVIDER = smallest). "
                       "lightning-v3.1 is the newest conversational model with mid-sentence "
                       "language switching.",
    },
    {
        "key": "SMALLEST_VOICE",
        "label": "SMALLEST_VOICE",
        "group": "TTS (Smallest.ai)",
        "type": "text",
        "default": "anitha",
        "description": "Smallest.ai voice_id (used only when TTS_PROVIDER = smallest; overrides the "
                       "picked ElevenLabs voice). Telugu-capable female voices: anitha, shruthi, "
                       "padmaja, chandana, sandhya, lavanya, nandini. Male: raju, shrihari, jeevan.",
    },
    {
        "key": "SMALLEST_LANGUAGE",
        "label": "SMALLEST_LANGUAGE",
        "group": "TTS (Smallest.ai)",
        "type": "select",
        "default": "hi",
        "choices": [
            {"value": "hi", "label": "hi — Hindi (handles Hindi+English code-mix well)"},
            {"value": "te", "label": "te — Telugu"},
            {"value": "ta", "label": "ta — Tamil"},
            {"value": "kn", "label": "kn — Kannada"},
            {"value": "ml", "label": "ml — Malayalam"},
            {"value": "mr", "label": "mr — Marathi"},
            {"value": "gu", "label": "gu — Gujarati"},
            {"value": "en", "label": "en — English"},
        ],
        "description": "Language sent to Smallest.ai (REQUIRED — the API rejects an empty language). "
                       "lightning-v3.1 still auto-switches mid-sentence, but this sets the primary "
                       "language. For a Telugu tutor pick 'te'; for Hindi-English code-mix pick 'hi'.",
    },
    {
        "key": "SMALLEST_SPEED",
        "label": "SMALLEST_SPEED",
        "group": "TTS (Smallest.ai)",
        "type": "number", "min": 0.5, "max": 2.0, "step": 0.05,
        "default": 1.0,
        "description": "Smallest.ai speaking speed multiplier. Range 0.5-2.0. 1.0 = normal.",
    },

    # ---------- TTS (ElevenLabs) ----------
    {
        "key": "ELEVENLABS_TTS_MODEL",
        "label": "ELEVENLABS_TTS_MODEL",
        "group": "TTS (ElevenLabs)",
        "type": "select",
        "default": "eleven_flash_v2_5",
        "choices": [
            {"value": "eleven_flash_v2_5",
             "label": "eleven_flash_v2_5 — fastest, lowest latency (streaming, recommended)"},
            {"value": "eleven_turbo_v2_5",
             "label": "eleven_turbo_v2_5 — fast, better quality (streaming)"},
            {"value": "eleven_multilingual_v2",
             "label": "eleven_multilingual_v2 — best multilingual, higher latency (streaming)"},
            {"value": "eleven_v3",
             "label": "eleven_v3 — conversational + emotion tags, HIGH latency (HTTP, non-streaming)"},
        ],
        "description": "ElevenLabs TTS model. flash/turbo/multilingual run on the low-latency "
                       "streaming WebSocket. eleven_v3 supports emotion/audio tags "
                       "([excited], [whispers], [laughs]) and the widest language set, but only "
                       "over the slower HTTP path — the app switches automatically when selected. "
                       "All models speak Hindi and Indian languages; v3 is only needed for emotion tags.",
    },
    {
        "key": "ELEVENLABS_TTS_LANGUAGE",
        "label": "ELEVENLABS_TTS_LANGUAGE",
        "group": "TTS (ElevenLabs)",
        "type": "text",
        "default": "",
        "description": "Optional target language code for multilingual/v3 models (e.g. hi, ta, "
                       "te, kn, en). Leave blank to let ElevenLabs auto-detect from the text. "
                       "Ignored by flash/turbo mono-lingual-tuned voices.",
    },
    {
        "key": "ELEVENLABS_TTS_SPEED",
        "label": "ELEVENLABS_TTS_SPEED",
        "group": "TTS (ElevenLabs)",
        "type": "number", "min": 0.7, "max": 1.2, "step": 0.05,
        "default": 1.0,
        "description": "Speaking speed multiplier. Range 0.7-1.2. 1.0 = normal. 0.85 = slightly "
                       "slower, clearer for non-native learners. (The test page slider overrides this.)",
    },
    {
        "key": "ELEVENLABS_TTS_STABILITY",
        "label": "ELEVENLABS_TTS_STABILITY",
        "group": "TTS (ElevenLabs)",
        "type": "number", "min": 0.0, "max": 1.0, "step": 0.05,
        "default": 0.5,
        "description": "Voice stability. Range 0.0-1.0. Higher = more consistent, lower = more "
                       "expressive but less predictable. 0.5 is balanced for tutoring.",
    },
    {
        "key": "ELEVENLABS_TTS_SIMILARITY_BOOST",
        "label": "ELEVENLABS_TTS_SIMILARITY_BOOST",
        "group": "TTS (ElevenLabs)",
        "type": "number", "min": 0.0, "max": 1.0, "step": 0.05,
        "default": 0.8,
        "description": "Adherence to the original voice identity. Range 0.0-1.0. Higher = sounds "
                       "more like the target voice. 0.8 gives strong consistency across a session.",
    },
    {
        "key": "ELEVENLABS_TTS_STYLE",
        "label": "ELEVENLABS_TTS_STYLE",
        "group": "TTS (ElevenLabs)",
        "type": "number", "min": 0.0, "max": 1.0, "step": 0.05,
        "default": 0.0,
        "description": "Style exaggeration. Range 0.0-1.0. 0.0 = flat natural delivery (best for "
                       "a tutor). Higher = more dramatic, can sound unnatural.",
    },
    {
        "key": "ELEVENLABS_TTS_USE_SPEAKER_BOOST",
        "label": "ELEVENLABS_TTS_USE_SPEAKER_BOOST",
        "group": "TTS (ElevenLabs)",
        "type": "bool",
        "default": True,
        "description": "Enable ElevenLabs speaker-boost enhancement. Increases similarity to the "
                       "target speaker for a small quality gain; disable only if latency is critical.",
    },
    {
        "key": "ELEVENLABS_TTS_APPLY_TEXT_NORMALIZATION",
        "label": "ELEVENLABS_TTS_APPLY_TEXT_NORMALIZATION",
        "group": "TTS (ElevenLabs)",
        "type": "select",
        "default": "on",
        "choices": [
            {"value": "on", "label": "on — always normalise numbers/abbreviations (recommended for mixed-script)"},
            {"value": "auto", "label": "auto — normalise only when ElevenLabs detects it helps"},
            {"value": "off", "label": "off — send text exactly as-is"},
        ],
        "description": "Text normalisation before synthesis. on = always normalise numbers, "
                       "abbreviations and mixed-script tokens (recommended for Hindi-English).",
    },

    # ---------- STT (ElevenLabs) ----------
    {
        "key": "ELEVENLABS_STT_MODEL",
        "label": "ELEVENLABS_STT_MODEL",
        "group": "STT (ElevenLabs)",
        "type": "select",
        "default": "scribe_v2_realtime",
        "choices": [
            {"value": "scribe_v2_realtime",
             "label": "scribe_v2_realtime — real-time WebSocket, ~50ms final transcript (recommended)"},
            {"value": "scribe_v2",
             "label": "scribe_v2 — batch, higher accuracy, ~300-500ms delay"},
        ],
        "description": "ElevenLabs speech-to-text model. scribe_v2_realtime commits instantly on "
                       "VAD stop (lowest latency, 99+ languages). scribe_v2 is batch — more "
                       "accurate but adds processing delay after speech ends.",
    },
    {
        "key": "ELEVENLABS_STT_COMMIT_STRATEGY",
        "label": "ELEVENLABS_STT_COMMIT_STRATEGY",
        "group": "STT (ElevenLabs)",
        "type": "select",
        "default": "manual",
        "choices": [
            {"value": "manual", "label": "manual — Pipecat's Silero VAD decides turn ends (recommended)"},
            {"value": "vad", "label": "vad — ElevenLabs server-side VAD decides turn ends"},
        ],
        "description": "How the realtime STT segments speech. manual = local Silero VAD controls "
                       "commits (consistent with the rest of the pipeline). vad = let ElevenLabs decide.",
    },
    {
        "key": "ELEVENLABS_STT_LANGUAGE",
        "label": "ELEVENLABS_STT_LANGUAGE",
        "group": "STT (ElevenLabs)",
        "type": "select",
        "default": "auto",
        "choices": [
            {"value": "auto", "label": "auto — all languages (Hindi/Telugu/Tamil/Kannada/English); CJK garbage auto-filtered"},
            {"value": "tel", "label": "tel — lock Telugu only"},
            {"value": "hin", "label": "hin — lock Hindi only"},
            {"value": "tam", "label": "tam — lock Tamil only"},
            {"value": "kan", "label": "kan — lock Kannada only"},
            {"value": "mal", "label": "mal — lock Malayalam only"},
            {"value": "eng", "label": "eng — lock English only"},
        ],
        "description": "STT language for ElevenLabs Scribe. 'auto' recognises ALL Indian languages "
                       "+ English at once (auto-detect); the pipeline drops any Chinese/Korean "
                       "hallucinations (noise/echo) via a CJK filter, so auto is clean now. Lock to "
                       "one ISO-639-3 code only if auto ever confuses two similar languages.",
    },

    # ---------- Region ----------
    {
        "key": "ELEVENLABS_REGION",
        "label": "ELEVENLABS_REGION",
        "group": "ElevenLabs region",
        "type": "select",
        "default": "global",
        "choices": [
            {"value": "global", "label": "global — api.elevenlabs.io"},
            {"value": "india", "label": "india — api.in.residency.elevenlabs.io"},
            {"value": "eu", "label": "eu — api.eu.residency.elevenlabs.io"},
        ],
        "description": "ElevenLabs data-residency region. Sets both the STT and TTS endpoints "
                       "(HTTP + WebSocket). Your voice IDs must exist in the selected region.",
    },

    # ---------- VAD (Silero, local) ----------
    {
        "key": "VAD_CONFIDENCE",
        "label": "VAD_CONFIDENCE",
        "group": "VAD (Silero)",
        "type": "number", "min": 0.0, "max": 1.0, "step": 0.05,
        "default": 0.6,
        "description": "Minimum Silero voice confidence to treat audio as speech. Range 0.0-1.0. "
                       "0.6 detects normal speech reliably. Raise toward 0.8 only in very noisy "
                       "rooms (risks missing the first word).",
    },
    {
        "key": "VAD_START_SECS",
        "label": "VAD_START_SECS",
        "group": "VAD (Silero)",
        "type": "number", "min": 0.05, "max": 2.0, "step": 0.05,
        "default": 0.2,
        "description": "Sustained speech required before a turn opens. Range 0.05-2.0. 0.2 = "
                       "snappy turn starts (recommended for ElevenLabs Scribe). Raise if coughs/"
                       "blips trigger false starts.",
    },
    {
        "key": "VAD_STOP_SECS",
        "label": "VAD_STOP_SECS",
        "group": "VAD (Silero)",
        "type": "number", "min": 0.1, "max": 2.0, "step": 0.05,
        "default": 0.5,
        "description": "Silence to wait before ending a turn. Range 0.1-2.0. 0.5s balances "
                       "latency vs cutting users off mid-pause. Lower for faster replies.",
    },
    {
        "key": "VAD_MIN_VOLUME",
        "label": "VAD_MIN_VOLUME",
        "group": "VAD (Silero)",
        "type": "number", "min": 0.0, "max": 1.0, "step": 0.05,
        "default": 0.3,
        "description": "Minimum RMS loudness before audio counts as speech. Range 0.0-1.0. "
                       "0.3 lets normal/soft voices through. Raise only to ignore constant low hum "
                       "(higher values make the mic 'deaf' to quiet speech).",
    },

    # ---------- Turn-taking ----------
    {
        "key": "ALLOW_INTERRUPTIONS",
        "label": "ALLOW_INTERRUPTIONS",
        "group": "Turn-taking",
        "type": "bool",
        "default": False,
        "description": "If off, Riya always finishes her sentence and is never cut off "
                       "(recommended for teaching). If on, the user can interrupt mid-reply.",
    },
    {
        "key": "INTERRUPT_WORDS",
        "label": "INTERRUPT_WORDS",
        "group": "Turn-taking",
        "type": "int", "min": 1, "max": 10, "step": 1,
        "default": 2,
        "description": "When interruptions are on, how many words the user must say while Riya "
                       "is speaking before she stops. Higher debounces echo/noise.",
    },
]

_BY_KEY = {s["key"]: s for s in SCHEMA}


# --- Coercion ---------------------------------------------------------------

def _coerce(spec: dict, value: Any) -> Any:
    t = spec["type"]
    try:
        if t == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "on", "yes")
        if t == "int":
            return int(float(value))
        if t == "number":
            return float(value)
        if t == "select":
            v = str(value)
            allowed = {c["value"] for c in spec.get("choices", [])}
            return v if v in allowed else spec["default"]
        return str(value)
    except (TypeError, ValueError):
        return spec["default"]


# --- Persistence ------------------------------------------------------------

def _read_raw() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    if not SETTINGS_FILE.exists():
        SETTINGS_FILE.write_text(json.dumps({}, indent=2))
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_raw(data: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))


# --- Public API -------------------------------------------------------------

def get_all() -> dict[str, Any]:
    """Every setting, coerced to its type, with defaults applied."""
    stored = _read_raw()
    out: dict[str, Any] = {}
    for spec in SCHEMA:
        key = spec["key"]
        out[key] = _coerce(spec, stored[key]) if key in stored else spec["default"]
    return out


def list_settings() -> list[dict]:
    """Schema + current value, grouped order preserved — for the settings UI."""
    values = get_all()
    rows = []
    for spec in SCHEMA:
        rows.append({**spec, "value": values[spec["key"]]})
    return rows


def update(values: dict[str, Any]) -> dict[str, Any]:
    """Bulk-set known keys, persist, and return the fresh coerced config."""
    stored = _read_raw()
    for key, raw in values.items():
        spec = _BY_KEY.get(key)
        if spec is None:
            continue
        stored[key] = _coerce(spec, raw)
    _write_raw(stored)
    return get_all()


def elevenlabs_urls(cfg: dict[str, Any]) -> tuple[str, str, str]:
    """Return (http_base, ws_stream_url, ws_stt_host) for the configured region."""
    host = REGION_HOSTS.get(cfg.get("ELEVENLABS_REGION", "global"), REGION_HOSTS["global"])
    return f"https://{host}", f"wss://{host}", host

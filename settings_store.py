"""
settings_store.py — configurable voice-agent settings (Django-admin-style).

A flat list of tunable settings (LLM / TTS / STT / VAD / turn-taking), each with
a name, type, default, dropdown choices, and a human description. Values persist
to data/settings.json; anything not overridden falls back to its schema default.

Providers: TTS = ElevenLabs / Google Chirp3-HD / Sarvam Bulbul; STT = ElevenLabs /
Sarvam saaras / Google / Groq Whisper. The "Cost meter" group holds the rates the
per-call cost meter in bot.py prices usage with.

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


# Gemini TTS prebuilt voices (name + vibe from Google docs). Tutor-friendly first.
GEMINI_VOICES = [
    ("Achird", "Friendly"), ("Sulafat", "Warm"), ("Aoede", "Breezy"),
    ("Leda", "Youthful"), ("Callirrhoe", "Easy-going"), ("Vindemiatrix", "Gentle"),
    ("Kore", "Firm"), ("Puck", "Upbeat"), ("Autonoe", "Bright"), ("Zephyr", "Bright"),
    ("Charon", "Informative"), ("Sadachbia", "Lively"), ("Laomedeia", "Upbeat"),
    ("Achernar", "Soft"), ("Despina", "Smooth"), ("Algieba", "Smooth"),
    ("Iapetus", "Clear"), ("Erinome", "Clear"), ("Umbriel", "Easy-going"),
    ("Enceladus", "Breathy"), ("Schedar", "Even"), ("Gacrux", "Mature"),
    ("Orus", "Firm"), ("Fenrir", "Excitable"), ("Rasalgethi", "Informative"),
    ("Alnilam", "Firm"), ("Pulcherrima", "Forward"), ("Zubenelgenubi", "Casual"),
    ("Sadaltager", "Knowledgeable"), ("Algenib", "Gravelly"),
]


def gemini_voices() -> list[dict]:
    """Gemini TTS prebuilt voices for the test-page picker. voice_id == the Gemini
    voice name (e.g. 'Kore') — passed straight to gemini-3.1-flash-tts-preview."""
    return [{"voice_id": name, "name": f"{name} — {vibe}",
             "languages": ["multilingual"]} for name, vibe in GEMINI_VOICES]


# Sarvam Bulbul speakers per model. Sarvam deprecated bulbul:v2 (the API answers
# 400 "use bulbul:v3" — checked 10 Sep 2026), so only v3 is offered. Speaker sets
# differ per model, so a stale name (e.g. v2's "anushka") falls back in bot.py.
# Every speaker reads all Bulbul languages; the call's language sets the locale.
SARVAM_SPEAKERS = {
    "bulbul:v3": ["priya", "ritu", "neha", "pooja", "simran", "kavya", "ishita", "shreya",
                  "roopa", "amelia", "sophia", "shubh", "aditya", "rahul", "rohan", "amit",
                  "dev", "ratan", "varun", "manan", "sumit", "kabir", "aayan", "ashutosh",
                  "advait"],
}


def sarvam_speakers(model: str) -> list[str]:
    return list(SARVAM_SPEAKERS.get(model, SARVAM_SPEAKERS["bulbul:v3"]))


def sarvam_voices(model: str) -> list[dict]:
    """Bulbul speakers for the picker, for the given model."""
    return [{"voice_id": name, "name": name.title(), "model": model,
             "languages": ["hi", "te", "ta", "kn", "ml", "bn", "en"]}
            for name in sarvam_speakers(model)]


# --- Schema -----------------------------------------------------------------
# type: "select" | "number" | "int" | "bool" | "text"
# select entries carry `choices` = [{"value", "label"}]
SCHEMA: list[dict[str, Any]] = [
    # ---------- LLM (Google Gemini) ----------
    {
        "key": "LLM_PROVIDER",
        "label": "LLM_PROVIDER",
        "group": "LLM provider",
        "type": "select",
        "default": "gemini",
        "choices": [
            {"value": "gemini", "label": "gemini — Google Gemini (GEMINI_MODEL below)"},
            {"value": "groq", "label": "groq — Groq-hosted open models (GROQ_LLM_MODEL below; fast, fewer content filters)"},
        ],
        "description": "Which LLM answers the learner. Temperature and max tokens (GEMINI_* keys) apply to both.",
    },
    {
        "key": "GROQ_LLM_MODEL",
        "label": "GROQ_LLM_MODEL",
        "group": "LLM provider",
        "type": "select",
        "default": "openai/gpt-oss-120b",
        "choices": [
            {"value": "openai/gpt-oss-120b", "label": "openai/gpt-oss-120b — best quality on Groq, good Hindi, ~$0.15/$0.60 per 1M"},
            {"value": "openai/gpt-oss-20b", "label": "openai/gpt-oss-20b — fastest, cheaper, weaker Indic"},
            {"value": "qwen/qwen3.8-27b", "label": "qwen/qwen3.8-27b — Qwen 3.8, looser style, decent Hindi"},
            {"value": "allam-2-7b", "label": "allam-2-7b — small Arabic/English model (not for Indic)"},
        ],
        "description": "Groq model used when LLM_PROVIDER = groq (list checked against the account on 28 Sep 2026).",
    },
    {
        "key": "GROQ_REASONING_EFFORT",
        "label": "GROQ_REASONING_EFFORT",
        "group": "LLM provider",
        "type": "select",
        "default": "low",
        "choices": [
            {"value": "low", "label": "low — minimal hidden reasoning; fastest, cheapest (voice default)"},
            {"value": "medium", "label": "medium"},
            {"value": "high", "label": "high — slow, many billed reasoning tokens"},
        ],
        "description": "For gpt-oss models on Groq: how much hidden reasoning before the spoken reply. "
                       "Reasoning tokens are billed as output and add latency.",
    },
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
            {"value": "google", "label": "google — Google Cloud TTS Chirp3-HD (service account; realtime, HD Indian voices)"},
            {"value": "gemini_lite", "label": "gemini_lite — Gemini 3.8 Flash-Lite TTS ($6/1M audio tok; cheaper, faster)"},
            {"value": "gemini_flash", "label": "gemini_flash — Gemini 3.8 Flash TTS ($9/1M audio tok; best quality)"},
            {"value": "gemini", "label": "gemini — Gemini 3.1 Flash TTS preview (older generateContent path; model below)"},
            {"value": "sarvam", "label": "sarvam — Sarvam Bulbul v3 (streaming WS; cheap stack)"},
        ],
        "description": "Which TTS engine synthesises Riya's voice. 'elevenlabs' uses the ElevenLabs "
                       "settings below (streaming WS; eleven_v3 via HTTP). 'google' uses Cloud TTS "
                       "Chirp3-HD — voice id composed per session language, ~0.2s first audio. "
                       "'sarvam' uses Bulbul (see 'TTS (Sarvam Bulbul)') — pick a language on the "
                       "call page, Bulbul needs it (defaults to Hindi otherwise).",
    },

    # ---------- STT provider ----------
    {
        "key": "STT_PROVIDER",
        "label": "STT_PROVIDER",
        "group": "STT provider",
        "type": "select",
        "default": "elevenlabs",
        "choices": [
            {"value": "elevenlabs", "label": "elevenlabs — ElevenLabs Scribe (realtime WebSocket, low latency)"},
            {"value": "sarvam", "label": "sarvam — Sarvam saaras (Indian codemix, native script; best Telugu accuracy)"},
            {"value": "google", "label": "google — Google Cloud STT (service account; realtime, low latency)"},
            {"value": "groq", "label": "groq — Groq Whisper (segmented per VAD turn; ~$0.04/hr, cheap stack)"},
        ],
        "description": "Which engine transcribes the learner's speech. 'elevenlabs' = realtime Scribe "
                       "(lowest latency, ~50ms). 'sarvam' = saaras codemix — best Indian-language "
                       "accuracy, native script (~300-500ms batch). 'google' = Cloud STT streaming. "
                       "'groq' = Whisper on each VAD-cut utterance (see 'STT (Groq Whisper)').",
    },

    # ---------- TTS (Sarvam Bulbul) ----------
    {
        "key": "SARVAM_TTS_MODEL",
        "label": "SARVAM_TTS_MODEL",
        "group": "TTS (Sarvam Bulbul)",
        "type": "select",
        "default": "bulbul:v3",
        "choices": [
            {"value": "bulbul:v3", "label": "bulbul:v3 — current Bulbul (v2 is deprecated by Sarvam)"},
        ],
        "description": "Bulbul model (used when TTS_PROVIDER = sarvam). Sarvam retired bulbul:v2 — "
                       "its API now rejects v2 requests — so v3 is the only option.",
    },
    {
        "key": "SARVAM_TTS_VOICE",
        "label": "SARVAM_TTS_VOICE",
        "group": "TTS (Sarvam Bulbul)",
        "type": "text",
        "default": "",
        "description": "Fallback speaker when no voice is picked on the call page (blank = priya). "
                       "A name that isn't a speaker of the model falls back to priya.",
    },
    {
        "key": "SARVAM_TTS_PACE",
        "label": "SARVAM_TTS_PACE",
        "group": "TTS (Sarvam Bulbul)",
        "type": "number", "min": 0.5, "max": 2.0, "step": 0.05,
        "default": 1.0,
        "description": "Speaking pace. Range 0.5-2.0. The call-page speed slider wins whenever it "
                       "is not at 1.00×.",
    },
    {
        "key": "SARVAM_TTS_TEMPERATURE",
        "label": "SARVAM_TTS_TEMPERATURE",
        "group": "TTS (Sarvam Bulbul)",
        "type": "number", "min": 0.01, "max": 1.0, "step": 0.05,
        "default": 0.6,
        "description": "Expressiveness / randomness. Range 0.01-1.0. Lower = steadier delivery.",
    },
    {
        "key": "SARVAM_TTS_PREPROCESSING",
        "label": "SARVAM_TTS_PREPROCESSING",
        "group": "TTS (Sarvam Bulbul)",
        "type": "bool",
        "default": True,
        "description": "Normalise numbers, ₹ amounts and English words in Indian-script text before "
                       "synthesis. Sarvam may force this on for v3.",
    },
    {
        "key": "SARVAM_TTS_MIN_BUFFER",
        "label": "SARVAM_TTS_MIN_BUFFER",
        "group": "TTS (Sarvam Bulbul)",
        "type": "int", "min": 10, "max": 200, "step": 10,
        "default": 50,
        "description": "Characters Sarvam buffers before generating audio. Lower = faster first "
                       "audio, possibly choppier prosody. Main latency knob.",
    },

    # ---------- STT (Groq Whisper) ----------
    {
        "key": "GROQ_STT_MODEL",
        "label": "GROQ_STT_MODEL",
        "group": "STT (Groq Whisper)",
        "type": "select",
        "default": "whisper-large-v3",
        "choices": [
            {"value": "whisper-large-v3", "label": "whisper-large-v3 — $0.111/hr, usable Telugu (recommended)"},
            {"value": "whisper-large-v3-turbo", "label": "whisper-large-v3-turbo — $0.04/hr, OK Hindi, garbles Telugu"},
        ],
        "description": "Groq Whisper model (used when STT_PROVIDER = groq). In our Telugu test turbo "
                       "garbled most words and, with no language hint, wrote Telugu speech in Gujarati "
                       "script (dropped by the script gate); large-v3 was close to Sarvam saaras. Groq "
                       "bills each request at a 10 s minimum.",
    },
    {
        "key": "GROQ_STT_LANGUAGE",
        "label": "GROQ_STT_LANGUAGE",
        "group": "STT (Groq Whisper)",
        "type": "select",
        "default": "auto",
        "choices": [
            {"value": "auto", "label": "auto — follow the call-page language; Whisper auto-detect if none picked"},
            {"value": "hin", "label": "hin — lock Hindi"},
            {"value": "tel", "label": "tel — lock Telugu"},
            {"value": "tam", "label": "tam — lock Tamil"},
            {"value": "kan", "label": "kan — lock Kannada"},
            {"value": "mal", "label": "mal — lock Malayalam"},
            {"value": "ben", "label": "ben — lock Bengali"},
            {"value": "eng", "label": "eng — lock English"},
        ],
        "description": "Whisper takes ONE language hint. 'auto' passes the language picked on the "
                       "call page; with no pick it auto-detects (foreign-script output is dropped by "
                       "the gate). A lock here overrides the call page.",
    },

    # ---------- Cost meter ----------
    {
        "key": "RATE_SARVAM_TTS_INR_PER_10K_CHARS",
        "label": "RATE_SARVAM_TTS_INR_PER_10K_CHARS",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 1000.0, "step": 0.5,
        "default": 30.0,
        "description": "Sarvam bulbul:v3 price in ₹ per 10,000 characters.",
    },
    {
        "key": "RATE_GOOGLE_CHIRP_USD_PER_1M_CHARS",
        "label": "RATE_GOOGLE_CHIRP_USD_PER_1M_CHARS",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 1000.0, "step": 0.5,
        "default": 30.0,
        "description": "Google Cloud TTS Chirp3-HD price in $ per 1M characters.",
    },
    {
        "key": "RATE_GEMINI_LITE_TTS_USD_PER_1M_AUDIO_TOK",
        "label": "RATE_GEMINI_LITE_TTS_USD_PER_1M_AUDIO_TOK",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 1000.0, "step": 0.5,
        "default": 6.0,
        "description": "Gemini 3.8 Flash-Lite TTS: $ per 1M audio output tokens ($6 till 31 Dec 2026, "
                       "$12 from 1 Jan 2027). ~40 audio tokens per second of speech.",
    },
    {
        "key": "RATE_GEMINI_FLASH_TTS_USD_PER_1M_AUDIO_TOK",
        "label": "RATE_GEMINI_FLASH_TTS_USD_PER_1M_AUDIO_TOK",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 1000.0, "step": 0.5,
        "default": 9.0,
        "description": "Gemini 3.8 Flash TTS: $ per 1M audio output tokens ($9 till 31 Dec 2026, $18 after).",
    },
    {
        "key": "RATE_GEMINI_TTS_USD_PER_1M_TEXT_TOK",
        "label": "RATE_GEMINI_TTS_USD_PER_1M_TEXT_TOK",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 100.0, "step": 0.1,
        "default": 0.5,
        "description": "Gemini 3.8 TTS: $ per 1M text input tokens ($0.50 till 31 Dec 2026, $1 after).",
    },
    {
        "key": "RATE_SARVAM_STT_INR_PER_HOUR",
        "label": "RATE_SARVAM_STT_INR_PER_HOUR",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 1000.0, "step": 1,
        "default": 30.0,
        "description": "Sarvam saaras speech-to-text price in ₹ per audio hour. The call streams mic "
                       "audio to Sarvam the whole time, so the meter prices it on call length — an "
                       "upper bound until checked against the Sarvam dashboard.",
    },
    {
        "key": "RATE_GROQ_TURBO_USD_PER_HOUR",
        "label": "RATE_GROQ_TURBO_USD_PER_HOUR",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 10.0, "step": 0.001,
        "default": 0.04,
        "description": "Groq whisper-large-v3-turbo price in $ per audio hour.",
    },
    {
        "key": "RATE_GROQ_LARGE_USD_PER_HOUR",
        "label": "RATE_GROQ_LARGE_USD_PER_HOUR",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 10.0, "step": 0.001,
        "default": 0.111,
        "description": "Groq whisper-large-v3 price in $ per audio hour.",
    },
    {
        "key": "RATE_GROQ_MIN_BILLED_SECS",
        "label": "RATE_GROQ_MIN_BILLED_SECS",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 60.0, "step": 1,
        "default": 10.0,
        "description": "Groq's per-request billing floor in seconds — every utterance is billed at "
                       "least this long.",
    },
    {
        "key": "RATE_GROQ_LLM_USD_IN_PER_1M",
        "label": "RATE_GROQ_LLM_USD_IN_PER_1M",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 100.0, "step": 0.01,
        "default": 0.15,
        "description": "Groq LLM input price $/1M tokens (gpt-oss-120b list price; used when LLM_PROVIDER = groq).",
    },
    {
        "key": "RATE_GROQ_LLM_USD_OUT_PER_1M",
        "label": "RATE_GROQ_LLM_USD_OUT_PER_1M",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 100.0, "step": 0.01,
        "default": 0.60,
        "description": "Groq LLM output price $/1M tokens (gpt-oss-120b list price).",
    },
    {
        "key": "RATE_LLM_USD_IN_PER_1M",
        "label": "RATE_LLM_USD_IN_PER_1M",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 100.0, "step": 0.01,
        "default": 0.25,
        "description": "Gemini input price in $ per 1M tokens (3.1 flash-lite = 0.25).",
    },
    {
        "key": "RATE_LLM_USD_OUT_PER_1M",
        "label": "RATE_LLM_USD_OUT_PER_1M",
        "group": "Cost meter",
        "type": "number", "min": 0.0, "max": 100.0, "step": 0.01,
        "default": 1.50,
        "description": "Gemini output price in $ per 1M tokens (3.1 flash-lite = 1.50).",
    },
    {
        "key": "USD_INR",
        "label": "USD_INR",
        "group": "Cost meter",
        "type": "number", "min": 1.0, "max": 200.0, "step": 0.5,
        "default": 88.0,
        "description": "Exchange rate used to convert $ rates to ₹.",
    },

    # ---------- Sarvam STT ----------
    {
        "key": "SARVAM_STT_MODEL",
        "label": "SARVAM_STT_MODEL",
        "group": "STT (Sarvam)",
        "type": "select",
        "default": "saaras:v3",
        "choices": [
            {"value": "saaras:v3", "label": "saaras:v3 — best quality, codemix support (recommended)"},
            {"value": "saarika:v2.5", "label": "saarika:v2.5 — faster, needs explicit language"},
        ],
        "description": "Sarvam speech-to-text model (used when STT_PROVIDER = sarvam). saaras:v3 "
                       "auto-detects language and handles Hindi/Telugu + English codemix in native "
                       "script — best Indian accuracy in our tests.",
    },
    {
        "key": "SARVAM_STT_MODE",
        "label": "SARVAM_STT_MODE",
        "group": "STT (Sarvam)",
        "type": "select",
        "default": "codemix",
        "choices": [
            {"value": "codemix", "label": "codemix — Hindi/Telugu + English mixed speech (recommended)"},
            {"value": "transcribe", "label": "transcribe — standard single-language"},
            {"value": "verbatim", "label": "verbatim — exact transcription, no normalisation"},
        ],
        "description": "Sarvam transcription mode (saaras:v3 only). codemix handles mixed "
                       "Indian-language + English speech without specifying a language.",
    },

    # ---------- Google Cloud audio (service account) ----------
    # ---------- Gemini TTS (API key) ----------
    {
        "key": "GEMINI_TTS_MODEL",
        "label": "GEMINI_TTS_MODEL",
        "group": "TTS (Gemini)",
        "type": "select",
        "default": "gemini-3.1-flash-tts-preview",
        "choices": [
            {"value": "gemini-3.1-flash-tts-preview",
             "label": "gemini-3.1-flash-tts-preview — generateContent path ($20/1M audio tok)"},
        ],
        "description": "Model for TTS_PROVIDER = gemini (the 3.1 preview path). The 3.8 models are "
                       "their own providers: gemini_lite / gemini_flash.",
    },
    {
        "key": "GEMINI_TTS_CHUNKING",
        "label": "GEMINI_TTS_CHUNKING",
        "group": "TTS (Gemini)",
        "type": "select",
        "default": "response",
        "choices": [
            {"value": "response", "label": "response — one TTS request per reply: no gaps between sentences (recommended)"},
            {"value": "sentence", "label": "sentence — one request per sentence: first word sooner, ~1.5 s hole after every । . ?"},
        ],
        "description": "Gemini 3.8 TTS has ~1.5 s time-to-first-byte per request. Per-sentence "
                       "chunking (pipecat default) therefore pauses after every sentence; "
                       "'response' sends the whole reply at once.",
    },
    {
        "key": "GEMINI_TTS_VOICE",
        "label": "GEMINI_TTS_VOICE",
        "group": "TTS (Gemini)",
        "type": "text",
        "default": "Aoede",
        "description": "Default Gemini prebuilt voice (Aoede, Kore, Sulafat, Achird, …). The voice "
                       "picked on the call page overrides this. Same 30 names as Chirp3-HD.",
    },
    {
        "key": "GEMINI_TTS_STYLE",
        "label": "GEMINI_TTS_STYLE",
        "group": "TTS (Gemini)",
        "type": "text",
        "default": "warm, friendly Indian English teacher; natural conversational pace",
        "description": "Delivery style sent as a speech_metadata annotation with every line "
                       "(3.8 models) or prefixed as an instruction (3.1 preview). Empty = model default.",
    },
    {
        "key": "GOOGLE_TTS_VOICE",
        "label": "GOOGLE_TTS_VOICE",
        "group": "Google Cloud audio",
        "type": "text",
        "default": "Aoede",
        "description": "Chirp3-HD voice NAME (e.g. Aoede, Charon, Kore, Achird). The final "
                       "voice id is built as '<lang>-Chirp3-HD-<name>' from the picked language. "
                       "Used when TTS_PROVIDER = google.",
    },
    {
        "key": "GOOGLE_STT_MODEL",
        "label": "GOOGLE_STT_MODEL",
        "group": "Google Cloud audio",
        "type": "text",
        "default": "chirp_2",
        "description": "Google Cloud STT model (used when STT_PROVIDER = google). chirp_2 = "
                       "multilingual incl. Telugu, streaming (us-central1). latest_long does NOT "
                       "support Telugu — avoid for Indian languages.",
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
            {"value": "ben", "label": "ben — lock Bengali only"},
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
    # ---------- Backchannel (listening fillers) ----------
    {
        "key": "BACKCHANNEL_ENABLED",
        "label": "BACKCHANNEL_ENABLED",
        "group": "Backchannel (listening fillers)",
        "type": "bool",
        "default": False,
        "description": "While the learner is talking, Riya softly reacts like a real listener: mostly the "
                       "recorded hums (data/backchannel/custom), sometimes a word (अच्छा / ओके / ठीक है), "
                       "every 2-3 beats and at their pauses. "
                       "Clips are Chirp3-HD in the picked voice, cached in data/backchannel/. "
                       "They do not count as Riya's turn (no captions, no interruption).",
    },
    {
        "key": "BACKCHANNEL_MIN_SECS",
        "label": "BACKCHANNEL_MIN_SECS",
        "group": "Backchannel (listening fillers)",
        "type": "number",
        "default": 4.0,
        "description": "Shortest gap between fillers while the learner talks (≈2 beats of 2 s).",
    },
    {
        "key": "BACKCHANNEL_MAX_SECS",
        "label": "BACKCHANNEL_MAX_SECS",
        "group": "Backchannel (listening fillers)",
        "type": "number",
        "default": 6.0,
        "description": "Longest gap between fillers (≈3 beats); each gap is random between min and max, so it never sounds metronomic.",
    },
    {
        "key": "BACKCHANNEL_GAIN",
        "label": "BACKCHANNEL_GAIN",
        "group": "Backchannel (listening fillers)",
        "type": "number",
        "default": 0.5,
        "description": "Filler loudness relative to Riya's normal voice (0.0–1.0). Keep it soft.",
    },
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

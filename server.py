"""
server.py — FastAPI app.

Routes:
  GET  /                          static/index.html (test page)
  GET  /admin                     static/admin.html (admin page)
  GET  /static/*                  other static files (css/js)
  GET    /api/prompts             list
  POST   /api/prompts             create
  GET    /api/prompts/{entry_id}  get
  PUT    /api/prompts/{entry_id}  update
  DELETE /api/prompts/{entry_id}  delete
  (same five under /api/voices)
  GET  /voice-lab                 static/voice_lab.html (A/B voices, mic → Whisper)
  GET  /api/sarvam/voices         Bulbul speakers for ?model= (default: saved model)
  POST /api/voice-lab/tts         render one line with Google Chirp3-HD, Sarvam or ElevenLabs
  POST /api/voice-lab/stt         transcribe audio with Groq Whisper
  WS   /ws                        voice call -> Pipecat
"""

import asyncio
import base64
import os
import time
from pathlib import Path

import aiohttp
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

import settings_store
import storage
from bot import _US_FIX, GOOGLE_LANG_CODE, run_bot

load_dotenv()

# Hosted deploys (Hugging Face Spaces etc.) can't ship the gitignored service-account
# file, so accept its JSON via GCP_SERVICE_ACCOUNT_JSON and materialise it on boot.
_SA_JSON = os.getenv("GCP_SERVICE_ACCOUNT_JSON")
if _SA_JSON and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
    _sa_path = Path(__file__).parent / "gcp-service-account.json"
    _sa_path.write_text(_SA_JSON)
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_sa_path)

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "7860"))

app = FastAPI(title="AI Riya prompt tester")


# ----- Static pages -----

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin")
async def admin():
    return FileResponse(STATIC_DIR / "admin.html")


@app.get("/settings")
async def settings_page():
    return FileResponse(STATIC_DIR / "settings.html")


@app.get("/voice-lab")
async def voice_lab_page():
    return FileResponse(STATIC_DIR / "voice_lab.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ----- Schemas -----

class PromptCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    system_prompt: str = Field(..., min_length=1)
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class PromptUpdate(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    tags: list[str] | None = None
    notes: str | None = None


class VoiceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    voice_id: str = Field(..., min_length=1)
    language: str = ""
    tags: list[str] = Field(default_factory=list)
    notes: str = ""


class VoiceUpdate(BaseModel):
    name: str | None = None
    voice_id: str | None = None
    language: str | None = None
    tags: list[str] | None = None
    notes: str | None = None


# ----- Prompts API -----

@app.get("/api/prompts")
async def api_list_prompts():
    return storage.list_prompts()


@app.post("/api/prompts", status_code=201)
async def api_create_prompt(body: PromptCreate):
    return storage.create_prompt(**body.model_dump())


@app.get("/api/prompts/{entry_id}")
async def api_get_prompt(entry_id: str):
    p = storage.get_prompt(entry_id)
    if not p:
        raise HTTPException(404, "prompt not found")
    return p


@app.put("/api/prompts/{entry_id}")
async def api_update_prompt(entry_id: str, body: PromptUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = storage.update_prompt(entry_id, **fields)
    if not updated:
        raise HTTPException(404, "prompt not found")
    return updated


@app.delete("/api/prompts/{entry_id}", status_code=204)
async def api_delete_prompt(entry_id: str):
    if not storage.delete_prompt(entry_id):
        raise HTTPException(404, "prompt not found")


# ----- Voices API -----

@app.get("/api/voices")
async def api_list_voices():
    return storage.list_voices()


@app.post("/api/voices", status_code=201)
async def api_create_voice(body: VoiceCreate):
    return storage.create_voice(**body.model_dump())


@app.get("/api/voices/{entry_id}")
async def api_get_voice(entry_id: str):
    v = storage.get_voice(entry_id)
    if not v:
        raise HTTPException(404, "voice not found")
    return v


@app.put("/api/voices/{entry_id}")
async def api_update_voice(entry_id: str, body: VoiceUpdate):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    updated = storage.update_voice(entry_id, **fields)
    if not updated:
        raise HTTPException(404, "voice not found")
    return updated


@app.delete("/api/voices/{entry_id}", status_code=204)
async def api_delete_voice(entry_id: str):
    if not storage.delete_voice(entry_id):
        raise HTTPException(404, "voice not found")


# ----- Settings API -----

class SettingsUpdate(BaseModel):
    values: dict


@app.get("/api/settings")
async def api_list_settings():
    return settings_store.list_settings()


@app.put("/api/settings")
async def api_update_settings(body: SettingsUpdate):
    return settings_store.update(body.values)


@app.get("/api/smallest/voices")
async def api_smallest_voices():
    """Curated Smallest.ai voices (all support hi/en/ta/te/kn/ml)."""
    return settings_store.smallest_voices()


@app.get("/api/gemini/voices")
async def api_gemini_voices():
    """Gemini TTS prebuilt voices (voice_id == Gemini voice name)."""
    return settings_store.gemini_voices()


@app.get("/api/sarvam/voices")
async def api_sarvam_voices(model: str | None = None):
    """Bulbul speakers for `model` (default: the saved SARVAM_TTS_MODEL)."""
    return settings_store.sarvam_voices(model or settings_store.get_all()["SARVAM_TTS_MODEL"])


# ----- Voice Lab (offline A/B, no Pipecat) -----

class LabTTS(BaseModel):
    provider: str  # "sarvam" | "elevenlabs"
    text: str = Field(..., min_length=1, max_length=2500)
    voice: str = Field(..., min_length=1)
    model: str = ""  # blank = saved setting
    language: str = "hin"
    pace: float = 1.0


class LabSTT(BaseModel):
    audio_b64: str = Field(..., min_length=1)
    mime: str = "audio/wav"
    model: str = "whisper-large-v3-turbo"
    language: str = ""  # hin/tel/… — blank = Whisper auto-detect


def _chirp_synthesize(text: str, voice_name: str, pace: float) -> bytes:
    """Chirp3-HD render as 24 kHz WAV (sync client — call via a thread). Applies the
    same "us"→"uss" respell as live calls."""
    from google.cloud import texttospeech as tts

    creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or str(BASE_DIR / "gcp-service-account.json")
    client = tts.TextToSpeechClient.from_service_account_file(creds)
    resp = client.synthesize_speech(
        input=tts.SynthesisInput(text=_US_FIX.sub("uss", text)),
        voice=tts.VoiceSelectionParams(language_code=voice_name[:5], name=voice_name),
        audio_config=tts.AudioConfig(
            audio_encoding=tts.AudioEncoding.LINEAR16,
            sample_rate_hertz=24000,
            speaking_rate=max(0.25, min(2.0, pace)),
        ),
    )
    return resp.audio_content


@app.post("/api/voice-lab/tts")
async def api_lab_tts(body: LabTTS):
    """Render one line with one provider/voice (full HTTP render) so the Voice Lab
    can play samples side by side. Sarvam cost = chars × the Cost meter rate."""
    cfg = settings_store.get_all()
    started = time.monotonic()
    cost = None
    async with aiohttp.ClientSession() as session:
        if body.provider == "sarvam":
            model = body.model or cfg["SARVAM_TTS_MODEL"]
            if body.voice not in settings_store.sarvam_speakers(model):
                raise HTTPException(400, f"{body.voice!r} is not a {model} speaker")
            payload = {
                "text": body.text,
                "target_language_code": "en-IN" if body.language == "eng"
                else GOOGLE_LANG_CODE.get(body.language, "hi-IN"),
                "speaker": body.voice,
                "model": model,
                "pace": max(0.5, min(2.0, body.pace)),
                "enable_preprocessing": cfg["SARVAM_TTS_PREPROCESSING"],
                "temperature": cfg["SARVAM_TTS_TEMPERATURE"],
            }
            headers = {"api-subscription-key": os.getenv("SARVAM_API_KEY", "")}
            async with session.post("https://api.sarvam.ai/text-to-speech",
                                    json=payload, headers=headers) as r:
                if r.status != 200:
                    raise HTTPException(502, f"Sarvam {r.status}: {await r.text()}")
                audio_b64 = (await r.json())["audios"][0]
            mime = "audio/wav"
            cost = len(body.text) / 10_000 * cfg["RATE_SARVAM_TTS_INR_PER_10K_CHARS"]
        elif body.provider == "elevenlabs":
            http_base, _, _ = settings_store.elevenlabs_urls(cfg)
            model = body.model or cfg["ELEVENLABS_TTS_MODEL"]
            payload = {
                "text": body.text,
                "model_id": model,
                "voice_settings": {
                    "stability": cfg["ELEVENLABS_TTS_STABILITY"],
                    "similarity_boost": cfg["ELEVENLABS_TTS_SIMILARITY_BOOST"],
                    "style": cfg["ELEVENLABS_TTS_STYLE"],
                    "use_speaker_boost": cfg["ELEVENLABS_TTS_USE_SPEAKER_BOOST"],
                    "speed": max(0.7, min(1.2, body.pace)),
                },
            }
            headers = {"xi-api-key": os.getenv("ELEVENLABS_API_KEY", "")}
            url = f"{http_base}/v1/text-to-speech/{body.voice}?output_format=mp3_44100_128"
            async with session.post(url, json=payload, headers=headers) as r:
                if r.status != 200:
                    raise HTTPException(502, f"ElevenLabs {r.status}: {await r.text()}")
                audio_b64 = base64.b64encode(await r.read()).decode()
            mime = "audio/mpeg"
        elif body.provider == "google":
            model = "chirp3-hd"
            lang = "en-IN" if body.language == "eng" else GOOGLE_LANG_CODE.get(body.language, "hi-IN")
            try:
                audio = await asyncio.to_thread(
                    _chirp_synthesize, body.text, f"{lang}-Chirp3-HD-{body.voice}", body.pace)
            except Exception as e:
                raise HTTPException(502, f"Google TTS: {e}")
            audio_b64 = base64.b64encode(audio).decode()
            mime = "audio/wav"
            cost = len(body.text) / 1e6 * cfg["RATE_GOOGLE_CHIRP_USD_PER_1M_CHARS"] * cfg["USD_INR"]
        else:
            raise HTTPException(400, "provider must be 'google', 'sarvam' or 'elevenlabs'")
    return {
        "audio_b64": audio_b64, "mime": mime, "model": model, "voice": body.voice,
        "chars": len(body.text), "cost_inr": cost,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


_GROQ_MODELS = {"whisper-large-v3-turbo", "whisper-large-v3"}
_AUDIO_EXT = {"audio/wav": "wav", "audio/mpeg": "mp3", "audio/webm": "webm",
              "audio/ogg": "ogg", "audio/mp4": "m4a"}


@app.post("/api/voice-lab/stt")
async def api_lab_stt(body: LabSTT):
    """Transcribe a mic recording or a rendered sample with Groq Whisper, priced
    with Groq's per-request billing floor."""
    if body.model not in _GROQ_MODELS:
        raise HTTPException(400, f"model must be one of {sorted(_GROQ_MODELS)}")
    cfg = settings_store.get_all()
    mime = body.mime.split(";")[0].strip()
    form = aiohttp.FormData()
    form.add_field("file", base64.b64decode(body.audio_b64),
                   filename=f"audio.{_AUDIO_EXT.get(mime, 'webm')}", content_type=mime)
    form.add_field("model", body.model)
    form.add_field("response_format", "verbose_json")
    code = GOOGLE_LANG_CODE.get(body.language)
    if code:
        form.add_field("language", code.split("-")[0])
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {os.getenv('GROQ_API_KEY', '')}"}
    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.groq.com/openai/v1/audio/transcriptions",
                                data=form, headers=headers) as r:
            if r.status != 200:
                raise HTTPException(502, f"Groq {r.status}: {await r.text()}")
            data = await r.json()
    duration = float(data.get("duration") or 0)
    billed = max(duration, cfg["RATE_GROQ_MIN_BILLED_SECS"])
    rate = (cfg["RATE_GROQ_LARGE_USD_PER_HOUR"] if body.model == "whisper-large-v3"
            else cfg["RATE_GROQ_TURBO_USD_PER_HOUR"])
    segments = data.get("segments") or []
    return {
        "text": (data.get("text") or "").strip(),
        "language": data.get("language"),
        "duration_s": duration,
        "billed_s": billed,
        "cost_inr": billed / 3600 * rate * cfg["USD_INR"],
        "no_speech_prob": max((s.get("no_speech_prob", 0) for s in segments), default=None),
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


# ----- WebSocket: voice call -----

@app.websocket("/ws")
async def ws_voice(websocket: WebSocket):
    await websocket.accept()
    try:
        config = await websocket.receive_json()
    except WebSocketDisconnect:
        return

    system_prompt = (config.get("system_prompt") or "").strip()
    voice_id = (config.get("voice_id") or "").strip()
    if not system_prompt or not voice_id:
        await websocket.close(code=1008, reason="missing system_prompt or voice_id")
        return

    # Optional tuning knobs from the browser; clamp to safe ranges.
    try:
        speed = float(config.get("speed", 1.0))
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.7, min(1.2, speed))

    # temperature: None → bot falls back to the saved GEMINI_TEMPERATURE setting.
    raw_temp = config.get("temperature", None)
    if raw_temp is None:
        temperature = None
    else:
        try:
            temperature = max(0.0, min(2.0, float(raw_temp)))
        except (TypeError, ValueError):
            temperature = None

    # Optional pre-selected language (hin/tel/tam/kan/mal/eng). When set, the bot
    # opens directly in that language instead of asking the learner to pick one.
    language = (config.get("language") or "").strip().lower() or None

    logger.info(
        f"Starting bot: voice_id={voice_id}, prompt_len={len(system_prompt)}, "
        f"speed={speed}, temperature={temperature}, language={language}"
    )
    try:
        await run_bot(
            websocket, system_prompt, voice_id,
            speed=speed, temperature=temperature, language=language,
        )
    except Exception as e:
        logger.exception(f"bot error: {e}")
    finally:
        logger.info("WebSocket session ended")


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)

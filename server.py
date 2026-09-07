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
  WS   /ws                        voice call -> Pipecat
"""

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field

import settings_store
import storage
from bot import run_bot

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
